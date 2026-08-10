from django.contrib import admin

from .models import DailyTask, ExceptionRecord, PlanRun


class DailyTaskInline(admin.TabularInline):
    model = DailyTask
    extra = 0
    fields = ("se_id", "sr_no", "dc_name", "objective", "recommended_task_type", "distance_km", "no_new_orders", "credit_on_hold")
    readonly_fields = fields
    can_delete = False


class ExceptionRecordInline(admin.TabularInline):
    model = ExceptionRecord
    extra = 0
    fields = ("source", "reason_code", "detail")
    readonly_fields = fields
    can_delete = False


@admin.register(PlanRun)
class PlanRunAdmin(admin.ModelAdmin):
    list_display = ("id", "scope_type", "scope_value", "plan_date", "run_timestamp", "metabase_configured", "se_count", "dc_count", "task_count")
    list_filter = ("scope_type", "metabase_configured", "plan_date")
    search_fields = ("scope_value",)
    inlines = [DailyTaskInline, ExceptionRecordInline]


@admin.register(DailyTask)
class DailyTaskAdmin(admin.ModelAdmin):
    list_display = ("plan_run", "se_id", "sr_no", "dc_name", "objective", "recommended_task_type", "no_new_orders", "credit_on_hold")
    list_filter = ("objective", "recommended_task_type", "no_new_orders", "credit_on_hold")
    search_fields = ("se_id", "dc_id", "dc_name")
