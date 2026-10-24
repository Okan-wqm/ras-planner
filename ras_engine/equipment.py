"""Equipment selection: compare what the plan requires with standard equipment the user picks."""
import math

# (key, label, capacity unit)
CATEGORIES = [
    ("tank", "Tank", "m³"),
    ("biofilter", "Biofilter", "m³"),
    ("biomedia", "Biomedia (plastic carrier)", "m³"),
    ("pump", "Recirculation pump", "m³/h"),
    ("drum_filter", "Drum filter", "m³/h"),
    ("degasser", "CO₂ degasser", "m³/h"),
    ("oxygen", "Oxygen supply", "kg O₂/day"),
    ("ozone", "Ozone generator", "g O₃/h"),
    ("uv", "UV unit", "m³/h"),
]
# sized on the Equipment page; the extra ones below only appear in standards and process diagrams
SIZED_CATEGORIES = list(CATEGORIES)
CATEGORIES += [
    ("rfs", "Radial flow settler", "m³/h"),
    ("incubator", "Hatchery incubator", "trays"),
    ("sand_filter", "Sand filter", "m³/h"),
    ("sump", "Sump", "m³"),
    ("reservoir", "Water reservoir", "m³"),
    ("chiller", "Chiller", "kW cooling"),
    ("heat_exchanger", "Heat exchanger", "kW"),
    ("other", "Other", "unit"),
]
CATEGORY_LABEL = {k: label for k, label, _ in CATEGORIES}
CATEGORY_UNIT = {k: unit for k, _, unit in CATEGORIES}


def stage_requirements(res: dict, index: int, water: str = "fresh") -> dict:
    """What one RAS unit requires over the plan (peaks). water: 'fresh' or 'salt' biofilter basis."""
    design = res["designs"][index]
    if design["empty"]:
        return {"empty": True}
    curve, stage_of_age = res["curve"], res["stage_of_age"]
    ages = [a for a, k in enumerate(stage_of_age) if k == index]
    batch_peak = max((curve[a] for a in ages), key=lambda r: r["volume_m3"])
    peak = design["peak"]
    bio = design.get("biofilter") or {}
    feasible = bool(bio.get("feasible"))
    bio_volume = bio.get("volume_fresh_m3" if water == "fresh" else "volume_salt_m3") if feasible else None
    media_volume = bio.get("media_fresh_m3" if water == "fresh" else "media_salt_m3") if feasible else None
    return {
        "empty": False,
        "batch_peak_volume_m3": batch_peak["volume_m3"],
        "batch_peak_biomass_kg": batch_peak["biomass_kg"],
        "max_density": batch_peak["density"],
        "max_batches": peak["batches"],
        "total_peak_volume_m3": peak["volume_m3"],
        "biofilter_feasible": bool(bio.get("feasible")),
        "pure_oxygen": design["pure_oxygen"],
        # required capacity per category, in that category's unit
        "required": {
            "biofilter": bio_volume,
            "biomedia": media_volume,  # plastic carrier volume = biofilter volume × fill ratio
            # the plan's peak day; everything follows the feed (set through the FCR)
            "pump": max(design.get("q_unit_m3h") or 0.0, peak["q_design_m3h"]),
            "drum_filter": max(design.get("q_unit_m3h") or 0.0, peak["q_design_m3h"]),
            "degasser": peak["q_co2_m3h"],
            "oxygen": peak["pure_o2_kgd"] if design["pure_oxygen"] else 0.0,
            "ozone": peak["ozone_kgd"] * 1000 / 24,
            "uv": max(design.get("q_unit_m3h") or 0.0, peak["q_design_m3h"]),
        },
    }


def tanks_for(steps: list, weight_g: float) -> int:
    """steps: [(up_to_weight_g or None, tanks), ...] sorted, last one open-ended."""
    for up_to, k in steps:
        if up_to is None or weight_g < up_to:
            return k
    return steps[-1][1]


def normalize_steps(steps) -> list:
    """Clean user steps: drop empty ones, sort by weight, make the last one open-ended."""
    clean = [(None if u in (None, "") else float(u), int(k)) for u, k in steps or [] if k]
    clean = [s for s in clean if s[1] > 0]
    clean.sort(key=lambda s: math.inf if s[0] is None else s[0])
    if clean:
        clean[-1] = (None, clean[-1][1])
    return clean


def _stage_entries(res, index):
    """Per batch in this unit: first day and, per day, start weight, volume and biomass."""
    curve, out = res["curve"], []
    for b in res["batches"]:
        seg = next((s for s in b["segments"] if s["stage"] == index), None)
        if seg:
            rows = [curve[t - b["stock_day"]] for t in range(seg["start"], seg["end"] + 1)]
            out.append({"b": f"B{b['batch']}", "start": seg["start"], "rows": rows})
    return out


def allocate_tanks(res: dict, index: int, steps: list, rest_days: int = 0) -> dict:
    """Tanks are pooled within a RAS unit. Each day a batch holds tanks_for(start weight) tanks;
    when it needs more (grading) it takes the lowest free tanks.
    rest_days: when a batch leaves (harvest or transfer day), its tanks are emptied and cleaned —
    0 = refilled the same day (another batch can take them that day), n = kept empty (dry) for n whole
    days and restocked the day after. Returns the number of tanks needed and every batch's allocations."""
    entries = _stage_entries(res, index)
    rest_days = max(int(rest_days or 0), 0)
    held, free, n_tanks, events = {}, [], 0, []
    by_day, busy_until, entry_of = {}, {}, {}
    for e in entries:
        last = e["start"] + len(e["rows"]) - 1
        # the tanks are busy through the day before the batch leaves (same-day turnaround) or through the dry days
        busy_until[id(e)] = last - 1 if rest_days == 0 else last + rest_days
        entry_of[id(e)] = e
        for i, r in enumerate(e["rows"]):
            if rest_days == 0 and i == len(e["rows"]) - 1 and i > 0:
                continue  # leaving day: its tanks are emptied and taken by the next batch
            by_day.setdefault(e["start"] + i, []).append((e, i, r))
    days = sorted(set(by_day) | {d + 1 for d in busy_until.values()})
    for day in days:
        for key in [k for k in held if busy_until[k] < day]:  # emptied (and dried) tanks are free again
            free.extend(t for t, _ in held.pop(key))
        free.sort()
        for e, i, r in by_day.get(day, []):
            need = tanks_for(steps, r["start_weight_g"])
            mine = held.setdefault(id(e), [])
            while len(mine) < need:
                if free:
                    tank = free.pop(0)
                else:
                    tank, n_tanks = n_tanks, n_tanks + 1
                mine.append((tank, day))
                events.append((e, tank, day))
    allocs = {}
    for e, tank, day in events:
        allocs.setdefault(id(e), []).append([tank, day])
    batches = [{"b": e["b"], "start": e["start"],
                "vols": [round(r["volume_m3"], 3) for r in e["rows"]],
                "ks": [tanks_for(steps, r["start_weight_g"]) for r in e["rows"]],
                "allocs": allocs.get(id(e), [])} for e in entries]
    return {"n_tanks": n_tanks, "batches": batches}


def size_tanks(req: dict, res: dict, index: int, tank_volume: float | None, steps: list, rest_days: int = 0) -> dict:
    """Check tank steps for one RAS unit.

    - steps given, no tank size  -> required tank size = worst of (step peak volume / tanks)
    - tank size given, no steps  -> one step: ceil(batch peak volume / tank size) tanks
    - both given                 -> check every step
    Total tanks = most tanks in use on any day (tanks are pooled between batches)."""
    steps = normalize_steps(steps)
    out = {"tank_volume": tank_volume, "auto_steps": False, "steps": []}
    if not steps and tank_volume:
        steps = [(None, max(1, math.ceil(req["batch_peak_volume_m3"] / tank_volume - 1e-9)))]
        out["auto_steps"] = True
    out["step_input"] = steps
    if not steps:
        return out
    entries = _stage_entries(res, index)
    lower = 0.0
    for up_to, k in steps:
        rows = [r for e in entries for r in e["rows"]
                if r["start_weight_g"] >= lower and (up_to is None or r["start_weight_g"] < up_to)]
        step = {"from": lower, "up_to": up_to, "tanks": k, "used": bool(rows)}
        if rows:
            peak = max(rows, key=lambda r: r["volume_m3"])
            step.update(peak_volume=peak["volume_m3"], required_tank_volume=peak["volume_m3"] / k)
            if tank_volume:
                step.update(margin_pct=(k * tank_volume / peak["volume_m3"] - 1) * 100,
                            density=peak["biomass_kg"] / (k * tank_volume),
                            ok=k * tank_volume >= peak["volume_m3"] - 1e-9)
        out["steps"].append(step)
        lower = up_to if up_to is not None else lower
    used = [s for s in out["steps"] if s["used"]]
    out["required_tank_volume"] = max(s["required_tank_volume"] for s in used) if used else None
    alloc = allocate_tanks(res, index, steps, rest_days)
    out["total_tanks"] = alloc["n_tanks"]
    out["max_per_batch"] = max(k for _, k in steps)
    out["allocation"] = alloc
    if tank_volume and used:
        out["installed_total_m3"] = out["total_tanks"] * tank_volume
        out["margin_pct"] = min(s["margin_pct"] for s in used)
        out["peak_density"] = max(s["density"] for s in used)
        out["ok"] = all(s["ok"] for s in used)
    return out


def size_item(required: float | None, capacity: float | None, qty: int | None) -> dict:
    """Generic equipment: quantity defaults to ceil(required / capacity)."""
    out = {"required": required, "capacity": capacity, "qty": qty, "auto_qty": False}
    if required is None:
        return out
    if required <= 0:
        out["not_needed"] = True
        return out
    if capacity and not qty:
        out["qty"] = max(1, math.ceil(required / capacity - 1e-9))
        out["auto_qty"] = True
    if capacity and out["qty"]:
        installed = capacity * out["qty"]
        out["installed"] = installed
        out["margin_pct"] = (installed / required - 1) * 100
        out["ok"] = installed >= required - 1e-9
    return out
