"""Equipment sizing and CAPEX lines shared by the views: what each RAS unit needs and what it costs."""
from ras_engine import CATEGORIES, size_item, size_tanks, stage_requirements
from ras_engine.compare import unit_targets
from ras_engine.equipment import SIZED_CATEGORIES, allocate_tanks
from ras_engine.process import NODE_TYPES, default_diagram

from .models import EquipmentStandard
from .parse import _float_or_none
from .services import diagram_results


def _resolve(sel: dict, standards: dict):
    """(capacity, standard or None) for a saved selection: a standard id, 'custom' or nothing."""
    item = str(sel.get("item") or "")
    if item.isdigit() and int(item) in standards:
        std = standards[int(item)]
        return std.capacity, std
    if item == "custom":
        return sel.get("capacity"), None
    return None, None


def _tank_steps(sel: dict) -> list:
    """Saved tank steps [[up_to_g or None, tanks], ...]; older saves only had one 'qty'."""
    if sel.get("steps"):
        return [(u, k) for u, k in sel["steps"]]
    return [(None, sel["qty"])] if sel.get("qty") else []


def _line(i, stage, key, label, std, sel, qty, unit):
    """One CAPEX line for a chosen piece of equipment (priced from its standard if it has one)."""
    if std:
        item = std.name
    else:
        item = f"Custom {sel.get('capacity'):g} {unit}" if sel.get("capacity") else "Custom"
    return {"key": f"{stage.uid or i}-{key}", "stage": stage.name, "stage_index": i, "category": label,
            "item": item, "qty": qty, "unit_price": std.unit_price if std else None}


def _requirements_from_diagram(req, scenario, index, design):
    """A unit with a process diagram: equipment must handle what the diagram sends through it."""
    if req.get("empty") or not _has_diagram(scenario, index):
        return
    r = diagram_results(_with_basis(scenario, index))
    if not r:
        req["source"] = "plan"  # the diagram cannot be solved; keep the plan estimate
        return
    tot, by = r["totals"], r["totals"]["flow_by_type"]
    fill = ((design.get("biofilter_params") and design["biofilter_params"].fill_ratio_pct) or 40.0) / 100
    req["required"].update({
        "pump": tot["recirculated_m3h"], "drum_filter": by.get("drum_filter", 0.0),
        "degasser": by.get("degasser", 0.0), "uv": by.get("uv", 0.0),
        "oxygen": tot["pure_o2_kgd"], "ozone": tot["ozone_kgd"] * 1000 / 24,
    })
    if tot["media_m3"] > 0:
        req["required"].update({"biomedia": tot["media_m3"], "biofilter": tot["media_m3"] / fill})
    req["source"] = "diagram"


def _equipment_units(scenario, res):
    """Requirements vs selections per RAS unit, the tank occupancy drawing and the CAPEX lines."""
    saved = scenario.equipment or {}
    water = saved.get("water") or "fresh"
    standards = {s.pk: s for s in EquipmentStandard.objects.all()}
    by_cat = {k: [s for s in standards.values() if s.category == k] for k, _, _ in CATEGORIES}
    units, occupancy = [], []
    rest_days = scenario.plan_settings()["tank_rest_days"]
    for i, stage in enumerate(res["stages"]):
        req = stage_requirements(res, i, water)
        _requirements_from_diagram(req, scenario, i, res["designs"][i])
        sel = scenario.equipment_of(i)
        dlines = _diagram_lines(scenario, i, stage.name, standards)
        for ln in dlines:
            ln["options"] = by_cat.get(ln["cat"], [])
            ln["tags"] = ", ".join(x for x in ln["names"] if x != ln["category"])
            ln["total"] = ln["unit_price"] * ln["qty"] if ln["unit_price"] is not None else None
        unit = {"index": i, "stage": stage, "req": req, "rows": [], "lines": [], "cost": 0.0, "priced": False,
                "diagram_lines": dlines, "has_diagram": _has_diagram(scenario, i),
                "diagram_total": sum(ln["total"] or 0 for ln in dlines),
                "diagram_priced": any(ln["total"] is not None for ln in dlines), "index_s": str(i)}
        if not req["empty"]:
            tsel = sel.get("tank", {})
            cap, std = _resolve(tsel, standards)
            tank = size_tanks(req, res, i, cap, _tank_steps(tsel), rest_days)
            tank.update(sel=tsel, std=std, options=by_cat["tank"],
                        rows=tank["step_input"] if not tank["auto_steps"] else [])
            if tank.get("total_tanks") and cap:
                unit["lines"].append(_line(i, stage, "tank", "Tank", std, tsel, tank["total_tanks"], "m³"))
            if std and std.unit_price is not None and tank.get("total_tanks"):
                tank["cost"] = tank["total_tanks"] * std.unit_price
                unit["cost"] += tank["cost"]
                unit["priced"] = True
            unit["tank"] = tank
            for key, label, cat_unit in SIZED_CATEGORIES[1:]:
                isel = sel.get(key, {})
                cap, std = _resolve(isel, standards)
                row = size_item(req["required"][key], cap, isel.get("qty"))
                row.update(key=key, label=label, unit=cat_unit, sel=isel, std=std, options=by_cat[key])
                if cap and row.get("qty") and not row.get("not_needed"):
                    unit["lines"].append(_line(i, stage, key, label, std, isel, row["qty"], cat_unit))
                    if std and std.unit_price is not None:
                        row["cost"] = row["qty"] * std.unit_price
                        unit["cost"] += row["cost"]
                        unit["priced"] = True
                unit["rows"].append(row)
            alloc = tank.get("allocation")
            assumed = alloc is None  # nothing chosen yet: draw one tank per batch at its peak size
            if assumed:
                alloc = allocate_tanks(res, i, [(None, 1)], rest_days)
            occupancy.append({
                "name": stage.name, "index": i, "assumed": assumed, "n_tanks": alloc["n_tanks"],
                "tank_volume": (tank.get("tank_volume") or tank.get("required_tank_volume")
                                or req["batch_peak_volume_m3"]),
                "batches": alloc["batches"]})
        # a unit with a process diagram is costed from what is drawn there (as CAPEX does)
        unit["total"] = unit["diagram_total"] if unit["has_diagram"] else unit["cost"]
        units.append(unit)
    return units, occupancy, water


def _diagram_for(scenario, res, index):
    """Saved diagram of a RAS unit, or a typical loop prefilled with that unit's peak loads.
    In flow-through mode the default is a single-pass layout (makeup → jet → tanks → discharge)."""
    saved = scenario.diagram_of(index)
    if saved and saved.get("nodes") is not None:
        return saved, False
    d = res["designs"][index]
    peak = d.get("peak") or {}
    # tanks carry the fish's own load; the biofilter adds nitrification's O₂ use and CO₂ itself.
    # loads stay at the CALCULATED level — the design envelope sizes equipment, not flows
    loads = {"tan_kgd": peak.get("tan_kgd", 0.0),
             "co2_kgd": max(peak.get("co2_kgd", 0.0) - peak.get("co2_bio_kgd", 0.0), 0.0),
             "tss_kgd": peak.get("tss_kd", 0.0),
             "o2_kgd": peak.get("o2_kgd", 0.0)}
    n_tanks = max(int(d["stage"].n_tanks), 1)
    basis = _diagram_basis(d)

    # system mode: flow-through gets a linear diagram, RAS gets the classic loop
    try:
        params = scenario.engine_params()
        mode = getattr(params.design, "mode", "ras")
    except AttributeError:
        mode = "ras"  # test mocks / non-Scenario objects default to RAS
    if mode == "flow_through":
        from ras_engine.flow_through import flow_through_needs
        ft = flow_through_needs(peak, params.flow, params.gas)
        ft_flow = ft.get("total_with_oxy_m3h", 100.0)
        if ft_flow == float("inf"):
            ft_flow = ft.get("total_no_oxy_m3h", 100.0)
        if ft_flow == float("inf") or ft_flow <= 0:
            ft_flow = 100.0
        from ras_engine.process import default_diagram_flow_through
        return default_diagram_flow_through(d["stage"].name, ft_flow, loads, n_tanks, basis), True

    flow = max(peak.get("q_design_m3h") or 100.0, d.get("q_unit_m3h") or 0.0)
    if flow == float("inf"):
        flow = 100.0
    return default_diagram(d["stage"].name, flow, loads, n_tanks, basis), True


def _diagram_basis(design):
    """The scenario's assumptions for a RAS unit, so the starting diagram agrees with the plan."""
    from ras_engine import Params
    from ras_engine.formulas import o2_saturation
    p = design["stage"].params or Params()
    if design.get("empty"):
        return {}
    temp = design["temperature"]
    csat = o2_saturation(temp, p.gas.salinity)
    return {
        "tan_removal_pct": p.flow.tan_removal_eff * 100, "co2_removal_pct": p.flow.co2_removal_eff * 100,
        "tss_removal_pct": p.flow.tss_removal_eff * 100,
        "o2_per_tan": p.gas.o2_per_tan, "co2_per_tan": p.gas.co2_per_tan,
        "media_m3": (design.get("biofilter") or {}).get("media_fresh_m3") if (design.get("biofilter") or {}).get("feasible") else None,
        "cone_o2_mgl": csat * p.gas.inlet_o2_saturation if p.gas.pure_oxygen else None,
        "temp_c": temp,
        "limits": {"tss": {"max": p.flow.tss_max}, "tan": {"max": p.flow.tan_max}, "co2": {"max": p.flow.co2_max},
                   "o2": {"min": round(csat * p.gas.tank_outlet_saturation, 2)}, "no3": {"max": p.flow.no3_max}},
    }


# specification fields that describe a piece of equipment (loads and removal rates do not)
_SIZE_FIELDS = ("volume_m3", "flow_m3h", "power_kw", "capacity_kw", "media_m3", "area_m2", "output_g_h", "trays",
                "dose_mj_cm2", "mesh_um")


def _describe(node, t):
    spec = (node.get("data") or {}).get("spec") or {}
    parts = []
    for f in t["spec"]:
        if f["key"] in _SIZE_FIELDS and spec.get(f["key"]) not in (None, ""):
            v = _float_or_none(spec[f["key"]])
            if v:   # an empty or zero size says nothing
                parts.append(f"{v:,.4g} {f['unit']}".strip())
    return ", ".join(parts[:2])


def _sized(node, r):
    """Size of an item from what the solved diagram sends through it: a radial flow settler's volume is its
    flow × residence time; a UV unit and a heat exchanger are sized by the water passing them."""
    if not r:
        return ""
    t = node.get("type")
    if t == "rfs" and r.get("volume_m3") is not None:
        return f"{r['volume_m3']:,.2f} m³ ({r.get('q_in', 0):,.1f} m³/h × {r.get('residence_min', 0):g} min)"
    if t in ("uv", "cone"):
        return f"{r.get('q_in', 0):,.1f} m³/h"
    if t in ("biofilter", "fbbr") and r.get("media_m3"):
        vessel = f"{r['vessel_m3']:,.1f} m³ vessel · " if r.get("vessel_m3") else ""
        return f"{vessel}{r['media_m3']:,.1f} m³ media ({r.get('fill_pct', 0):g} % fill)"
    if t in ("hx_plate", "hx_tube") and r.get("q_process") is not None:
        return f"{r['q_process']:,.1f} m³/h · {r.get('duty_kw', 0):,.1f} kW"
    return ""


def _diagram_lines(scenario, index, stage_name, standards):
    """CAPEX lines for the equipment drawn in a RAS unit's process diagram: identical pieces (same type,
    standard, size and price) are one line with their quantity, e.g. 'Pump · 290 m³/h × 4'."""
    import hashlib
    diagram = _with_basis(scenario, index) or {}
    solved = (diagram_results(diagram) or {}).get("nodes") or {} if diagram.get("nodes") else {}
    groups = {}
    for n in diagram.get("nodes") or []:
        t = NODE_TYPES.get(n.get("type"))
        if not t or not t["category"]:  # make-up water, discharge and connections are not equipment
            continue
        data = n.get("data") or {}
        std = standards.get(int(data["standard"])) if str(data.get("standard") or "").isdigit() else None
        price = _float_or_none(data.get("unit_price"))
        if price is None and std:
            price = std.unit_price
        qty = _float_or_none(data.get("qty")) or 1
        desc = std.name if std else (_sized(n, solved.get(n.get("id"))) or _describe(n, t))
        sig = (n.get("type"), std.pk if std else None, price, desc)
        g = groups.get(sig)
        if g is None:
            g = groups[sig] = {"stage": stage_name, "stage_index": index,
                               "category": t["label"], "item": desc or "—", "qty": 0, "names": [],
                               "unit_price": price, "from_diagram": True, "type": n.get("type"),
                               "cat": t["category"], "std": std, "size": desc, "node_ids": [],
                               "own_price": _float_or_none(data.get("unit_price"))}
        g["qty"] += qty
        g["names"].append(data.get("name") or t["label"])
        g["node_ids"].append(str(n.get("id")))
    lines = list(groups.values())
    for g in lines:
        g["qty"] = int(g["qty"]) if g["qty"] == int(g["qty"]) else g["qty"]
        # stable line key: what is installed (type, standard, which nodes) — NOT the resolved price or
        # size, so a price change or a re-solve cannot re-key the line and orphan a saved override.
        # gkey is the POST prefix the Equipment page uses to pick a standard/price for these nodes.
        gkey = hashlib.sha1(repr((g["type"], g["std"].pk if g["std"] else None,
                                  sorted(g["node_ids"]))).encode()).hexdigest()[:10]
        g["key"] = f"p{scenario.unit_id(index)}-{gkey}"
        g["gkey"] = gkey
    return lines


def _has_diagram(scenario, index):
    nodes = (scenario.diagram_of(index) or {}).get("nodes") or []
    return any(NODE_TYPES.get(n.get("type"), {}).get("category") for n in nodes)


def _media_basis(scenario, index, res=None):
    """Biofilter media required for a RAS unit (step 04) and the fill ratio: shared by its biofilters."""
    if res is None:
        from .services import plan_cached
        got = plan_cached(scenario)
        res = got[1] if got else None
    if not res or index >= len(res["designs"]):
        return None
    design = res["designs"][index]
    t = unit_targets(design, (scenario.equipment or {}).get("water") or "fresh")
    return {"media_m3": t.get("media_m3"), "fill_pct": t.get("fill_pct"), "fish": _fish_basis(design)} if t else None


def _fish_basis(design):
    """The unit's fish on its busiest day; see _fish_of."""
    return _fish_of(design.get("peak") or {})


def _fish_of(day):
    """One day of a RAS unit from the plan: its fish, feed, and what one kg of feed makes in the tanks (fish
    only: nitrification is the biofilter's own load). Tanks in the diagram work out their loads from these.
    FLOW PHILOSOPHY: loads stay at the CALCULATED level — the design envelope sizes equipment
    (compare targets, OPEX) but must NOT inflate the diagram loads or the recirculation flow."""
    feed = day.get("feed_kg") or 0.0
    if feed <= 0:
        return None
    co2_fish = max(day.get("co2_kgd", 0.0) - day.get("co2_bio_kgd", 0.0), 0.0)
    return {"feed_kgd": feed, "biomass_kg": day.get("biomass_kg", 0.0), "count": day.get("count", 0.0),
            "batches": day.get("batches", 0), "tan_per_feed": day.get("tan_kgd", 0.0) / feed,
            "tss_per_feed": day.get("tss_kgd", 0.0) / feed, "co2_per_feed": co2_fish / feed,
            "o2_per_feed": day.get("o2_kgd", 0.0) / feed,
            "ozone_per_feed": day.get("ozone_kgd", 0.0) / feed}


def _with_basis(scenario, index, res=None):
    """The saved diagram with the unit's current media requirement (the plan may have changed since)."""
    d = scenario.diagram_of(index)
    if not d or not d.get("nodes"):
        return d
    basis = _media_basis(scenario, index, res)
    return {**d, "basis": basis} if basis else d


def _capex_data(scenario, res):
    """CAPEX lines (from diagrams or Equipment selections), other items and totals."""
    saved = scenario.capex or {}
    units, _occ, _water = _equipment_units(scenario, res)
    overrides = saved.get("price_overrides") or {}
    standards = {s.pk: s for s in EquipmentStandard.objects.all()}
    lines, missing = [], 0
    for u in units:
        # a RAS unit with a process diagram is costed from the diagram, otherwise from Equipment
        source = (_diagram_lines(scenario, u["index"], u["stage"].name, standards)
                  if _has_diagram(scenario, u["index"]) else u["lines"])
        u["capex_source"] = "diagram" if _has_diagram(scenario, u["index"]) else "equipment"
        for ln in source:
            ln["capex_source"] = u["capex_source"]
            price = overrides.get(ln["key"], ln["unit_price"])
            ln.update(price=price, overridden=ln["key"] in overrides,
                      total=price * ln["qty"] if price is not None else None)
            missing += price is None
            lines.append(ln)
    equipment_total = sum(ln["total"] or 0 for ln in lines)

    extras = []
    for x in saved.get("extras") or []:
        if x.get("mode") == "percent":
            total = equipment_total * (x.get("percent") or 0) / 100
        else:
            total = (x.get("qty") or 0) * (x.get("unit_price") or 0)
        extras.append({**x, "total": total})

    by_group = {"Equipment": equipment_total}
    for x in extras:
        by_group[x["group"]] = by_group.get(x["group"], 0) + x["total"]
    grand_total = sum(by_group.values())
    by_stage = {}
    for ln in lines:
        by_stage[ln["stage"]] = by_stage.get(ln["stage"], 0) + (ln["total"] or 0)
    top = max(by_group.values()) if by_group and max(by_group.values()) > 0 else 1
    annual = res["annual_harvest_t"]
    return {
        "lines": lines, "missing": missing, "equipment_total": equipment_total, "extras": extras,
        "extras_total": sum(x["total"] for x in extras),
        # percent extras ride on the priced lines only: say so when some lines have no price
        "percent_missing": missing if any(x["mode"] == "percent" for x in extras) else 0,
        "by_group": [(g, v, v / top * 100) for g, v in by_group.items()],
        "by_stage": list(by_stage.items()), "grand_total": grand_total,
        "annual_harvest_t": annual, "per_tonne": grand_total / annual if annual else None,
        "tank_m3": sum(u["tank"].get("installed_total_m3") or 0 for u in units if "tank" in u),
        "currency": saved.get("currency", "EUR"),
    }
