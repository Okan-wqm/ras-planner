"""Workflow rail: the nine design steps and the library, on every page."""
from django.urls import NoReverseMatch, reverse

# (number, label, url name taking the scenario pk or None, url names that light the step, hint)
STEPS = [
    ("01", "Brief", "brief", {"brief"}, "What the customer wants: batches, fish, weight, site and water"),
    ("02", "Production plan", "production", {"production"},
     "Growth, batches over time, harvests"),
    ("03", "Units & tanks", "units", {"units"}, "RAS units, tank sizes and grading"),
    ("04", "Requirements", "requirements", {"requirements"}, "Peak loads, required flows and treatment targets"),
    ("05", "Process design", "process", {"process"}, "Where the water goes: flows, treatment, performance"),
    ("06", "Equipment", "equipment", {"equipment"}, "Equipment for the drawn process"),
    ("07", "CAPEX", "capex", {"capex"}, "Investment"),
    ("08", "OPEX", "opex", {"opex"}, "Operating cost per year"),
    ("09", "Proposal", "proposals", {"proposals", "proposal_edit"}, "Client proposal and PDF"),
]
LIBRARY = [
    ("Feed table", "feeds", {"feeds"}),
    ("Standards", "standards", {"standards"}),
    ("Company profile", "company", {"company"}),
    ("AI settings", "ai_settings", {"ai_settings"}),
    ("Jet oxygenator", "jet_oxygenator", {"jet_oxygenator"}),
    ("Export credit (Eksfin)", "loan_calculator", {"loan_calculator"}),
]


def _scenario_pk(request):
    match = getattr(request, "resolver_match", None)
    if match is None:
        return None
    if "pk" in match.kwargs:
        return match.kwargs["pk"]
    if "pid" in match.kwargs:  # proposal pages belong to their scenario
        from .models import Proposal
        return Proposal.objects.filter(pk=match.kwargs["pid"]).values_list("scenario_id", flat=True).first()
    from .models import Scenario
    return Scenario.objects.values_list("pk", flat=True).first()


def workflow(request):
    match = getattr(request, "resolver_match", None)
    current = match.url_name if match else None
    pk = _scenario_pk(request)
    steps = []
    for n, label, name, lit, hint in STEPS:
        url = None
        if name:
            try:
                url = reverse(name, args=[pk]) if name not in ("proposals",) else reverse(name)
            except NoReverseMatch:
                url = None
        steps.append({"n": n, "label": label, "url": url, "on": current in lit, "hint": hint, "status": ""})
    if pk and current not in ("process_snapshot",):
        from .models import Scenario
        from .services import step_status
        sc = Scenario.objects.filter(pk=pk).first()
        if sc:
            status = step_status(sc)
            for st in steps:
                st["status"] = status.get(st["n"], "")
    library = [{"label": label, "url": reverse(name), "on": current in lit} for label, name, lit in LIBRARY]
    if pk:
        library += [{"label": "All assumptions", "url": reverse("scenario_edit", args=[pk]),
                     "on": current in ("scenario_edit", "scenario_new")},
                    {"label": "Biofilter calculator", "url": reverse("biofilter", args=[pk]), "on": current == "biofilter"},
                    {"label": "Agents", "url": reverse("agents_page"), "on": current == "agents_page"},
                    {"label": "Single batch (Excel check)", "url": reverse("dashboard", args=[pk]),
                     "on": current in ("dashboard", "dashboard_home")}]
    # previous / next step around the current one
    idx = next((i for i, st in enumerate(steps) if st["on"]), None)
    prev_step = next_step = None
    if idx is not None and current not in ("dashboard", "dashboard_home"):
        prev_step = steps[idx - 1] if idx > 0 else None
        next_step = steps[idx + 1] if idx + 1 < len(steps) else None
    # agent activity: what the army did on the last mission (visible in the workflow)
    agent_log = None
    if pk:
        from planner.models import Scenario as _Sc
        _sc = _Sc.objects.filter(pk=pk).first()
        if _sc and (_sc.opex or {}).get("agent_log"):
            agent_log = _sc.opex["agent_log"]
    from .views_ai import ai_panel_config
    return {"workflow_steps": steps, "library_links": library, "step_prev": prev_step,
            "step_next": next_step, "ai_cfg": ai_panel_config(), "agent_log": agent_log}
