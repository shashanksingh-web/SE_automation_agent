"""Real authentication (added 2026-09-14) -- see planning.models.UserProfile's own
docstring for why this exists at all (there was no real auth anywhere before this).

Two families of endpoint here:
- /auth/* -- login/logout/me/change-password. Anyone can call these (that's the point
  of a login endpoint); "me" and "change-password" require an existing session.
- /admin/users/* -- user account management. Every one of these requires an
  authenticated ADMIN (see require_admin below) -- unlike the rest of this app's
  /admin/* endpoints (routing overrides, DC selection, pipeline config), which
  pre-date real auth and are deliberately left open for now (see this session's own
  scoping: "full real login now" was chosen, but retrofitting every existing admin
  endpoint is a separate, larger change not requested here). Creating accounts and
  setting passwords is the one part of "admin" that must not be left wide open even
  while the rest of the admin surface still is.
"""
from __future__ import annotations

import json
from functools import wraps

from django.contrib.auth import authenticate, login as django_login, logout as django_logout, update_session_auth_hash
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST

from .models import UserProfile


def _serialize_authenticated_user(user: User) -> dict:
    """Exact shape of src/features/rbac/types.ts AuthenticatedUser -- camelCase, not
    this app's usual PascalCase API convention, since this is consumed directly by
    that interface with no normalization layer in between (deliberate: this is a new,
    separate concern from the rest of the planning API, not part of that family)."""
    profile = user.profile
    return {
        "role": profile.role,
        "email": profile.email,
        "employeeCode": profile.employee_code or None,
        "name": profile.name or user.username,
    }


def require_admin(view_func):
    """401 if not logged in, 403 if logged in but not an ADMIN profile. A user created
    via createsuperuser (or otherwise lacking a UserProfile entirely) is treated as
    not-admin rather than crashing on the missing OneToOne -- fail closed, not 500."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=401)
        profile = getattr(request.user, "profile", None)
        if profile is None or profile.role != UserProfile.Role.ADMIN:
            return JsonResponse({"error": "Admin role required"}, status=403)
        return view_func(request, *args, **kwargs)
    return wrapper


def _json_body(request) -> dict:
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return {}


@csrf_exempt
@require_POST
def auth_login(request):
    """POST /api/planning/auth/login/ {username, password} -> 200 AuthenticatedUser,
    401 {"error"} on bad credentials OR a correct password for a deactivated account
    (authenticate() refuses is_active=False users itself, same message either way --
    never reveal which one it was, standard practice), 422 if the account has no
    UserProfile (created outside admin_users_create, e.g. via createsuperuser)."""
    body = _json_body(request)
    username, password = body.get("username"), body.get("password")
    if not username or not password:
        return JsonResponse({"error": "username and password are required"}, status=400)
    user = authenticate(request, username=username, password=password)
    if user is None:
        return JsonResponse({"error": "Incorrect username or password"}, status=401)
    if not hasattr(user, "profile"):
        return JsonResponse({"error": "This account has no role profile configured -- contact an admin"}, status=422)
    django_login(request, user)
    return JsonResponse(_serialize_authenticated_user(user))


@csrf_exempt
@require_POST
def auth_logout(request):
    django_logout(request)
    return JsonResponse({})


@require_GET
def auth_me(request):
    """GET /api/planning/auth/me/ -- the frontend calls this once on app load to
    restore a session across a page refresh (real session cookie now, replacing the
    old sessionStorage-of-a-locally-typed-object stand-in). 401 if not logged in --
    an expected, routine response (every logged-out page load hits this), not an error
    the frontend should surface."""
    if not request.user.is_authenticated or not hasattr(request.user, "profile"):
        return JsonResponse({"error": "Not authenticated"}, status=401)
    return JsonResponse(_serialize_authenticated_user(request.user))


@csrf_exempt
@require_POST
def auth_change_password(request):
    """POST /api/planning/auth/change-password/ {current_password, new_password} --
    self-service, requires an existing session. Verifies current_password first (a
    logged-in session alone isn't authorization to blindly set a new password -- same
    "prove you still know it" convention every real change-password flow uses).
    update_session_auth_hash is required here, not optional -- set_password() rotates
    the password hash Django's session auth is keyed on, so without this the user's
    own successful password change would immediately log them out of the session they
    just used to make the change."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required"}, status=401)
    body = _json_body(request)
    current_password, new_password = body.get("current_password"), body.get("new_password")
    if not current_password or not new_password:
        return JsonResponse({"error": "current_password and new_password are required"}, status=400)
    if not request.user.check_password(current_password):
        return JsonResponse({"error": "Current password is incorrect"}, status=400)
    try:
        validate_password(new_password, user=request.user)
    except DjangoValidationError as e:
        return JsonResponse({"error": " ".join(e.messages)}, status=400)
    request.user.set_password(new_password)
    request.user.save()
    update_session_auth_hash(request, request.user)
    return JsonResponse({})


def _serialize_user_row(user: User) -> dict:
    profile = getattr(user, "profile", None)
    return {
        "Id": user.id,
        "Username": user.username,
        "Name": profile.name if profile else "",
        "Role": profile.role if profile else None,
        "Email": profile.email if profile else "",
        "Employee_Code": profile.employee_code if profile else "",
        "Is_Active": user.is_active,
        "Date_Joined": user.date_joined,
        "Last_Login": user.last_login,
    }


@require_GET
@require_admin
def admin_users_list(request):
    """GET /api/planning/admin/users/ -- every account, newest first. select_related
    avoids one query per row for the profile join."""
    users = User.objects.select_related("profile").order_by("-date_joined")
    return JsonResponse([_serialize_user_row(u) for u in users], safe=False, json_dumps_params={"default": str})


@csrf_exempt
@require_POST
@require_admin
def admin_users_create(request):
    """POST /api/planning/admin/users/ {username, password, name, role, email,
    employee_code} -> 201 the new user row, 400 on any validation failure (duplicate
    username, weak password per AUTH_PASSWORD_VALIDATORS, invalid role). email/
    employee_code are optional -- only the roles that actually use them (SE / ABM,RBM,
    ZBM respectively, see UserProfile's own docstring) need them set, but nothing here
    enforces that pairing server-side (same soft-trust posture the old client-side-only
    login already had for these two fields -- not tightened further here, out of scope
    for this change)."""
    body = _json_body(request)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role = body.get("role")
    if not username:
        return JsonResponse({"error": "username is required"}, status=400)
    if role not in UserProfile.Role.values:
        return JsonResponse({"error": f"role must be one of {UserProfile.Role.values}"}, status=400)
    if User.objects.filter(username=username).exists():
        return JsonResponse({"error": f"Username {username!r} is already taken"}, status=400)
    try:
        validate_password(password)
    except DjangoValidationError as e:
        return JsonResponse({"error": " ".join(e.messages)}, status=400)
    user = User.objects.create_user(username=username, password=password)
    UserProfile.objects.create(
        user=user, role=role, name=(body.get("name") or "").strip(),
        email=(body.get("email") or "").strip(), employee_code=(body.get("employee_code") or "").strip(),
    )
    return JsonResponse(_serialize_user_row(user), status=201, json_dumps_params={"default": str})


@csrf_exempt
@require_http_methods(["POST"])
@require_admin
def admin_users_set_active(request, user_id: int):
    """POST /api/planning/admin/users/<id>/set-active/ {is_active: bool} -- activate/
    deactivate. Deactivating doesn't kill that user's CURRENT session (Django's
    AuthenticationMiddleware re-checks is_active on the NEXT request, not retroactively
    -- acceptable here, same "takes effect from the next thing that checks it" posture
    the rest of this app's config overrides already have) but authenticate() (used by
    auth_login) already refuses a deactivated user's next login attempt for free."""
    body = _json_body(request)
    if "is_active" not in body:
        return JsonResponse({"error": "is_active is required"}, status=400)
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return JsonResponse({"error": f"No user with id={user_id}"}, status=404)
    if user.id == request.user.id and not body["is_active"]:
        return JsonResponse({"error": "You cannot deactivate your own account"}, status=400)
    user.is_active = bool(body["is_active"])
    user.save(update_fields=["is_active"])
    return JsonResponse(_serialize_user_row(user), json_dumps_params={"default": str})


@csrf_exempt
@require_POST
@require_admin
def admin_users_reset_password(request, user_id: int):
    """POST /api/planning/admin/users/<id>/reset-password/ {new_password} -- an admin
    setting a new password for ANOTHER account directly (no need to know the old one,
    unlike auth_change_password's self-service flow) -- e.g. after a forgot-password
    request made outside this system. Deliberately does NOT call
    update_session_auth_hash -- that only matters for the CALLER's own current
    session, and the caller here is the admin, not the account being reset."""
    body = _json_body(request)
    new_password = body.get("new_password") or ""
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return JsonResponse({"error": f"No user with id={user_id}"}, status=404)
    try:
        validate_password(new_password, user=user)
    except DjangoValidationError as e:
        return JsonResponse({"error": " ".join(e.messages)}, status=400)
    user.set_password(new_password)
    user.save(update_fields=["password"])
    return JsonResponse({})
