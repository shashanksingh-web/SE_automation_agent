from django.urls import path

from . import auth_views, views

app_name = "planning"

urlpatterns = [
    path("auth/login/", auth_views.auth_login, name="auth_login"),
    path("auth/logout/", auth_views.auth_logout, name="auth_logout"),
    path("auth/me/", auth_views.auth_me, name="auth_me"),
    path("auth/change-password/", auth_views.auth_change_password, name="auth_change_password"),
    path("admin/users/", auth_views.admin_users_list, name="admin_users_list"),
    path("admin/users/create/", auth_views.admin_users_create, name="admin_users_create"),
    path("admin/users/<int:user_id>/set-active/", auth_views.admin_users_set_active, name="admin_users_set_active"),
    path("admin/users/<int:user_id>/reset-password/", auth_views.admin_users_reset_password, name="admin_users_reset_password"),
    # Per-module version prefix (added 2026-09-18, explicit user request to modularize
    # the 7 scope endpoints "in their respective API" -- each module's own /v1/ segment
    # so SE's contract can move to /se/v2/ independently of ABM/District/etc. ever
    # needing to, rather than one shared version number forcing every module to bump
    # together. Same 7 view functions/shared _scope_view dispatch underneath -- this is
    # a routing change only, no logic duplicated across modules.
    path("se/v1/<str:scope_value>/", views.se_plan, name="se_plan"),
    path("abm/v1/<str:scope_value>/", views.abm_plan, name="abm_plan"),
    path("rbm/v1/<str:scope_value>/", views.rbm_plan, name="rbm_plan"),
    path("node/v1/<str:scope_value>/", views.node_plan, name="node_plan"),
    path("block/v1/<str:scope_value>/", views.block_plan, name="block_plan"),
    path("district/v1/<str:scope_value>/", views.district_plan, name="district_plan"),
    path("state/v1/<str:scope_value>/", views.state_plan, name="state_plan"),
    path("normalize/", views.normalize, name="normalize"),
    path("tuff/<str:scope_type>/<str:scope_value>/", views.tuff, name="tuff"),
    path("routes/<str:se>/<str:plan_date>/", views.route_plans, name="route_plans"),
    path("routes/<str:se>/<str:plan_date>/select/<str:plan_type>/", views.select_route_plan_view, name="select_route_plan"),
    path("routes/<str:se>/<str:plan_date>/accept/<str:plan_type>/", views.accept_route_plan_view, name="accept_route_plan"),
    path("routes/<str:se>/<str:plan_date>/reject/", views.reject_route_plan_view, name="reject_route_plan"),
    path("routes/<str:se>/<str:plan_date>/<str:plan_type>/stops/add/", views.add_route_stop_view, name="add_route_stop"),
    path("routes/<str:se>/<str:plan_date>/<str:plan_type>/stops/remove/", views.remove_route_stop_view, name="remove_route_stop"),
    path("pitch/<int:daily_task_id>/", views.pitch_script, name="pitch_script"),
    path("pitch/<int:daily_task_id>/audio/", views.pitch_audio, name="pitch_audio"),
    path("pitch-dc-card/<int:daily_task_id>/generate/", views.generate_pitch_and_dc_card, name="generate_pitch_and_dc_card"),
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
    path("admin/generate-all-states/", views.admin_generate_all_states, name="admin_generate_all_states"),
    path("admin/routing-overrides/", views.admin_routing_overrides, name="admin_routing_overrides"),
    path("admin/routing-overrides/delete/", views.admin_routing_overrides_delete, name="admin_routing_overrides_delete"),
    path("admin/config/", views.admin_pipeline_config, name="admin_pipeline_config"),
    path("admin/tracking/", views.admin_tracking, name="admin_tracking"),
    path("admin/reconcile/", views.admin_reconcile, name="admin_reconcile"),
    path("admin/discount-schemes/", views.admin_discount_schemes, name="admin_discount_schemes"),
    path("admin/dc-selection/", views.admin_dc_selection, name="admin_dc_selection"),
    path("admin/dc-selection/preview/", views.admin_dc_selection_preview, name="admin_dc_selection_preview"),
    path("admin/dc-selection/search/", views.admin_dc_selection_search, name="admin_dc_selection_search"),
    path("admin/dc-selection/upload-rank-csv/", views.admin_dc_selection_upload_rank_csv, name="admin_dc_selection_upload_rank_csv"),
    path("admin/dc-selection/upload-selected-dcs/", views.admin_dc_selection_upload_selected_dcs, name="admin_dc_selection_upload_selected_dcs"),
    path("admin/dc-selection/sample-rank-csv/", views.admin_dc_selection_sample_rank_csv, name="admin_dc_selection_sample_rank_csv"),
    path("admin/dc-selection/sample-selected-dcs-csv/", views.admin_dc_selection_sample_selected_dcs_csv, name="admin_dc_selection_sample_selected_dcs_csv"),
]
