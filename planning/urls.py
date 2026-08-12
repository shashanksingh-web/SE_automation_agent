from django.urls import path

from . import views

app_name = "planning"

urlpatterns = [
    path("se/<str:scope_value>/", views.se_plan, name="se_plan"),
    path("abm/<str:scope_value>/", views.abm_plan, name="abm_plan"),
    path("rbm/<str:scope_value>/", views.rbm_plan, name="rbm_plan"),
    path("node/<str:scope_value>/", views.node_plan, name="node_plan"),
    path("block/<str:scope_value>/", views.block_plan, name="block_plan"),
    path("district/<str:scope_value>/", views.district_plan, name="district_plan"),
    path("state/<str:scope_value>/", views.state_plan, name="state_plan"),
    path("normalize/", views.normalize, name="normalize"),
    path("tuff/<str:scope_type>/<str:scope_value>/", views.tuff, name="tuff"),
    path("routes/<str:se>/<str:plan_date>/", views.route_plans, name="route_plans"),
    path("routes/<str:se>/<str:plan_date>/select/<str:plan_type>/", views.select_route_plan_view, name="select_route_plan"),
    path("pitch/<int:daily_task_id>/", views.pitch_script, name="pitch_script"),
    path("headcount/", views.headcount_bifurcation, name="headcount_bifurcation"),
    path("runs/", views.plan_run_list, name="plan_run_list"),
    path("runs/<int:plan_run_id>/", views.plan_run_detail, name="plan_run_detail"),
]
