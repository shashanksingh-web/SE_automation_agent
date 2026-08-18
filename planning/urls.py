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
    path("dc-card/<int:daily_task_id>/", views.dc_card, name="dc_card"),
    path("headcount/", views.headcount_bifurcation, name="headcount_bifurcation"),
    path("directory/states/", views.directory_states, name="directory_states"),
    path("directory/nodes/", views.directory_nodes, name="directory_nodes"),
    path("directory/districts/", views.directory_districts, name="directory_districts"),
    path("directory/blocks/", views.directory_blocks, name="directory_blocks"),
    path("directory/zbms/", views.directory_zbms, name="directory_zbms"),
    path("directory/rbms/", views.directory_rbms, name="directory_rbms"),
    path("directory/abms/", views.directory_abms, name="directory_abms"),
    path("directory/ses/", views.directory_ses, name="directory_ses"),
    path("directory/dcs/", views.directory_dcs, name="directory_dcs"),
    path("streaks/", views.visit_streaks, name="visit_streaks"),
    path("completion-stats/", views.completion_stats, name="completion_stats"),
    path("scheduled-scopes/", views.scheduled_scopes, name="scheduled_scopes"),
    path("runs/", views.plan_run_list, name="plan_run_list"),
    path("runs/<int:plan_run_id>/", views.plan_run_detail, name="plan_run_detail"),
]
