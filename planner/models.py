from datetime import date

from django.db import models

from ras_engine import BiofilterParams, Feed as EngineFeed, Params, PlanParams, RasStage
from ras_engine.equipment import CATEGORIES, CATEGORY_UNIT
from ras_engine.params import cast

MAX_HORIZON_DAYS = 1096  # plans and the Gantt chart cover at most 3 years (3 × 365.25 d)
HORIZON_UNITS = {"days": 1, "weeks": 7, "months": 30.4375, "years": 365.25}

DEFAULT_PLAN = {
    "start_date": "2026-12-01",
    "interval_days": 30,
    "horizon_days": 1096,
    "stages": [
        {"name": "Fingerling", "max_weight_g": 5, "n_tanks": 8, "retention_min": 30},
        {"name": "Grow-out", "max_weight_g": 300, "n_tanks": 8, "retention_min": 30},
        {"name": "Large trout", "max_weight_g": 3000, "n_tanks": 8, "retention_min": 30},
    ],
}


class Feed(models.Model):
    name = models.CharField("Feed name", max_length=100)
    min_weight_g = models.FloatField("Min. weight (g)")
    max_weight_g = models.FloatField("Max. weight (g)")
    protein_pct = models.FloatField("Protein (%)")
    fcr = models.FloatField("FCR")

    class Meta:
        ordering = ["min_weight_g"]
        verbose_name = "Feed"
        verbose_name_plural = "Feeds"

    def __str__(self):
        return f"{self.name} ({self.min_weight_g:g}–{self.max_weight_g:g} g)"

    def to_engine(self) -> EngineFeed:
        return EngineFeed(self.name, self.min_weight_g, self.max_weight_g,
                          self.protein_pct / 100, self.fcr)


class EquipmentStandard(models.Model):
    """Industry-standard equipment the user works with (shared by all scenarios)."""
    category = models.CharField("Category", max_length=20, choices=[(k, label) for k, label, _ in CATEGORIES])
    name = models.CharField("Name", max_length=120)
    capacity = models.FloatField("Capacity")
    unit_price = models.FloatField("Unit price", null=True, blank=True)
    note = models.CharField("Note", max_length=200, blank=True)

    class Meta:
        ordering = ["category", "capacity"]
        verbose_name = "Equipment standard"
        verbose_name_plural = "Equipment standards"

    def __str__(self):
        return f"{self.name} ({self.capacity:g} {self.unit})"

    @property
    def unit(self):
        return CATEGORY_UNIT[self.category]


class ProcessTemplate(models.Model):
    """A named process diagram the user saved, to load into any RAS unit of any scenario.
    `inputs` stores the manual overrides (feed, temperature, flow) that go with the diagram."""
    name = models.CharField("Name", max_length=120, unique=True)
    diagram = models.JSONField("Diagram", default=dict)
    source = models.CharField("Saved from", max_length=200, blank=True)
    # manual inputs: {"feed_kgd": 45.0, "temp_c": 14.0, "flow_m3h": 120, "feeding_hours": 8,
    #                 "hourly_feed": [{"hour":8,"kg":5},...]}
    inputs = models.JSONField("Manual inputs", default=dict, blank=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated"]
        verbose_name = "Saved process diagram"
        verbose_name_plural = "Saved process diagrams"

    def __str__(self):
        return self.name


class Scenario(models.Model):
    name = models.CharField("Scenario name", max_length=100)
    params = models.JSONField("Constants", default=dict)
    biofilter = models.JSONField("Biofilter constants", default=dict)
    temperature_steps = models.CharField(
        "Temperature profile (day:°C)", max_length=500, blank=True,
        help_text="E.g. 1:12, 60:13, 120:14. Leave empty to use a constant temperature.")
    use_feed_catalog = models.BooleanField("Use the feed table", default=True)
    production = models.JSONField("Production plan", default=dict)
    equipment = models.JSONField("Equipment selection", default=dict)
    capex = models.JSONField("CAPEX", default=dict)
    process = models.JSONField("Process diagrams", default=dict)  # RAS index -> diagram
    opex = models.JSONField("OPEX inputs", default=dict)
    brief = models.JSONField("Project brief", default=dict)  # the customer's target and the site
    # blob schema stamp, new rows start at the current version — see planner/schema.py
    # (migration 0016 seeds existing rows with 1 so upgrade() migrates them on first load)
    schema_version = models.PositiveSmallIntegerField(default=2)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated"]
        verbose_name = "Scenario"
        verbose_name_plural = "Scenarios"

    def __str__(self):
        return self.name

    def engine_params(self) -> Params:
        return Params.from_flat(self.params)

    def plan_settings(self) -> dict:
        """Production plan settings (defaults when nothing is saved)."""
        p = self.production or {}
        return {
            "start_date": p.get("start_date", DEFAULT_PLAN["start_date"]),
            "interval_days": int(p.get("interval_days", DEFAULT_PLAN["interval_days"])),
            "horizon_days": min(int(p.get("horizon_days", DEFAULT_PLAN["horizon_days"])), MAX_HORIZON_DAYS),
            # what the user typed, e.g. 18 months; older plans only have days
            "horizon_value": p.get("horizon_value", p.get("horizon_days", 3) if "horizon_days" in p else 3),
            "horizon_unit": p.get("horizon_unit", "days" if "horizon_days" in p else "years"),
            "stages": p.get("stages") or DEFAULT_PLAN["stages"],
            # days a tank stays empty (cleaned, dry) after a batch leaves, before the next batch; 0 = same day
            "tank_rest_days": int(p.get("tank_rest_days", 0) or 0),
        }

    def engine_plan(self) -> tuple[PlanParams, list[RasStage]]:
        s = self.plan_settings()
        plan = PlanParams(date.fromisoformat(s["start_date"]), s["interval_days"], s["horizon_days"],
                          params=Params.from_flat(self.params))
        stages = []
        for st in s["stages"]:
            ov = st.get("overrides") or {}
            params = Params.from_flat({**self.params, **{k: v for k, v in ov.items()
                                                         if not k.startswith("bio.")}})
            bio = self.engine_biofilter()
            for k, v in ov.items():
                if k.startswith("bio.") and hasattr(bio, k[4:]):
                    setattr(bio, k[4:], cast(getattr(BiofilterParams(), k[4:]), v))
            stages.append(RasStage(st["name"], float(st["max_weight_g"]), int(st["n_tanks"]),
                                   float(st["retention_min"]), params=params, biofilter=bio,
                                   uid=st.get("id") or str(len(stages))))
        return plan, stages

    # --- per RAS unit storage, keyed by the unit's stable id (older data: by its position)
    def unit_ids(self) -> list[str]:
        return [st.get("id") or str(i) for i, st in enumerate(self.plan_settings()["stages"])]

    def unit_id(self, index: int) -> str:
        ids = self.unit_ids()
        return ids[index] if 0 <= index < len(ids) else str(index)

    def diagram_of(self, index: int) -> dict | None:
        return (self.process or {}).get(self.unit_id(index))

    def set_diagram(self, index: int, diagram: dict | None) -> None:
        uid = self.unit_id(index)
        rest = {k: v for k, v in (self.process or {}).items() if k != uid}
        self.process = {**rest, uid: diagram} if diagram is not None else rest

    def equipment_of(self, index: int) -> dict:
        return ((self.equipment or {}).get("stages") or {}).get(self.unit_id(index), {})

    def engine_biofilter(self) -> BiofilterParams:
        bp = BiofilterParams()
        for key, value in self.biofilter.items():
            if hasattr(bp, key) and value not in (None, ""):
                setattr(bp, key, cast(getattr(BiofilterParams(), key), value))
        return bp


class CompanyProfile(models.Model):
    """The company that issues proposals (one row)."""
    name = models.CharField("Company name", max_length=200, default="Suderra AS")
    address = models.TextField("Address", blank=True)
    country = models.CharField("Country", max_length=80, default="Norway")
    org_no = models.CharField("Organisation number", max_length=60, blank=True)
    vat_no = models.CharField("VAT number", max_length=60, blank=True)
    phone = models.CharField("Phone", max_length=80, blank=True)
    email = models.CharField("E-mail", max_length=120, blank=True)
    website = models.CharField("Website", max_length=120, blank=True)
    signatory = models.CharField("Signed by", max_length=120, blank=True)
    signatory_title = models.CharField("Title", max_length=120, blank=True)
    logo = models.TextField("Logo (data URI)", blank=True)

    class Meta:
        verbose_name = "Company profile"

    def __str__(self):
        return self.name

    @classmethod
    def get(cls):
        return cls.objects.first() or cls.objects.create()


class AgentSpec(models.Model):
    """One army agent's editable context (role prompt). Seeded from planner.agents.DEFAULT_AGENTS;
    edit on the Library · Agents page - the orchestrator only dispatches enabled specialists."""
    name = models.SlugField(unique=True)
    label = models.CharField("Label", max_length=60)
    role = models.TextField("Context / role prompt")
    enabled = models.BooleanField(default=True)

    class Meta:
        verbose_name = "agent"

    def __str__(self):
        return f"{self.label} ({self.name})"


class Proposal(models.Model):
    scenario = models.ForeignKey(Scenario, null=True, blank=True, on_delete=models.SET_NULL, related_name="proposals")
    number = models.CharField("Proposal no.", max_length=40)
    title = models.CharField("Project title", max_length=250)
    date = models.DateField("Date")
    valid_until = models.DateField("Valid until", null=True, blank=True)
    currency = models.CharField("Currency", max_length=8, default="EUR")
    vat_pct = models.FloatField("VAT %", null=True, blank=True)
    # customer: entered per proposal
    customer_name = models.CharField("Customer", max_length=250, blank=True)
    customer_address = models.TextField("Address", blank=True)
    customer_contact = models.CharField("Contact person", max_length=150, blank=True)
    customer_email = models.CharField("E-mail", max_length=150, blank=True)
    customer_phone = models.CharField("Phone", max_length=80, blank=True)
    customer_reg = models.CharField("Registration / tax no.", max_length=150, blank=True)
    # texts (blank intro = written from the scenario)
    intro = models.TextField("Introduction", blank=True)
    terms = models.TextField("Terms", blank=True)
    payment = models.TextField("Payment terms", blank=True)
    sizing_note = models.TextField("System sizing note", blank=True)
    work_packages = models.JSONField("Work packages", default=list)  # [{name, notes, price}]
    lump_sum = models.FloatField("Total design fee", null=True, blank=True)
    clauses = models.JSONField("General conditions", default=list)  # [{title, text}]
    sections = models.JSONField("Sections included", default=dict)   # key -> bool
    # the figures the generated PDF showed, kept so the proposal outlives its scenario
    snapshot = models.JSONField("Figures snapshot of the last PDF", null=True, blank=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated"]
        verbose_name = "Proposal"
        verbose_name_plural = "Proposals"

    def __str__(self):
        return f"{self.number} · {self.title}"


class AISettings(models.Model):
    """API keys for text generation (one row). Stored in this local database only."""
    provider = models.CharField("Default provider", max_length=10, default="claude",
                                choices=[("claude", "Claude (Anthropic)"), ("openai", "OpenAI"), ("zai", "Z.ai (GLM)")])
    claude_key = models.CharField("Claude API key", max_length=300, blank=True)
    claude_model = models.CharField("Claude model", max_length=80, blank=True)
    openai_key = models.CharField("OpenAI API key", max_length=300, blank=True)
    openai_model = models.CharField("OpenAI model", max_length=80, blank=True)
    zai_key = models.CharField("Z.ai API key", max_length=300, blank=True)
    zai_model = models.CharField("Z.ai model", max_length=80, blank=True)

    class Meta:
        verbose_name = "AI settings"

    @classmethod
    def get(cls):
        return cls.objects.first() or cls.objects.create()

    def key(self, provider):
        return getattr(self, f"{provider}_key", "")

    def model(self, provider):
        return getattr(self, f"{provider}_model", "")

    def configured(self):
        return [p for p in ("claude", "openai", "zai") if self.key(p)]
