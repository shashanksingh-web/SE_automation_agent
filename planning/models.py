from django.db import models


class PlanRun(models.Model):
    """One activation of the SE Daily Task Agent (se_daily_plan_agent.generate_se_daily_plan)
    at a given scope. Mirrors se_daily_plan_agent.py's own Run_Summary shape -- see
    AGENT_OPERATING_PROMPTS.md for what each agent does and does not guarantee."""

    class ScopeType(models.TextChoices):
        SE = "SE", "SE"
        ABM = "ABM", "ABM"
        RBM = "RBM", "RBM"
        NODE = "NODE", "Node"
        BLOCK = "BLOCK", "Block"
        DISTRICT = "DISTRICT", "District"
        STATE = "STATE", "State"

    class Status(models.TextChoices):
        PENDING_REVIEW = "PENDING_REVIEW", "Pending Review"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    scope_type = models.CharField(max_length=10, choices=ScopeType.choices)
    scope_value = models.CharField(max_length=255)
    plan_date = models.DateField()
    run_timestamp = models.DateTimeField(auto_now_add=True)
    metabase_configured = models.BooleanField(default=False)
    se_count = models.IntegerField(default=0)
    dc_count = models.IntegerField(default=0)
    task_count = models.IntegerField(default=0)
    # 7.3/8.5/4.4/4.5 custom-value status at the time of this run -- see
    # [[ask-custom-config-before-planning]] in memory / AGENT_OPERATING_PROMPTS.md Prompt 0.
    dynamic_parameters = models.JSONField(default=dict, blank=True)
    note = models.TextField(blank=True, default="")
    # SEs in scope who got 0 tasks, with why -- e.g. [{"se_id": ..., "se_email": ...,
    # "reason": "No punch-in recorded for this SE today (Section 3a gating signal)"}].
    # generate_se_daily_plan() (se_daily_plan_agent.py) computes this in-memory per SE
    # (attendance gate fail, or no in-scope DC qualified for any objective) but never
    # persisted it before -- an SE with 0 tasks left no DailyTask row and no other trace,
    # so the reason was lost the moment the request finished. Wired 2026-08-08.
    skipped_ses = models.JSONField(default=list, blank=True)

    # Orchestration/lifecycle fields (soft approval gate -- status is an audit trail, not
    # a filter: GET endpoints keep returning runs regardless of status, since no reviewer
    # workflow exists yet to guarantee someone actually approves every run every day).
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING_REVIEW)
    reviewed_by = models.CharField(max_length=255, blank=True, default="")
    reviewed_at = models.DateTimeField(blank=True, null=True)
    error_message = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(blank=True, null=True)
    finished_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-run_timestamp"]
        indexes = [
            models.Index(fields=["scope_type", "scope_value", "plan_date"]),
        ]

    def __str__(self):
        return f"{self.scope_type}:{self.scope_value} @ {self.plan_date} ({self.run_timestamp:%Y-%m-%d %H:%M})"


class DailyTask(models.Model):
    """One row per DC per SE per day -- the doc's Section 10 15-column output shape,
    exactly as produced by se_daily_plan_agent.DailyTaskRow. Persisted flat (not
    normalized further) so the table maps 1:1 onto what the agent already returns."""

    plan_run = models.ForeignKey(PlanRun, related_name="tasks", on_delete=models.CASCADE)

    se_id = models.CharField(max_length=64)
    se_name = models.CharField(max_length=255, blank=True, null=True)
    plan_date = models.DateField()

    # Columns 1-3
    sr_no = models.IntegerField()
    dc_name = models.CharField(max_length=255, blank=True, null=True)
    # Nullable since 2026-08-06 -- a Farmer Meeting task (8.11 Layer 0/1, FM_Urgency)
    # genuinely isn't tied to a specific DC at generation time (Layer 0/1 returns early,
    # before any DC-level candidate logic runs), unlike every other DailyTask row.
    dc_id = models.CharField(max_length=32, blank=True, null=True)
    # Column 4
    distance_km = models.FloatField(blank=True, null=True)
    # Columns 5-7
    recommended_task_type = models.CharField(max_length=32)
    purpose_of_visit = models.CharField(max_length=128, blank=True, default="")
    reason_of_visit = models.CharField(max_length=255, blank=True, default="")
    # Column 8
    last_visit_date = models.DateField(blank=True, null=True)
    days_since_last_visit = models.IntegerField(blank=True, null=True)
    # Columns 9-10
    present_outstanding = models.FloatField(blank=True, null=True)
    present_overdue = models.FloatField(blank=True, null=True)
    # Real aging bucket from dc_datamart's os_1_to_90/os_90_plus split -- not an exact
    # "overdue since <date>"/day-count, since no such field exists in dc_datamart's
    # confirmed schema (checked live 2026-08-06). See DailyTaskRow.Overdue_Aging_Bucket.
    overdue_aging_bucket = models.CharField(max_length=20, blank=True, null=True)
    # dc_datamart.weighted_avg_repayment_days -- a real, live, DC-wide average, not a
    # per-bucket day count (dc_datamart has no field that splits days-overdue by the
    # 0-90/90+ amount buckets). See DailyTaskRow.Avg_Repayment_Days.
    avg_repayment_days = models.FloatField(blank=True, null=True)
    # Columns 11-12
    last_order_date = models.DateField(blank=True, null=True)
    last_order_value = models.FloatField(blank=True, null=True)
    # Column 13
    last_payment_date = models.DateField(blank=True, null=True)
    last_payment_join_key_unconfirmed = models.BooleanField(default=True)
    # Column 14
    ytd_private_label = models.FloatField(blank=True, null=True)
    # Column 15
    dc_club_participation = models.CharField(max_length=64, blank=True, default="")
    # se_daily_plan_agent.normalize_dc_club()'s full per-DC club dict, structured --
    # dc_club_participation above is a one-line prose summary of the exact same data,
    # this is that data broken out so a caller can render current standing (tier/zone/
    # TOD%/reward) and eligibility-if-outstanding-cleared (tier/TOD%/reward) as distinct
    # UI elements instead of parsing prose. {} when club data wasn't available this run
    # (Enrollment_Basis unconfirmed doesn't block this -- see that field's own caveat,
    # recorded once per run via the Club_Enrollment_Flag_Unconfirmed exception).
    club_detail = models.JSONField(default=dict, blank=True)

    # Confirmed 2026-08-18 -- cross-cutting "cover this one first" signal: chronic miss
    # escalation (DCVisitStreak.consecutive_misses >= DCVisitStreak.ESCALATION_THRESHOLD),
    # a real overdue balance aged 90+ days, or credit-on-hold. See DailyTaskRow.Critical.
    critical = models.BooleanField(default=False)
    critical_reasons = models.CharField(max_length=255, blank=True, default="")

    # Not printed columns, kept for traceability/safety (see DailyTaskRow docstring)
    objective = models.CharField(max_length=32, blank=True, default="")
    no_new_orders = models.BooleanField(default=False)
    credit_on_hold = models.BooleanField(default=False)
    credit_on_hold_reason = models.CharField(max_length=128, blank=True, null=True)
    estimated_duration = models.IntegerField(default=0)
    priority_multiplier = models.FloatField(default=1.0)

    # Feedback-loop outcome fields (Tier 1) -- populated later by `reconcile_outcomes`,
    # once plan_date has passed and real Visits/Sales/Payments data for that day exists.
    # UNKNOWN (not COMPLETED/MISSED) is the honest default until reconciliation runs --
    # never guessed at generation time, same honest-degrade pattern as the rest of the
    # pipeline (see se_daily_plan_agent.py's own guardrail language).
    class OutcomeStatus(models.TextChoices):
        UNKNOWN = "UNKNOWN", "Unknown"
        COMPLETED = "COMPLETED", "Completed"
        PARTIAL = "PARTIAL", "Partial"
        MISSED = "MISSED", "Missed"

    outcome_status = models.CharField(max_length=16, choices=OutcomeStatus.choices, default=OutcomeStatus.UNKNOWN)
    actual_visit_date = models.DateField(blank=True, null=True)
    actual_order_value = models.FloatField(blank=True, null=True)
    actual_payment_amount = models.FloatField(blank=True, null=True)
    reconciled_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["plan_run", "se_id", "sr_no"]
        indexes = [
            models.Index(fields=["se_id", "plan_date"]),
            models.Index(fields=["dc_id"]),
        ]

    def __str__(self):
        return f"{self.se_id} #{self.sr_no} {self.dc_name} ({self.objective})"


class ExceptionRecord(models.Model):
    """Exceptions_Report entries tied to a PlanRun -- Section 5/7's guardrail: an
    exceptions report ships on every run, including clean ones, never silently."""

    plan_run = models.ForeignKey(PlanRun, related_name="exceptions", on_delete=models.CASCADE)
    record_id = models.CharField(max_length=128, blank=True, null=True)
    source = models.CharField(max_length=64)
    reason_code = models.CharField(max_length=64)
    detail = models.TextField()
    run_timestamp = models.DateTimeField()

    class Meta:
        ordering = ["plan_run", "reason_code"]

    def __str__(self):
        return f"{self.reason_code} ({self.source})"


class DCVisitStreak(models.Model):
    """Repeat-skip tracking per (SE, DC), independent of any single DailyTask/PlanRun --
    a DC missed 3+ consecutive times gets escalated (priority_multiplier boost) the next
    time it's scored, per `reconcile_outcomes`. Reset to 0 on any COMPLETED/PARTIAL
    outcome for that pair."""

    # Single source of truth for both reconcile_outcomes.py's own escalation-note logic
    # and services.generate_plan_for_scope's Critical flag (confirmed 2026-08-18) -- a
    # module-level constant in reconcile_outcomes.py would create a circular import if
    # services.py imported it back (reconcile_outcomes already imports FROM services.py).
    ESCALATION_THRESHOLD = 3

    se_id = models.CharField(max_length=64)
    dc_id = models.CharField(max_length=32)
    consecutive_misses = models.IntegerField(default=0)
    last_outcome_date = models.DateField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["se_id", "dc_id"], name="unique_se_dc_streak"),
        ]

    def __str__(self):
        return f"{self.se_id}/{self.dc_id}: {self.consecutive_misses} consecutive misses"


class ObjectiveCompletionStats(models.Model):
    """Trailing-30d completion rate per (SE, objective) -- rolled up by
    `compute_completion_stats` from DailyTask.outcome_status, consumed by BO1/BO3 scoring
    as a bounded (0.7x-1.3x) weighting multiplier (Tier 2 adaptive weighting). Neutral
    (1.0x) whenever sample_size is too small to be meaningful -- see
    se_daily_plan_agent.completion_multiplier()."""

    se_id = models.CharField(max_length=64)
    objective = models.CharField(max_length=32)
    completion_rate_30d = models.FloatField()
    sample_size = models.IntegerField()
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["se_id", "objective"], name="unique_se_objective_stats"),
        ]

    def __str__(self):
        return f"{self.se_id}/{self.objective}: {self.completion_rate_30d:.0%} (n={self.sample_size})"


class ScheduledScope(models.Model):
    """A (scope_type, scope_value) pair `run_scheduled_tuff` runs TUFF for once daily via
    cron, independent of any ad-hoc `activate_tuff`/`generate_se_plan` CLI invocation.
    `active=False` lets a scope be paused without deleting its history."""

    scope_type = models.CharField(max_length=10, choices=PlanRun.ScopeType.choices)
    scope_value = models.CharField(max_length=255)
    active = models.BooleanField(default=True)
    last_run_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["scope_type", "scope_value"], name="unique_scheduled_scope"),
        ]

    def __str__(self):
        return f"{self.scope_type}:{self.scope_value} ({'active' if self.active else 'paused'})"


class PitchScript(models.Model):
    """The Pitching Agent's output -- one per DailyTask, generated automatically right
    after task assignment (planning/pitching.py). Config-driven from pitch_config/'s 5
    CSVs (Visit-Purpose taxonomy, Ask/Tell/Wish structure, S1-S8 applicable-data-source
    mapping) -- see AGENT_OPERATING_PROMPTS.md / the pitching-agent memory for the full
    design. purpose_key is the matched Purpose or Purpose-combo (set-matched, since the
    task engine's own combo ordering doesn't always match pitch_config's key ordering)."""

    daily_task = models.OneToOneField(DailyTask, related_name="pitch", on_delete=models.CASCADE)
    purpose_key = models.CharField(max_length=255)
    script_hindi = models.TextField()
    # e.g. ["S5 Outstanding", "S8 YTD PL Sale"] / ["S4 Current Inventory (no DC-level source)"]
    data_sources_used = models.JSONField(default=list, blank=True)
    data_sources_skipped = models.JSONField(default=list, blank=True)
    generated_at = models.DateTimeField(auto_now_add=True)

    # The S1 block-peer top-product recommendation (planning.pitching._format_product_list)
    # -- already computed into script_hindi's own S1 sentence as free text ("...सबसे ज़्यादा
    # बिकने वाले प्रोडक्ट्स: C SQUARE (Brand: GAPL, Sub-category: Insecticide, Segment:
    # PRIVATE LABEL) (₹31,200), ..."), captured here structured too so a caller doesn't
    # have to parse Hindi prose. A list of 0-5 {name, value, category, sub_category,
    # brand, business_segment, scope} dicts, ranked highest-value first -- widened
    # 2026-08-18 from a single top product per direct instruction. scope is "block" or
    # "node" (planning.services' block-first-then-node-fallback within dominant_category)
    # or "nearby_radius"/"nearby_node" (planning.services._attach_nearby_product_
    # recommendations' geographic fallback, used only when this DC's own block+node peers
    # had zero purchase data in its category, or it has no dominant_category at all).
    # Never padded to a fixed count -- a DC with only 2 real candidates just gets 2.
    recommended_products = models.JSONField(default=list, blank=True)

    def __str__(self):
        return f"Pitch for {self.daily_task_id} ({self.purpose_key})"


class DCCard(models.Model):
    """DC Card (Preface) -- "Dehaat Center Ko Jaano" -- planning/dc_card.py's output. A
    second, complementary pre-pitch briefing framework (pitch_config's "DC Card
    (Preface)" CSV), shown to the SE when they open a DC's card, BEFORE the PitchScript's
    own Ask/Tell/Wish. Generated automatically alongside PitchScript, same trigger point
    and per-DC-task cadence (planning.services.generate_plan_for_scope) -- unlike
    FocusProductTargetRun, this isn't opt-in, since every DC-tied task genuinely has a
    DC to brief on.

    Not modeled as 7 separate fields for each CSV sub-point -- collapsed to one field per
    of the 3 CSV sections (matching how the card is actually read/shown, one block at a
    time) plus the single rendered Hindi text. Crop Type/Style (Section 2's DC-first
    "what crop does this DC serve" question) has no field here at all -- see
    data_sources_skipped, which always carries it; there being no source is the finding,
    not a bug to work around with a blank column."""

    daily_task = models.OneToOneField(DailyTask, related_name="dc_card", on_delete=models.CASCADE)

    who_section = models.TextField(blank=True, default="")
    where_dc_stands_section = models.TextField(blank=True, default="")
    private_label_section = models.TextField(blank=True, default="")
    card_hindi = models.TextField()

    # Same recommended-products list as PitchScript's own field of the same name
    # (planning.dc_card._pl_recommendation reuses pitching's _format_product_list, so
    # the two can never name different products for the same DC/category/run) --
    # captured structured here too, not just inside private_label_section's prose. See
    # PitchScript.recommended_products for the full shape/scope-tag docstring.
    recommended_products = models.JSONField(default=list, blank=True)

    # Structured form of who_section's "Business Area Strength" bullet (planning.dc_card.
    # _business_area_detail, added 2026-08-22) -- every sub-category this fiscal-YTD,
    # split Branded vs. Private Label with share% and product-wise detail, paired with
    # the same structure over the prior FY's YTD window where that data exists. {} when
    # this DC has no current-year business-area data at all.
    business_area_detail = models.JSONField(default=dict, blank=True)

    # Structured form of who_section's "Turnover-wise Standing" bullet (planning.dc_card.
    # _turnover_detail, added 2026-08-22) -- last-FY/YTD purchase, club-scheme qualifying
    # turnover, and the YoY PL comparison as separate fields instead of one dense
    # sentence. {} when none of those signals are available this run (same gate as
    # _turnover_standing's own None case).
    turnover_detail = models.JSONField(default=dict, blank=True)

    # se_daily_plan_agent.normalize_dc_club()'s raw per-DC dict, structured (added
    # 2026-08-22) -- same shape/field meaning as DailyTask.club_detail (see that field's
    # docstring), backing this card's "Scheme Standing" bullet instead of DailyTask's
    # DC_Club_Participation. {} when club data wasn't available this run.
    club_detail = models.JSONField(default=dict, blank=True)

    data_sources_used = models.JSONField(default=list, blank=True)
    data_sources_skipped = models.JSONField(default=list, blank=True)
    generated_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"DC Card for {self.daily_task_id}"


class RoutePlan(models.Model):
    """The Routing Agent's output -- one row per algorithm-generated route for one SE on
    one plan_date (Routing_Agent_Configuration_Sheet_v5_FINAL, R5.1: minimum 3 per
    SE/day, from Models 1-3). Sits between PlanRun's Ranked_Pool (built inside
    se_daily_plan_agent.generate_se_daily_plan(), see planning.routing) and DailyTask --
    exactly one RoutePlan per PlanRun/SE is_default_selected=True, and that plan's
    RouteStop rows are what actually get synced into DailyTask, so Pitching Agent /
    reporting / the API never need to know RoutePlan exists (per the Data Norm Agent
    doc: "Pitching Agent and Routing Agent share no output tables at all")."""

    class PlanType(models.TextChoices):
        PRIORITY_MAX = "PRIORITY_MAX", "Priority-Max (Model 1)"
        DISTANCE_MIN = "DISTANCE_MIN", "Distance-Min (Model 2)"
        BALANCED = "BALANCED", "Balanced (Model 3)"
        SE_OVERRIDE = "SE_OVERRIDE", "SE Self-Override (Model 4)"  # not generated yet, reserved
        # Plan B (Beat_Planning_Routing_Agent_Cluster_Model.xlsx, confirmed 2026-08-28) --
        # a genuinely different mode from Models 1-3 above (all "Plan A"), not a 4th
        # variant of the same algorithm: density-balanced territory clustering ->
        # cumulative BO score per cluster -> greedy score-per-km cluster selection under
        # an 80km/180min budget. Generated only when explicitly chosen (see
        # make_routing_plan_asker()) -- the source doc itself states the exact Plan A ->
        # Plan B auto-trigger condition is "not yet confirmed," so this never fires
        # silently as a fallback.
        # 2026-08-31: workbook's own Sheet 7 "3-Route Comparison Summary" (unchanged by
        # today's Conditional Ceiling update, confirmed present in both the old and new
        # workbook) runs Plan B's SAME selection logic 3 times, once per ranking
        # criterion, exactly mirroring Plan A's Models 1-3 -- R5.1's "minimum 3 per
        # SE/day" applies to Plan B too, not just Plan A. CLUSTER_BASED is kept as the
        # Route 1/Efficiency-Balanced label (17 rows already persisted under this name
        # before the 3-route fix) rather than renamed, so no data migration is needed.
        CLUSTER_BASED = "CLUSTER_BASED", "Cluster-Based Efficiency (Plan B, Route 1)"
        CLUSTER_SCOREMAX = "CLUSTER_SCOREMAX", "Cluster-Based Score-Max (Plan B, Route 2)"
        CLUSTER_DISTMIN = "CLUSTER_DISTMIN", "Cluster-Based Distance-Min (Plan B, Route 3)"

    plan_run = models.ForeignKey(PlanRun, related_name="route_plans", on_delete=models.CASCADE)
    se_id = models.CharField(max_length=64)
    se_name = models.CharField(max_length=255, blank=True, null=True)
    plan_date = models.DateField()

    plan_type = models.CharField(max_length=16, choices=PlanType.choices)

    origin_lat = models.FloatField(blank=True, null=True)
    origin_lon = models.FloatField(blank=True, null=True)
    # R0.4: previous working day's punch-in, confirmed -- if none exists yet, the agent
    # waits for today's real punch-in rather than guessing a fallback (see planning.routing).
    ORIGIN_BASIS_CHOICES = [
        ("prev_day_punch_in", "Previous working day's punch-in"),
        # R0.4's "waits for today's real punch-in instead of guessing" case where that
        # punch-in has already actually happened by the time this ran (plan_date's own
        # attendance record exists, even though no prior-day one does) -- a resolved
        # origin, not a deferral, so it gets its own label rather than being folded into
        # either of the other two.
        ("today_punch_in", "No prior-day punch-in, but today's own punch-in already happened"),
        ("waiting_for_today", "No prior punch-in at all yet -- deferred, no plan generated this run"),
    ]
    origin_basis = models.CharField(max_length=20, choices=ORIGIN_BASIS_CHOICES)

    total_distance_km = models.FloatField(blank=True, null=True)  # informational only, R1.5/R1.6 -- never a gate
    total_travel_minutes = models.FloatField(blank=True, null=True)
    total_visit_minutes = models.FloatField(blank=True, null=True)
    total_minutes = models.FloatField(blank=True, null=True)
    priority_score_captured = models.FloatField(blank=True, null=True)

    # Speed/alpha assumption audit trail -- these two ops assumptions feed directly into the numbers
    # above (travel time/priority, and therefore feasibility) but weren't persisted
    # anywhere, so a later re-run with a different assumption was unauditable against
    # this one. avg_speed_kmph_used applies to all 3 models; alpha_used only to BALANCED
    # (Model 3's priority/travel blend weight, see build_route_balanced) and stays null
    # for the other two.
    avg_speed_kmph_used = models.FloatField(blank=True, null=True)
    alpha_used = models.FloatField(blank=True, null=True)

    feasible = models.BooleanField(default=True)
    # e.g. Travel_Floor_Not_Met when even the best model can't reach the 180-min
    # travel floor (R1.2) with a non-trivial stop set (GR-R5, fail-safe not a hard reject).
    infeasibility_reason = models.CharField(max_length=255, blank=True, default="")

    # Model 1 (Priority-Max) is the default auto-selected plan -- matches today's
    # automated/cron behavior (no SE app exists in this repo to make a real choice from
    # yet). planning.management.commands.select_route_plan flips this to a different
    # plan_type and re-syncs DailyTask accordingly (stands in for R5.3's "SE selects").
    is_default_selected = models.BooleanField(default=False)
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["plan_run", "se_id", "plan_type"]
        indexes = [
            models.Index(fields=["plan_run", "se_id"]),
        ]

    def __str__(self):
        return f"{self.se_id} {self.plan_type} @ {self.plan_date} (PlanRun #{self.plan_run_id})"


class RouteStop(models.Model):
    """One ordered stop within a RoutePlan (R6.1's Ordered_Stops). Distance/time are
    per-leg (Distance_From_Prev/Travel_Time_From_Prev), not cumulative -- unlike the
    older single-route DailyTask.distance_km, which stays cumulative-from-origin for
    backward compatibility with existing reporting."""

    route_plan = models.ForeignKey(RoutePlan, related_name="stops", on_delete=models.CASCADE)
    dc_id = models.CharField(max_length=32)
    sequence_no = models.IntegerField()
    # Comma-joined, same convention DailyTask.objective already uses for bundled purposes.
    purposes = models.CharField(max_length=255, blank=True, default="")
    eta_minutes_from_origin = models.FloatField(blank=True, null=True)
    distance_from_prev_km = models.FloatField(blank=True, null=True)
    travel_time_from_prev_min = models.FloatField(blank=True, null=True)
    visit_duration_min = models.FloatField(default=45.0)

    class Meta:
        ordering = ["route_plan", "sequence_no"]

    def __str__(self):
        return f"{self.route_plan_id} #{self.sequence_no} {self.dc_id}"


class FocusProductTargetRun(models.Model):
    """Focus Product Campaign Targeting (Product _cohort/, planning/product_cohort.py)
    result, tied to a PlanRun -- product-first, not DC-first, so unlike RoutePlan/
    PitchScript this isn't generated automatically for every PlanRun. Only created when
    a run is explicitly given a Focus Product (materialId) to evaluate, since no
    confirmed source anywhere in this pipeline says which Focus Products to check by
    default for a given scope/day (see the pitch_config Focus Product Targeting CSV).

    step_2a/2b/3 are stored as opaque JSON, not reshaped into confirmed columns -- Step
    2B/3's response schema was still unconfirmed as of the last live check (2026-08-14),
    and Step 2A's own shape, while confirmed, doesn't warrant its own columns for what's
    fundamentally a pass-through diagnostic result, not a scored/ranked pipeline output
    like DailyTask/RoutePlan."""

    plan_run = models.ForeignKey(PlanRun, related_name="focus_product_targets", on_delete=models.CASCADE)
    material_id = models.CharField(max_length=64)
    node_id = models.CharField(max_length=64)
    step_2a = models.JSONField(blank=True, null=True)
    step_2b = models.JSONField(blank=True, null=True)
    step_3 = models.JSONField(blank=True, null=True)
    generated_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"FocusProduct {self.material_id}@{self.node_id} (PlanRun #{self.plan_run_id})"


class RouteDroppedDC(models.Model):
    """A DC from the Ranked_Pool that didn't make it into a given RoutePlan (R6.1's
    Dropped_DCs) -- e.g. Geo_Incomplete (GR-R1), Legal_Hold (GR-R2), or
    Capacity_Exceeded (couldn't fit within the 420-min-total/5-task caps -- R1.2's
    180-min figure is a travel FLOOR, not a capacity ceiling, so it never drops a DC by
    itself; it shows up as a plan-level Travel_Floor_Not_Met instead, see RoutePlan).
    Never a silent omission -- every candidate the Ranked_Pool offered either appears in
    RouteStop or here, with why."""

    route_plan = models.ForeignKey(RoutePlan, related_name="dropped_dcs", on_delete=models.CASCADE)
    dc_id = models.CharField(max_length=32)
    reason = models.CharField(max_length=255)

    def __str__(self):
        return f"{self.route_plan_id} dropped {self.dc_id}: {self.reason}"


class BeatZoneAssignment(models.Model):
    """Beat_Planning_Routing_Agent_Cluster_Model.xlsx, Sheet 11 Model B (Fixed Rotation):
    a stable, persisted DC -> zone_index mapping per SE, drawn from geography/density
    (not daily scores, per the sheet's own wording) -- the missing piece Repeat-Avoidance
    (Model A, see planning.routing._cooling_down_dc_ids) can't provide: a DC that never
    wins the daily ranking has zero cooldown history to block, so it can go permanently
    unvisited under Model A alone (confirmed live, 2026-09-01 -- several DCs on a real
    SE's Ranked_Pool were dropped in 5/5 consecutive Plan B runs). Zones are recomputed
    only when explicitly asked (opt-in --enable-rotation / ?rotation=true), never
    silently reshuffled, since a changing rotation would break the whole point of a
    predictable cadence -- see planning.routing._get_or_assign_zone."""

    se_id = models.CharField(max_length=64)
    dc_id = models.CharField(max_length=32)
    zone_index = models.IntegerField()
    # Denormalized (not looked up live from today's candidate pool) so a newly-appearing
    # DC can be assigned to its nearest EXISTING zone centroid even on a day when most of
    # that zone's other members aren't in the current Ranked_Pool at all.
    latitude = models.FloatField()
    longitude = models.FloatField()
    # Redundant across every row for the same se_id (by design, not normalized into a
    # separate config table) -- num_zones/anchor_date together define "which zone is on
    # today" (zone_index_today = (plan_date - anchor_date).days % num_zones), and every
    # row needs both to be self-sufficient for that lookup without a second query.
    num_zones = models.IntegerField()
    anchor_date = models.DateField()
    computed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("se_id", "dc_id")]

    def __str__(self):
        return f"{self.se_id}:{self.dc_id} -> zone {self.zone_index}/{self.num_zones}"
