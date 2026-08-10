from django.urls import path

from . import views

app_name = "planning"

urlpatterns = [
    path("se/<str:scope_value>/", views.se_plan, name="se_plan"),
    path("abm/<str:scope_value>/", views.abm_plan, name="abm_plan"),
    path("node/<str:scope_value>/", views.node_plan, name="node_plan"),
    path("block/<str:scope_value>/", views.block_plan, name="block_plan"),
    path("district/<str:scope_value>/", views.district_plan, name="district_plan"),
    path("state/<str:scope_value>/", views.state_plan, name="state_plan"),
    path("runs/", views.plan_run_list, name="plan_run_list"),
    path("runs/<int:plan_run_id>/", views.plan_run_detail, name="plan_run_detail"),
]
