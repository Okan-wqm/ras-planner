"""Targets from the production plan (step 04) against what a drawn process diagram does (step 05).

The plan gives each RAS unit's peak loads and required flows; the diagram decides where the water goes
and how much each piece of equipment treats. Every row is a check with a status:
'ok' (meets the target), 'warn' (close, or something to look at) or 'alarm' (falls short).
"""
from .process import COMPONENT_LABEL, COMPONENT_UNIT, LIMIT_TOL


def unit_targets(design: dict, water: str = "fresh") -> dict:
    """What the plan asks of one RAS unit at its busiest day (everything follows the feed)."""
    if design.get("empty"):
        return {}
    peak = design["peak"]
    bio = design.get("biofilter") or {}
    media = bio.get("media_fresh_m3" if water == "fresh" else "media_salt_m3") if bio.get("feasible") else None
    q = peak["q_design_m3h"]
    return {
        # the unit's recirculation: the loads' design flow, or more when the tank retention or the biofilter asks
        "design_flow_m3h": max(design.get("q_unit_m3h") or 0.0, q) if q != float("inf") else None,
        "flow_set_by": design.get("flow_set_by"),
        "tan_kgd": peak["tan_kgd"], "tss_kgd": peak["tss_kgd"], "o2_fish_kgd": peak["o2_kgd"],
        "co2_kgd": peak["co2_kgd"],
        # FLOW PHILOSOPHY: the diagram works at the CALCULATED level (minimum flow).
        # The design envelope sizes equipment (cone capacity on the equipment page),
        # but the compare checks that the loop meets the calculated need.
        # what the cone actually supplies: fish + biofilter O2, at the diffuser efficiency
        # (design["o2_diffuser_eff"] comes from gas.diffuser_efficiency; 0.95 keeps old plans)
        "pure_o2_kgd": (peak["o2_total_kgd"] / design.get("o2_diffuser_eff", 0.95)
                        if design.get("pure_oxygen") and peak.get("o2_total_kgd") else None),
        "o2_diffuser_eff": design.get("o2_diffuser_eff", 0.95),
        "makeup_m3h": peak["q_no3_m3d"] / 24 if peak["q_no3_m3d"] != float("inf") else None,
        "media_m3": media,
        "fill_pct": design["biofilter_params"].fill_ratio_pct if design.get("biofilter_params") else None,
    }


def _status(actual, target, warn_below=1.0, alarm_below=0.9):
    if target is None or actual is None:
        return "none"
    if target <= 0:
        return "ok"
    r = actual / target
    return "ok" if r >= warn_below else "warn" if r >= alarm_below else "alarm"


def compare(targets: dict, results: dict) -> list[dict]:
    """Rows of (check, target, drawn, unit, status, note) for the process page and the proposal."""
    if not targets or not results:
        return []
    t, tot = targets, results.get("totals") or {}
    load = tot.get("fish_load") or {}
    rows = []

    if results.get("converged") is False:  # everything below is built on numbers that did not settle
        rows.append({"check": "Solver convergence", "target": "settled", "actual": "did not settle", "unit": "",
                     "status": "alarm", "note": "concentrations did not settle — other rows unreliable"})

    def row(check, target, actual, unit, status, note=""):
        rows.append({"check": check, "target": target, "actual": actual, "unit": unit, "status": status, "note": note})

    # the loads drawn on the tanks should cover the plan's peak
    for key, label in (("tan_kgd", "TAN load on the tanks"), ("tss_kgd", "TSS load on the tanks"),
                       ("o2_fish_kgd", "Fish O₂ use on the tanks")):
        drawn = load.get("o2_kgd" if key == "o2_fish_kgd" else key, 0.0)
        row(label, t[key], drawn, "kg/day", _status(drawn, t[key], 0.98, 0.9),
            "plan's busiest day")
    q = tot.get("recirculated_m3h")
    row("Recirculated flow", t["design_flow_m3h"], q, "m³/h",
        _status(q, t["design_flow_m3h"], warn_below=0.995),  # 0.5% rounding tolerance
        {"biofilter": "the biofilter's max. removal per pass sets it", "retention": "tank retention time sets it"}
        .get(t.get("flow_set_by"), "the tank loads (O₂, TAN, CO₂, TSS) set it"))
    # treatment performance on the drawn load
    produced_tan = load.get("tan_kgd", 0.0)
    nitrified = tot.get("tan_nitrified_kgd", 0.0)
    row("TAN nitrified", produced_tan, nitrified, "kg/day", _status(nitrified, produced_tan, 0.9, 0.75),
        "the rest leaves with drain, sludge or discharge water")
    produced_tss = load.get("tss_kgd", 0.0)
    removed = tot.get("tss_removed_kgd", 0.0)
    row("TSS removed", produced_tss, removed, "kg/day", _status(removed, produced_tss, 0.9, 0.75),
        f"to sludge: {tot.get('sludge_m3h', 0.0):.2f} m³/h carrying {tot.get('sludge_tss_kgd', 0.0):.1f} kg/day")
    co2_in = load.get("co2_kgd", 0.0)  # CO₂ from the fish on the tanks; nitrification adds more
    stripped = tot.get("co2_stripped_kgd", 0.0)
    row("CO₂ stripped", co2_in, stripped, "kg/day", _status(stripped, co2_in, 0.9, 0.75),
        "fish CO₂ drawn on the tanks; nitrification adds more")
    if t.get("pure_o2_kgd") is not None:
        pure = tot.get("pure_o2_kgd", 0.0)
        # the two sides measure different things: the plan sizes pure O₂ for the fish (÷ diffuser
        # efficiency from the constants), the diagram doses gas for every consumer through the
        # equipment's own transfer efficiency — a mismatch is worth a look, not a hard fail
        eff = t.get("o2_diffuser_eff", 0.95)
        status, note = _status(pure, t["pure_o2_kgd"]), "oxygen equipment"
        if t["pure_o2_kgd"] > 0 and not 0.9 <= pure / t["pure_o2_kgd"] <= 1.1:
            status = "warn"
            note = (f"plan (fish-only O₂, η={eff:g}) vs diagram (all consumers, own efficiency) differ by "
                    f"{abs(pure / t['pure_o2_kgd'] - 1) * 100:.0f}%")
        row("Pure oxygen dissolved", t["pure_o2_kgd"], pure, "kg/day", status, note)
    makeup = results.get("makeup_m3h")
    if t.get("makeup_m3h") is not None:
        row("Make-up water", t["makeup_m3h"], makeup, "m³/h", _status(makeup, t["makeup_m3h"], 1.0, 0.8),
            "needed to hold NO₃ at its limit")
    if t.get("media_m3"):
        media = tot.get("media_m3", 0.0)
        row("Biofilter media", t["media_m3"], media, "m³", _status(media, t["media_m3"], warn_below=0.995),
            "media volume set on the biofilters" if media else "set the media volume on the biofilter")
    # tank water against the limits
    limits = results.get("limits") or {}
    tanks = tot.get("tanks") or {}
    for c, lim in limits.items():
        if c not in COMPONENT_LABEL or not tanks:
            continue
        values = [tk["c"][c] for tk in tanks.values()]
        if "max" in lim and lim["max"] is not None:
            worst = max(values)
            row(f"Tank {COMPONENT_LABEL[c]} (worst tank)", lim["max"], worst, COMPONENT_UNIT[c],
                "ok" if worst <= lim["max"] + LIMIT_TOL else "alarm", "maximum")
        if "min" in lim and lim["min"] is not None:
            worst = min(values)
            row(f"Tank {COMPONENT_LABEL[c]} (worst tank)", lim["min"], worst, COMPONENT_UNIT[c],
                "ok" if worst >= lim["min"] - LIMIT_TOL else "alarm", "minimum")
    return rows


def summary_status(rows: list[dict]) -> str:
    states = {r["status"] for r in rows}
    return "alarm" if "alarm" in states else "warn" if "warn" in states else "ok" if rows else "none"
