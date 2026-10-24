"""Library calculators that stand on their own (not tied to a scenario)."""

from django.shortcuts import render

from ras_engine import jet

from .costing import _capex_data
from .models import Scenario
from .parse import _num
from .services import plan_result

# (name, label, unit, default, help) — the fields of the jet oxygenator form, in display groups
JET_FIELDS = [
    ("Water and site", [
        ("flow_m3h", "Design (highest) flow", "m³/h", 100, ""),
        ("flow_min_m3h", "Lowest flow", "m³/h", None, "optional: gate valve table for a flow range"),
        ("head_m", "Head", "m", 1.0, "feed tank water level above the outlet water level"),
        ("tank_margin_m", "Feed tank rise above the head", "m", 0.0, "pumped feed only"),
        ("water_depth_m", "Water depth in the tank", "m", None, "below the tank water level; checks that the box fits"),
        ("floor_clear_m", "Clearance under the skirt", "m", 0.30, "free water between the skirt and the tank floor"),
        ("plate_below_m", "Plate below the tank water level", "m", 0.10, "the whole gas cushion stays inside the tank water"),
        ("temp_c", "Temperature", "°C", 14.0, ""),
        ("salinity", "Salinity", "g/kg", 0.0, "fresh 0, sea ~33–35"),
        ("altitude_m", "Site altitude", "m", 0.0, "lowers the air pressure, the saturations and the gas pressure"),
    ]),
    ("Gases", [
        ("o2_in", "Inlet O₂", "mg/L", 8.0, ""),
        ("n2_in_pct", "Inlet N₂ saturation", "%", 100.0, ""),
        ("targets", "Target outlet O₂", "mg/L", None, "e.g. 14 16 18; empty = a range"),
        ("o2_purity_pct", "Feed gas O₂", "%", 95.0, "generator ~93–95, liquid oxygen ~99.5"),
        ("E", "Measured compartment efficiency E", "0–1", None, "optional: from a prototype; no corrections"),
        ("low_head_exp", "NTU exponent below 0.8 m head", "", 3.0, "3 = cautious"),
    ]),
    ("Platform", [
        ("compartments", "Gas compartments", "", 4, ""),
        ("nozzle_mm", "Nozzle diameter", "mm", None, "empty = chosen automatically"),
        ("cd", "Discharge coefficient Cd", "", None, "optional: overrides the nozzle type"),
        ("cushion_m", "Gas cushion height", "m", 0.20, ""),
        ("baffle_m", "Compartment baffles below the inner surface", "m", None, "empty = 1.5 × bubble cloud (min. 0.45)"),
        ("skirt_m", "Skirt below the inner surface", "m", None, "empty = 2 × bubble cloud (min. 0.60)"),
        ("spare_pct", "Spare holes", "%", 10.0, ""),
        ("pump_eff", "Pump efficiency (if the head is pumped)", "0–1", 0.45, ""),
    ]),
]



def jet_oxygenator(request):
    data = request.GET
    values, kwargs, error = {}, {}, None
    for _group, fields in JET_FIELDS:
        for name, _label, _unit, default, _help in fields:
            raw = data.get(name, "")
            values[name] = raw if raw != "" else ("" if default is None else default)
            if name == "targets":
                ts = [_num(t) for t in raw.replace(",", " ").split()] if raw else []
                kwargs["targets"] = [t for t in ts if t is not None] or None
            elif name == "compartments":
                kwargs[name] = int(_num(raw) or default)
            else:
                kwargs[name] = _num(raw) if raw != "" else default
    shape = data.get("shape", "rect")
    kwargs["shape"] = shape if shape in jet.SHAPES else "rect"
    nozzle_type = data.get("nozzle_type", "rounded")
    kwargs["nozzle_type"] = nozzle_type if nozzle_type in jet.NOZZLE_TYPES else "rounded"
    result = None
    if "flow_m3h" in data:
        try:
            result = jet.design(**kwargs)
        except jet.JetInputError as exc:
            error = str(exc)
    groups = [(g, [{"name": n, "label": lab, "unit": u, "value": values[n], "help": h} for n, lab, u, _d, h in fs])
              for g, fs in JET_FIELDS]
    return render(request, "planner/jet.html", {
        "groups": groups, "nozzle_type": kwargs["nozzle_type"], "shape": kwargs["shape"], "shapes": jet.SHAPES.items(), "nozzle_labels": jet.NOZZLE_LABELS.items(),
        "r": result, "error": error, "scenarios": Scenario.objects.all(),
        "scenario": Scenario.objects.first()})


# ---------- export credit (Eksfin) loan ----------

LOAN_FIELDS = [
    ("Project", [
        ("project_total", "Project total (contract value)", "", None, "empty = this scenario's CAPEX"),
        ("down_pct", "Down payment", "%", 15.0, "OECD export credits: at least 15 % paid by the buyer"),
    ]),
    ("Loan", [
        ("rate_pct", "Interest rate", "% / year", 6.0, "CIRR fixed rate or floating + margin"),
        ("years", "Repayment period", "years", 10.0, "after the grace period"),
        ("grace_months", "Grace period (interest only)", "months", 0, "e.g. the construction time"),
    ]),
    ("Fees", [
        ("fee_pct", "Arrangement fee", "% of loan", 0.0, "bank's one-off fee"),
        ("premium_pct", "Eksfin risk premium", "% of loan", 0.0, "one-off; depends on country and buyer risk"),
    ]),
]


def _scenario_capex(sc):
    if sc is None:
        return None, "EUR"
    got, err = plan_result(sc)
    if err or got is None:
        return None, (sc.capex or {}).get("currency", "EUR")
    c = _capex_data(sc, got[1])
    return c["grand_total"], c["currency"]


def loan_calculator(request):
    from ras_engine import finance
    data = request.GET
    sc = None
    if str(data.get("scenario", "")).isdigit():
        sc = Scenario.objects.filter(pk=int(data["scenario"])).first()
    sc = sc or Scenario.objects.first()
    capex, currency = _scenario_capex(sc)
    values, kwargs = {}, {}
    for _group, fields in LOAN_FIELDS:
        for name, _label, _unit, default, _help in fields:
            raw = data.get(name, "")
            values[name] = raw if raw != "" else ("" if default is None else default)
            kwargs[name] = _num(raw) if raw != "" else default
    if kwargs["project_total"] is None:
        kwargs["project_total"] = capex or finance.DEFAULTS["project_total"]
    freq = _num(data.get("per_year"))
    per_year = int(freq) if freq in (1, 2, 4, 12) else 12  # a junk value falls back to monthly
    method = data.get("method", "annuity")
    grace = _num(kwargs["grace_months"])
    kwargs.update(per_year=per_year, method=method, finance_fees=data.get("finance_fees") == "1",
                  grace_months=int(grace) if grace and grace == int(grace) else 0)
    result, error = None, None
    try:
        result = finance.loan(**kwargs)
    except finance.LoanInputError as exc:
        error = str(exc)
    groups = [(g, [{"name": n, "label": lab, "unit": u or currency, "value": values[n], "help": h} for n, lab, u, _d, h in fs])
              for g, fs in LOAN_FIELDS]
    return render(request, "planner/loan.html", {
        "groups": groups, "r": result, "error": error, "currency": currency, "capex": capex,
        "per_year": per_year, "frequencies": finance.FREQUENCIES.items(), "method": method,
        "methods": finance.METHODS.items(), "finance_fees": kwargs["finance_fees"],
        "period_name": {12: "month", 4: "quarter", 2: "half-year", 1: "year"}[per_year] if per_year in (12, 4, 2, 1) else "period",
        "scenarios": Scenario.objects.all(), "scenario": sc})
