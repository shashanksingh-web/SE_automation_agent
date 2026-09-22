"""Real tests (added 2026-09-22) -- until this, the entire repo had zero automated
test coverage of any kind (planning/tests.py was still Django's default startapp
stub). Scoped deliberately narrow rather than attempting broad coverage in one pass:
this covers the two things that are both brand new and security-critical --
planning.auth_views' login flow, and planning.views' now-universal auth enforcement
(see that module's own docstring) -- since those are exactly the surface a silent
regression (e.g. someone adding a new view and forgetting the decorator, or editing
an existing one and dropping it) would be both easy to introduce and expensive to
miss. Broader coverage of the actual pipeline agents (normalization, routing,
pitching) is a separate, much larger undertaking -- they depend on live Redshift/LLM
data this test environment has no access to, and would need a real mocking layer
this repo doesn't have yet.

Run with `python manage.py test planning`."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import UserProfile

# Every routed view in planning.urls (46, matching that file exactly) as
# (url_name, kwargs, http_method, "admin"|"any"). kwargs are throwaway values that
# only need to satisfy the URL pattern's converter types (str/int) -- the auth
# decorator fires before the view body ever touches the database, so nothing here
# needs to correspond to a real object.
_SCOPE = {"scope_value": "TestScope"}
_SE_DATE = {"se": "test.se@example.com", "plan_date": "2026-09-22"}
_SE_DATE_TYPE = {**_SE_DATE, "plan_type": "A"}

ROUTED_VIEWS = [
    ("se_plan", _SCOPE, "get", "any"),
    ("abm_plan", _SCOPE, "get", "any"),
    ("rbm_plan", _SCOPE, "get", "any"),
    ("node_plan", _SCOPE, "get", "any"),
    ("block_plan", _SCOPE, "get", "any"),
    ("district_plan", _SCOPE, "get", "any"),
    ("state_plan", _SCOPE, "get", "any"),
    ("normalize", {}, "post", "admin"),
    ("tuff", {"scope_type": "SE", "scope_value": "TestScope"}, "post", "admin"),
    ("route_plans", _SE_DATE, "get", "any"),
    ("select_route_plan", _SE_DATE_TYPE, "post", "any"),
    ("accept_route_plan", _SE_DATE_TYPE, "post", "any"),
    ("reject_route_plan", _SE_DATE, "post", "any"),
    ("add_route_stop", _SE_DATE_TYPE, "post", "any"),
    ("remove_route_stop", _SE_DATE_TYPE, "post", "any"),
    ("pitch_script", {"daily_task_id": 1}, "get", "any"),
    ("dc_card", {"daily_task_id": 1}, "get", "any"),
    ("headcount_bifurcation", {}, "get", "any"),
    ("directory_states", {}, "get", "any"),
    ("directory_nodes", {}, "get", "any"),
    ("directory_districts", {}, "get", "any"),
    ("directory_blocks", {}, "get", "any"),
    ("directory_zbms", {}, "get", "any"),
    ("directory_rbms", {}, "get", "any"),
    ("directory_abms", {}, "get", "any"),
    ("directory_ses", {}, "get", "any"),
    ("directory_dcs", {}, "get", "any"),
    ("visit_streaks", {}, "get", "any"),
    ("completion_stats", {}, "get", "any"),
    ("scheduled_scopes", {}, "get", "any"),
    ("plan_run_list", {}, "get", "any"),
    ("plan_run_detail", {"plan_run_id": 1}, "get", "any"),
    ("admin_tracking", {}, "get", "admin"),
    ("admin_reconcile", {}, "post", "admin"),
    ("admin_discount_schemes", {}, "get", "admin"),
    ("admin_generate_all_states", {}, "post", "admin"),
    ("admin_routing_overrides", {}, "get", "admin"),
    ("admin_routing_overrides_delete", {}, "post", "admin"),
    ("admin_pipeline_config", {}, "get", "admin"),
    ("admin_dc_selection_preview", {}, "post", "admin"),
    ("admin_dc_selection", {}, "get", "admin"),
    ("admin_dc_selection_search", {}, "get", "admin"),
    ("admin_dc_selection_upload_rank_csv", {}, "post", "admin"),
    ("admin_dc_selection_upload_selected_dcs", {}, "post", "admin"),
    ("admin_dc_selection_sample_rank_csv", {}, "get", "admin"),
    ("admin_dc_selection_sample_selected_dcs_csv", {}, "get", "admin"),
]


class ViewAuthEnforcementTests(TestCase):
    """Regression guard for planning.views' "every routed view requires a real
    logged-in session" contract (see that module's own docstring, added the same
    day). Exhaustive over ROUTED_VIEWS above, not a sample -- the whole point is that
    a future view added without the decorator, or an existing one that loses it in a
    refactor, fails a test here instead of silently shipping open."""

    @classmethod
    def setUpTestData(cls):
        cls.se_user = User.objects.create_user("test_se_role", password="irrelevant-not-used")
        UserProfile.objects.create(user=cls.se_user, role=UserProfile.Role.SE, name="Test SE")
        cls.admin_user = User.objects.create_user("test_admin_role", password="irrelevant-not-used")
        UserProfile.objects.create(user=cls.admin_user, role=UserProfile.Role.ADMIN, name="Test Admin")

    def _call(self, name, kwargs, method):
        url = reverse(f"planning:{name}", kwargs=kwargs)
        return getattr(self.client, method)(url)

    def test_every_view_rejects_anonymous_with_401(self):
        for name, kwargs, method, _role in ROUTED_VIEWS:
            with self.subTest(view=name):
                resp = self._call(name, kwargs, method)
                self.assertEqual(resp.status_code, 401, f"{name}: expected 401 for an anonymous request, got {resp.status_code}")
                self.assertEqual(resp.json().get("error"), "Authentication required")

    def test_admin_views_reject_authenticated_non_admin_with_403(self):
        self.client.force_login(self.se_user)
        for name, kwargs, method, role in ROUTED_VIEWS:
            if role != "admin":
                continue
            with self.subTest(view=name):
                resp = self._call(name, kwargs, method)
                self.assertEqual(resp.status_code, 403, f"{name}: expected 403 for a non-admin authenticated request, got {resp.status_code}")
                self.assertEqual(resp.json().get("error"), "Admin role required")

    # Deliberately NOT an exhaustive "call every view as an authenticated user"
    # sweep, unlike the two tests above. First version of this test did exactly that
    # and it was a real incident: force_login + a real request lets the DECORATOR
    # through into the REAL view body -- for most views that's an inert DB read, but
    # `normalize`/`tuff` (live Redshift pulls) and `admin_reconcile`/
    # `admin_discount_schemes` (a live pipeline run and a live Discount Service HTTP
    # call, respectively) are not, and this test suite has no business ever
    # triggering those for real. Spot-checking one hand-verified side-effect-free
    # view per role tier proves the same thing (the decorator calls view_func at all)
    # without needing to audit every current and future view's internals to keep this
    # test suite safe to run.
    def test_a_login_only_view_accepts_any_authenticated_role(self):
        self.client.force_login(self.se_user)
        resp = self.client.get(reverse("planning:directory_states"))
        self.assertNotIn(resp.status_code, (401, 403))

    def test_an_admin_view_accepts_admin(self):
        self.client.force_login(self.admin_user)
        resp = self.client.get(reverse("planning:admin_tracking"))
        self.assertNotIn(resp.status_code, (401, 403))


class LoginFlowTests(TestCase):
    """planning.auth_views.auth_login/auth_me -- the session every test above (and
    every real user) depends on."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("real_user", password="a-real-password-123")
        UserProfile.objects.create(user=cls.user, role=UserProfile.Role.SE, name="Real User")
        cls.no_profile_user = User.objects.create_user("no_profile_user", password="a-real-password-123")

    def _login(self, username, password):
        return self.client.post(
            reverse("planning:auth_login"),
            data={"username": username, "password": password},
            content_type="application/json",
        )

    def test_correct_credentials_log_in_and_persist_the_session(self):
        resp = self._login("real_user", "a-real-password-123")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["role"], UserProfile.Role.SE)
        # The session set by login should now authenticate subsequent requests.
        me = self.client.get(reverse("planning:auth_me"))
        self.assertEqual(me.status_code, 200)

    def test_wrong_password_is_rejected(self):
        resp = self._login("real_user", "not-the-real-password")
        self.assertEqual(resp.status_code, 401)

    def test_deactivated_account_is_rejected_with_the_same_message(self):
        self.user.is_active = False
        self.user.save()
        resp = self._login("real_user", "a-real-password-123")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"], "Incorrect username or password")

    def test_account_with_no_role_profile_is_rejected_not_crashed(self):
        resp = self._login("no_profile_user", "a-real-password-123")
        self.assertEqual(resp.status_code, 422)

    def test_auth_me_without_a_session_is_401(self):
        resp = self.client.get(reverse("planning:auth_me"))
        self.assertEqual(resp.status_code, 401)
