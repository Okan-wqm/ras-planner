"""Workflow steps 01 Brief, 03 Units & tanks and 04 Requirements."""
import math
import uuid
from datetime import date

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from ras_engine import growth_curve
from ras_engine.equipment import normalize_steps

from .forms import BIO_EXCLUDE, BiofilterForm, ParamsForm, scenario_base_values, stage_field_specs
from .models import Feed, Scenario
from .services import plan_result, prewarm
from .views import (_equipment_units, _finite, _float_or_none, _int_or_none, _parse_stages, _stage_panels)

REQUIREMENT_GROUPS = ["Gas balance", "Flows"]
# the flow table: per parameter, the limit in the tank and the water entering the tank
TREAT = [("tan", "TAN", "flow.tan_max", "flow.tan_removal_eff", "flow.tan_best", "biofilter"),
         ("co2", "CO₂", "flow.co2_max", "flow.co2_removal_eff", "flow.co2_best", "degasser"),
         ("tss", "TSS", "flow.tss_max", "flow.tss_removal_eff", "flow.tss_best", "solids filters")]


def _flow_rows(design):
    """One RAS unit: what each parameter asks of the flow, Q = load / (tank limit − water entering)."""
    from ras_engine.formulas import o2_saturation
    p = design["stage"].params
    g, f = p.gas, p.flow
    peak = design["peak"]
    csat = o2_saturation(design["temperature"], g.salinity)
    o2_in = csat * (g.inlet_o2_saturation if g.pure_oxygen else g.aeration_saturation)
    rows = [{"key": "o2", "label": "O₂", "load": peak["o2_kgd"], "load_note": "used by the fish",
             "limit": round(csat * g.tank_outlet_saturation, 2), "limit_note": "minimum leaving the tank",
             "inlet": round(o2_in, 2), "inlet_note": "pure oxygen (cone)" if g.pure_oxygen else "aerated water",
             "eta": g.o2_transfer_efficiency, "pure": g.pure_oxygen, "csat": round(csat, 3), "q": peak["q_o2_m3h"]}]
    for key, label, kmax, keff, kbest, by in TREAT:
        mx, eff, best = getattr(f, kmax[5:]), getattr(f, keff[5:]), getattr(f, kbest[5:])
        rows.append({"key": key, "label": label, "load": peak[f"{key}_kgd"], "load_note": "produced in the tanks",
                     "limit": mx, "limit_note": "maximum in the tank", "inlet": round(mx + eff * (best - mx), 3),
                     "inlet_note": f"after the {by} ({eff * 100:.0f} % per pass)", "best": best,
                     "q": peak[f"q_{key}_m3h"]})
    return rows


def _save_flow_table(scenario, post, res):
    """Store what was typed in the flow tables: limits and inlet water per RAS unit."""
    from ras_engine.formulas import o2_saturation
    from .forms import same_value
    settings = scenario.plan_settings()
    stages = [dict(st) for st in settings["stages"]]
    base = dict(scenario.params or {})
    base_flat = scenario.engine_params().to_flat()
    single = len(stages) == 1
    for i, d in enumerate(res["designs"]):
        if d["empty"] or f"u{i}-o2_in" not in post:
            continue
        p = d["stage"].params
        csat = o2_saturation(d["temperature"], p.gas.salinity)
        vals = {}
        pure = post.get(f"u{i}-pure") == "yes"
        vals["gas.pure_oxygen"] = pure
        o2_in, o2_out = _float_or_none(post.get(f"u{i}-o2_in")), _float_or_none(post.get(f"u{i}-o2_out"))
        if o2_in is not None and csat > 0:
            vals["gas.inlet_o2_saturation" if pure else "gas.aeration_saturation"] = round(o2_in / csat, 4)
        if o2_out is not None and csat > 0:
            vals["gas.tank_outlet_saturation"] = round(o2_out / csat, 4)
        for key, _label, kmax, keff, kbest, _by in TREAT:
            mx = _float_or_none(post.get(f"u{i}-{key}_max"))
            cin = _float_or_none(post.get(f"u{i}-{key}_in"))
            mx = getattr(p.flow, kmax[5:]) if mx is None else mx
            vals[kmax] = mx
            if cin is not None:
                best = min(getattr(p.flow, kbest[5:]), cin)  # the best the treatment reaches cannot be above what enters
                vals[kbest] = best
                vals[keff] = round(min(max((mx - cin) / (mx - best), 0.0), 1.0), 4) if mx > best else 0.0
        if single:
            base.update(vals)
        else:
            ov = dict(stages[i].get("overrides") or {})
            for k, v in vals.items():  # a RAS unit keeps only what differs from the scenario
                if same_value(v, base_flat[k]):
                    ov.pop(k, None)
                else:
                    ov[k] = v
            stages[i]["overrides"] = ov
    scenario.params = base
    scenario.production = {**settings, "stages": stages}
# shown on the Brief, not again among the requirement constants
BRIEF_KEYS = {"growth.temperature", "gas.salinity", "growth.initial_count"}


# ---------- 01 Brief ----------

def _normalize_temp_steps(text):
    """Accept calendar-date format ('1 Jan:10, 10 Feb:12') or day-number format ('1:10, 41:12').
    Always store as day:temp so the engine can parse it directly."""
    text = text.strip()
    if not text:
        return text
    try:
        from ras_engine.params import parse_date_steps
        steps = parse_date_steps(text)
        return ", ".join(f"{d}:{t:g}" for d, t in steps)
    except (ValueError, ImportError):
        return text  # let the engine's own parser deal with it (or reject)


def _brief_values(scenario):
    b = scenario.brief or {}
    plan = scenario.plan_settings()
    params = scenario.engine_params()
    return {
        "name": scenario.name,
        "customer": b.get("customer", ""),
        "batches_per_year": b.get("batches_per_year") or round(365 / max(plan["interval_days"], 1), 1),
        "pieces_per_batch": b.get("pieces_per_batch"),
        "target_weight_g": b.get("target_weight_g") or plan["stages"][-1]["max_weight_g"],
        "start_date": plan["start_date"],
        "water_m3h": b.get("water_m3h"), "altitude_m": b.get("altitude_m"),
        "water_note": b.get("water_note", ""),
        "temperature": params.growth.temperature, "temperature_steps": scenario.temperature_steps,
        "salinity": params.gas.salinity, "use_feed_catalog": scenario.use_feed_catalog,
        "initial_weight_g": params.growth.initial_weight_g,
        "set_stocking": True,
    }


def _brief_posted(post):
    """The brief form exactly as it was typed, for re-rendering the page after a rejected save."""
    return {
        "name": post.get("name", "").strip(),
        "customer": post.get("customer", "").strip(),
        "batches_per_year": _float_or_none(post.get("batches_per_year")),
        "pieces_per_batch": _float_or_none(post.get("pieces_per_batch")),
        "target_weight_g": _float_or_none(post.get("target_weight_g")),
        "start_date": post.get("start_date", ""),
        "water_m3h": _float_or_none(post.get("water_m3h")), "altitude_m": _float_or_none(post.get("altitude_m")),
        "water_note": post.get("water_note", "").strip(),
        "temperature": _float_or_none(post.get("temperature")),
        "temperature_steps": _normalize_temp_steps(post.get("temperature_steps", "").strip()),
        "salinity": _float_or_none(post.get("salinity")),
        "use_feed_catalog": post.get("use_feed_catalog") == "on",
        "initial_weight_g": _float_or_none(post.get("initial_weight_g")),
        "set_stocking": post.get("set_stocking") == "on",
    }


def _brief_page(request, scenario, v):
    """Step 01 with the given values: the normal page, or the form as typed after a rejected save."""
    ctx = {"scenario": scenario, "scenarios": Scenario.objects.all(), "b": v,
           "interval": max(round(365 / v["batches_per_year"]), 1) if v["batches_per_year"] else None,
           "stock_now": scenario.engine_params().growth.initial_count}
    if v["pieces_per_batch"]:
        try:
            ctx["derived"] = stocking_for(scenario, v["pieces_per_batch"], v["target_weight_g"])
        except ValueError as exc:
            ctx["derived_error"] = str(exc)
        except TypeError:   # e.g. a rejected post with no target weight: nothing to derive
            pass
    return render(request, "planner/brief.html", ctx)


def stocking_for(scenario, pieces: float, target_weight_g: float) -> dict:
    """Fish to stock per batch so that `pieces` reach the target weight (mortality on the way)."""
    plan, stages = scenario.engine_plan()
    feeds = [f.to_engine() for f in Feed.objects.all()] if scenario.use_feed_catalog else None
    curve = growth_curve(scenario.engine_params(), target_weight_g, feeds=feeds,
                         temperature_steps=scenario.temperature_steps, stages=stages)
    survival = curve[-1]["count"] / curve[0]["count"] if curve[0]["count"] else 1.0
    return {"days": len(curve), "survival": survival, "stock": math.ceil(pieces / survival) if survival else None}


def brief(request, pk):
    scenario = get_object_or_404(Scenario, pk=pk)
    if request.method == "POST":
        post = request.POST
        errors = []
        batches = _float_or_none(post.get("batches_per_year"))
        pieces = _float_or_none(post.get("pieces_per_batch"))
        target = _float_or_none(post.get("target_weight_g"))
        if not batches or batches <= 0 or batches > 52:
            errors.append("Batches per year must be between 1 and 52.")
        if not target or target <= 0:
            errors.append("Enter the target fish weight.")
        try:
            start = date.fromisoformat(post.get("start_date", ""))
        except ValueError:
            start = None
            errors.append("Enter the first stocking date.")
        if errors:
            # re-render with what was typed: a redirect would throw the whole form away
            for e in errors:
                messages.error(request, e)
            scenario.refresh_from_db()
            return _brief_page(request, scenario, _brief_posted(post))
        scenario.name = post.get("name", "").strip() or scenario.name
        scenario.brief = {"customer": post.get("customer", "").strip(), "batches_per_year": batches,
                          "pieces_per_batch": pieces, "target_weight_g": target,
                          "water_m3h": _float_or_none(post.get("water_m3h")),
                          "altitude_m": _float_or_none(post.get("altitude_m")),
                          "water_note": post.get("water_note", "").strip()}
        scenario.temperature_steps = _normalize_temp_steps(post.get("temperature_steps", ""))
        scenario.use_feed_catalog = post.get("use_feed_catalog") == "on"
        params = dict(scenario.params or {})
        for key, field in (("growth.temperature", "temperature"), ("gas.salinity", "salinity"),
                           ("growth.initial_weight_g", "initial_weight_g")):
            v = _float_or_none(post.get(field))
            if v is not None:
                params[key] = v
        scenario.params = params
        # the plan follows the brief: interval from batches per year, harvest at the target weight
        prod = scenario.plan_settings()
        prod["start_date"] = start.isoformat()
        prod["interval_days"] = max(round(365 / batches), 1)
        stages = [dict(st) for st in prod["stages"]]
        if len(stages) > 1 and target <= stages[-2]["max_weight_g"]:
            messages.error(request, f"The target weight must be above the previous RAS unit's exit weight "
                                    f"({stages[-2]['max_weight_g']:g} g). Change the units on step 03.")
            scenario.refresh_from_db()  # drop the unsaved assignments below: the form shows the typed values
            return _brief_page(request, scenario, _brief_posted(post))
        stages[-1]["max_weight_g"] = target
        prod["stages"] = stages
        scenario.production = prod
        scenario.save()
        if pieces and post.get("set_stocking") == "on":
            try:
                st = stocking_for(scenario, pieces, target)
                scenario.params = {**scenario.params, "growth.initial_count": st["stock"]}
                scenario.save()
                messages.success(request, f"Brief saved. {st['stock']:,} fish are stocked per batch so that "
                                          f"{pieces:,.0f} reach {target:g} g ({st['survival'] * 100:.1f} % survive "
                                          f"{st['days']} days).")
            except ValueError as exc:
                messages.error(request, f"Brief saved, but the stocking could not be worked out: {exc}")
        else:
            messages.success(request, "Brief saved.")
        prewarm(scenario)
        return redirect("brief", pk=pk)

    return _brief_page(request, scenario, _brief_values(scenario))


# ---------- 03 Units & tanks ----------

def _stages_from_post(post, base):
    """RAS-unit rows exactly as typed (raw text kept, even where a number was expected), for
    re-rendering step 03 after a rejected save."""
    names = post.getlist("stage_name")
    mxs = post.getlist("stage_max") + [""] * len(names)
    tanks = post.getlist("stage_tanks") + [""] * len(names)
    rets = post.getlist("stage_ret") + [""] * len(names)
    ids = post.getlist("stage_id") + [""] * len(names)
    stages, seen = [], set()
    for i, (name, mx, n_tanks, ret, uid) in enumerate(zip(names, mxs, tanks, rets, ids)):
        if not name.strip() and not mx.strip():
            continue
        if not uid or uid in seen:
            uid = uuid.uuid4().hex[:8]
        seen.add(uid)
        ov = {}
        for key, _label, _unit, _group, default in stage_field_specs():
            values = post.getlist("ov_" + key)
            if i < len(values) and values[i] != "":
                ov[key] = values[i] == "True" if isinstance(default, bool) else values[i]
        stages.append({"id": uid, "name": name, "max_weight_g": mx, "n_tanks": n_tanks,
                       "retention_min": ret, "overrides": ov})
    return stages


def _tanks_from_post(post, eq_units):
    """The tank picks re-shown as typed when step 03 is re-rendered after a rejected save."""
    for u in eq_units:
        uid, t = u.get("uid"), u.get("tank")
        if not uid or t is None or f"t-{uid}-item" not in post:
            continue
        t["sel"] = {**t.get("sel", {}), "item": post.get(f"t-{uid}-item", ""),
                    "capacity": _float_or_none(post.get(f"t-{uid}-cap", ""))}
        t["rows"] = [[up, k] for up, k in zip(post.getlist(f"t-{uid}-upto"), post.getlist(f"t-{uid}-k"))]
        t["auto_steps"] = False


def _units_page(request, scenario, settings, base, post=None, status=200):
    """Step 03 rendered. With `post` (a save that was rejected), the RAS rows, tank picks and rest
    days show what was typed; everything else is the page as saved."""
    shown = settings
    if post is not None:
        shown = {**settings, "stages": _stages_from_post(post, base),
                 "tank_rest_days": post.get("tank_rest_days", settings["tank_rest_days"])}
    ctx = {"scenario": scenario, "scenarios": Scenario.objects.all(), "settings": shown,
           "panels": _stage_panels(shown, base)}
    got, err = plan_result(scenario)
    if err:
        ctx["error"] = err
        return render(request, "planner/units.html", ctx, status=status)
    plan, res = got
    eq_units, occupancy, _water = _equipment_units(scenario, res)
    for u in eq_units:
        u["uid"] = scenario.unit_id(u["index"])
    if post is not None:
        _tanks_from_post(post, eq_units)
    total_volume = sum(d["peak"]["volume_m3"] for d in res["designs"] if not d["empty"]) or 1
    peak_day = 0
    if res["daily"]:
        totals = [sum(res["daily"][k][t]["volume_m3"] for k in range(len(res["daily"]))) for t in range(res["horizon"])]
        peak_day = max(range(len(totals)), key=totals.__getitem__) if totals else 0
    ctx.update({"res": res, "plan": plan, "designs": res["designs"], "units": eq_units, "harvests": res["harvests"],
                "total_volume": total_volume,
                "fish_per_batch": scenario.engine_params().growth.initial_count,
                "occupancy": {"start": plan.start_date.isoformat(), "horizon": res["horizon"],
                              "stages": occupancy, "peak_day": peak_day}})
    return render(request, "planner/units.html", ctx, status=status)


def units(request, pk):
    scenario = get_object_or_404(Scenario, pk=pk)
    settings = scenario.plan_settings()
    base = scenario_base_values(scenario)
    if request.method == "POST":
        # optimistic concurrency: the form carries the `updated` it was rendered with; a mismatch
        # means another tab saved since — show what was typed over the current page, save nothing
        posted_updated = request.POST.get("updated", "").strip()
        if posted_updated and posted_updated != scenario.updated.isoformat():
            messages.warning(request, f"This scenario was saved by another tab at {scenario.updated}. "
                                      "Reload the page and re-apply your changes.")
            return _units_page(request, scenario, settings, base, post=request.POST, status=409)
        stages, errors = _parse_stages(request.POST, base)
        if stages is None:
            # re-render with what was typed: a redirect would throw the whole form away
            for e in errors:
                messages.error(request, e)
            return _units_page(request, scenario, settings, base, post=request.POST)
        rest = _int_or_none(request.POST.get("tank_rest_days"))
        scenario.production = {**{k: v for k, v in settings.items()}, "stages": stages,
                               "tank_rest_days": max(rest, 0) if rest is not None else settings["tank_rest_days"]}
        # tanks per unit, saved under the unit's id
        equipment = dict(scenario.equipment or {})
        sel_all = dict(equipment.get("stages") or {})
        for st in stages:
            uid = st["id"]
            if f"t-{uid}-item" not in request.POST:
                continue
            steps = [[_float_or_none(u), _int_or_none(k)] for u, k in
                     zip(request.POST.getlist(f"t-{uid}-upto"), request.POST.getlist(f"t-{uid}-k"))]
            sel = dict(sel_all.get(uid) or {})
            sel["tank"] = {"item": request.POST.get(f"t-{uid}-item", ""),
                           "capacity": _float_or_none(request.POST.get(f"t-{uid}-cap", "")),
                           "qty": None, "steps": [list(s) for s in normalize_steps(steps)]}
            sel_all[uid] = sel
        # a RAS unit deleted in this form takes its saved diagram and equipment picks with it;
        # units that are still in the form (their ids were posted) keep theirs untouched
        live = {st["id"] for st in stages}
        dead = {k for k in (scenario.process or {}) if k not in live} | {k for k in sel_all if k not in live}
        if dead:
            scenario.process = {k: v for k, v in (scenario.process or {}).items() if k in live}
            sel_all = {k: v for k, v in sel_all.items() if k in live}
        equipment["stages"] = sel_all
        scenario.equipment = equipment
        scenario.save()
        prewarm(scenario)
        if dead:
            messages.info(request, f"Removed data for {len(dead)} deleted RAS unit(s)")
        messages.success(request, "RAS units and tanks saved.")
        return redirect("units", pk=pk)

    return _units_page(request, scenario, settings, base)


def _flow_units_from_post(scenario, post):
    """The posted flow tables re-rendered without a plan (the plan cannot be calculated): the typed
    limits and inlet water stand; loads and computed flows are unknown."""
    from types import SimpleNamespace
    stages = scenario.plan_settings()["stages"]
    units, i = [], 0
    while f"u{i}-o2_in" in post:
        st = stages[i] if i < len(stages) else {}
        rows = [{"key": "o2", "label": "O₂", "load": None, "load_note": "",
                 "limit": _float_or_none(post.get(f"u{i}-o2_out")), "limit_note": "",
                 "inlet": _float_or_none(post.get(f"u{i}-o2_in")), "inlet_note": "",
                 "eta": None, "pure": post.get(f"u{i}-pure") == "yes", "csat": None, "q": None}]
        for key, label, _kmax, _keff, _kbest, _by in TREAT:
            rows.append({"key": key, "label": label, "load": None, "load_note": "",
                         "limit": _float_or_none(post.get(f"u{i}-{key}_max")), "limit_note": "",
                         "inlet": _float_or_none(post.get(f"u{i}-{key}_in")), "inlet_note": "",
                         "best": None, "q": None})
        units.append({"index": i, "rows": rows,
                      "d": SimpleNamespace(stage=SimpleNamespace(name=st.get("name", f"RAS-{i + 1}")),
                                           peak={}, temperature=None)})
        i += 1
    return units


# ---------- 04 Requirements ----------

def requirements(request, pk):
    scenario = get_object_or_404(Scenario, pk=pk)
    params_form = ParamsForm(request.POST or None, values=scenario.params, groups=REQUIREMENT_GROUPS,
                             exclude=BRIEF_KEYS)
    bio_form = BiofilterForm(request.POST or None, values=scenario.biofilter, exclude=BIO_EXCLUDE | {"design_weight_g"},
                             prefix="bio")
    if request.method == "POST" and request.POST.get("form") == "flows":
        got, err = plan_result(scenario)
        if err:
            # re-render with the typed table: a redirect would throw it away
            messages.error(request, err)
            return render(request, "planner/requirements.html", {
                "scenario": scenario, "scenarios": Scenario.objects.all(), "error": err,
                "flow_units": _flow_units_from_post(scenario, request.POST),
                # the assumptions below were not part of this post: keep showing the saved values
                "params_form": ParamsForm(values=scenario.params, groups=REQUIREMENT_GROUPS, exclude=BRIEF_KEYS),
                "bio_form": BiofilterForm(values=scenario.biofilter, exclude=BIO_EXCLUDE | {"design_weight_g"},
                                          prefix="bio")})
        _plan, res0 = got
        _save_flow_table(scenario, request.POST, res0)
        scenario.save()
        messages.success(request, "Flow table saved; the plan, requirements and targets are recalculated.")
        return redirect("requirements", pk=pk)
    if request.method == "POST":
        if params_form.is_valid() and bio_form.is_valid():
            scenario.params = {**(scenario.params or {}), **params_form.cleaned_data}
            scenario.biofilter = {**(scenario.biofilter or {}), **bio_form.cleaned_data}
            scenario.save()
            messages.success(request, "Treatment assumptions saved; requirements recalculated.")
            return redirect("requirements", pk=pk)
        messages.error(request, "Some values are not valid; see the marked fields.")
    ctx = {"scenario": scenario, "scenarios": Scenario.objects.all(), "params_form": params_form,
           "bio_form": bio_form}
    got, err = plan_result(scenario)
    if err:
        ctx["error"] = err
        return render(request, "planner/requirements.html", ctx)
    plan, res = got
    designs = res["designs"]
    flows = {"start": plan.start_date.isoformat(), "stages": [s.name for s in res["stages"]],
             "series": [{k: [_finite(d[k], 1) for d in days] for k in ("q_o2_m3h", "q_tan_m3h", "q_co2_m3h", "q_tss_m3h")}
                        for days in res["daily"]]}
    flow_units = [{"index": i, "d": d, "rows": _flow_rows(d)}
                  for i, d in enumerate(designs) if not d["empty"]]
    # flow-through (akarsu) comparison: how much fresh water without recirculation
    from ras_engine.flow_through import flow_through_needs
    from ras_engine.params import Params as _P
    _params = _P.from_flat(scenario.params)
    ft = None
    for d in designs:
        if not d["empty"]:
            ft = flow_through_needs(d["peak"], _params.flow, _params.gas)
            break
    ctx.update({"res": res, "plan": plan, "designs": designs, "flows": flows,
                "flow_units": flow_units, "ft": ft})
    return render(request, "planner/requirements.html", ctx)
