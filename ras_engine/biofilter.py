"""Biofilter sizing (the 'Sayfa11' sheet). One row per tank retention time."""
import math
from dataclasses import replace

from . import EngineInputError
from .params import BiofilterParams

# The k_media rate scaling is linear in specific surface area only up to here: beyond it, thin biofilms and
# oxygen transport inside the carrier stop the rate from following the area (over-crediting high-SSA media).
# The cap is pending a transient/biofilm model; the parity fixture (1000 m²/m³) sits exactly on it, untouched.
MAX_MEDIA_AREA_M2M3 = 1000.0


def biofilter_row(bp: BiofilterParams, retention_min: float, design=None) -> dict:
    total_volume = bp.tank_volume_m3 * bp.n_tanks                        # D16
    tan_kg_day = bp.tan_per_protein * bp.protein * bp.daily_feed_kg      # D23
    tan_g_m3_h = tan_kg_day / bp.feeding_hours * 1000 / total_volume     # D25 (kp)

    q_tank = total_volume / (retention_min / 60)                         # F
    q_filter = q_tank - bp.makeup_flow_m3h                               # G
    q_bio = q_filter + bp.makeup_flow_m3h                                # H
    tan_out = (q_tank * bp.tan_target - tan_g_m3_h * total_volume) / q_tank        # I
    efficiency = q_bio * (bp.tan_target - tan_out) * 100 / (q_bio * bp.tan_target)  # J

    monod = bp.monod_max * tan_out / (tan_out + bp.monod_ks)             # L
    k_temp = math.exp(bp.temp_coefficient * (bp.temperature - 20))       # M
    # the area that actually scales the rate: linear scaling over-credits carriers above the cap
    media_area_eff = min(bp.media_area_m2m3, MAX_MEDIA_AREA_M2M3)
    k_media = media_area_eff / bp.reference_area                         # N
    rate_salt = monod * k_temp * k_media                                 # O
    rate_fresh = rate_salt * bp.freshwater_factor                        # P

    # outlet TAN ≤ 0: the flow cannot hold tank TAN at target. Removal per pass above the limit: no biofilter
    # can do that, the flow must be larger (shorter retention)
    too_much = efficiency > bp.max_removal_pct + 1e-9
    feasible = tan_out > 0 and not too_much
    if feasible:
        removed = (q_filter * bp.tan_target + bp.makeup_flow_m3h * bp.tan_inlet - q_tank * tan_out)
        media_salt, media_fresh = removed / rate_salt, removed / rate_fresh  # Q, R
        fill = bp.fill_ratio_pct / 100
        vol_salt, vol_fresh = media_salt / fill, media_fresh / fill          # S, T
        res_salt, res_fresh = vol_salt / q_bio * 60, vol_fresh / q_bio * 60  # U, V
    else:
        media_salt = media_fresh = vol_salt = vol_fresh = res_salt = res_fresh = None
    # advisory safety notes (Blancheton, Sète 2016): never let the design drift to the unsafe side
    notes = []
    if design is not None:
        if retention_min < design.biofilter_min_retention:
            notes.append(f"residence {retention_min:g} min < {design.biofilter_min_retention:g} min "
                         "design minimum — NO₂ production risk")
        if bp.tan_target < design.tan_knee:
            notes.append(f"target TAN {bp.tan_target:g} mg/L is below the ~{design.tan_knee:g} mg/L "
                         "knee — nitrification kinetics fall sharply and the media volume explodes")
    return {
        # outlet TAN ≤ 0: the flow cannot hold tank TAN at target, so volumes are meaningless
        "feasible": feasible,
        "reason": ("" if feasible else "the flow cannot keep tank TAN at the target" if tan_out <= 0
                   else f"needs {efficiency:.0f} % TAN removal per pass (max. {bp.max_removal_pct:g} %)"),
        "retention_min": retention_min, "q_tank_m3h": q_tank, "q_biofilter_m3h": q_bio,
        "tan_out": tan_out, "efficiency_pct": efficiency, "monod_rate": monod,
        "k_temp": k_temp, "k_media": k_media, "rate_salt": rate_salt, "rate_fresh": rate_fresh,
        "media_area_capped": bp.media_area_m2m3 > MAX_MEDIA_AREA_M2M3,
        "media_salt_m3": media_salt, "media_fresh_m3": media_fresh,
        "volume_salt_m3": vol_salt, "volume_fresh_m3": vol_fresh,
        "residence_salt_min": res_salt, "residence_fresh_min": res_fresh,
        "notes": notes,
    }


def max_retention_min(bp: BiofilterParams) -> float:
    """Retention time at which outlet TAN reaches zero: TAN_target − kp·t/60 = 0  →  t = 60·TAN_target / kp.
    Stay below it (in practice, well below)."""
    total_volume = bp.tank_volume_m3 * bp.n_tanks
    kp = bp.tan_per_protein * bp.protein * bp.daily_feed_kg / bp.feeding_hours * 1000 / total_volume
    return 60 * bp.tan_target / kp if kp > 0 else math.inf


def biofilter_table(bp: BiofilterParams, design=None) -> list[dict]:
    times = []
    for token in str(bp.retention_times).replace(";", ",").split(","):
        if not token.strip():
            continue
        try:
            t = float(token)
        except ValueError:
            raise EngineInputError(f"Retention times must be positive numbers separated by commas — "
                                   f"'{token.strip()}' is not valid") from None
        if t <= 0:
            raise EngineInputError("Retention times must be positive numbers separated by commas — "
                                   f"'{token.strip()}' is not valid")
        times.append(t)
    return [biofilter_row(bp, t, design) for t in times]


# Fields fed by the simulation (they replace manual values when a design weight is set)
LINKED_FIELDS = ("temperature", "protein", "daily_feed_kg", "tank_volume_m3")


def design_point(rows: list[dict], weight_g: float) -> dict:
    """First day the fish reach the weight; the last day if they never do."""
    for r in rows:
        if r["weight_g"] >= weight_g:
            return r
    return rows[-1]


def link_to_simulation(bp: BiofilterParams, rows: list[dict]) -> tuple[BiofilterParams, dict | None]:
    """When design_weight_g > 0, use that day's simulation values as biofilter inputs.

    Total required volume and feed are split across RAS units; feed is raised by the safety factor.
    """
    if not bp.design_weight_g or not rows:
        return bp, None
    day = design_point(rows, bp.design_weight_g)
    units = max(int(bp.n_ras_units), 1)
    unit_volume = day["volume_m3"] / units
    unit_feed = day["feed_kg"] / units
    linked = replace(
        bp,
        temperature=day["temperature"],
        protein=day["protein"],
        daily_feed_kg=unit_feed * bp.feed_safety_factor,
        tank_volume_m3=unit_volume / max(int(bp.n_tanks), 1),
    )
    info = {
        "reached": day["weight_g"] >= bp.design_weight_g, "day": day["day"],
        "weight_g": day["weight_g"], "count": day["count"], "biomass_kg": day["biomass_kg"],
        "density": day["density"], "temperature": day["temperature"], "protein": day["protein"],
        "feed_name": day["feed_name"], "farm_feed_kg": day["feed_kg"], "unit_feed_kg": unit_feed,
        "design_feed_kg": linked.daily_feed_kg, "farm_volume_m3": day["volume_m3"],
        "unit_volume_m3": unit_volume, "tank_volume_m3": linked.tank_volume_m3,
        "units": units, "n_tanks": int(bp.n_tanks), "q_tan_m3h": day["q_tan_m3h"] / units,
        "max_retention_min": max_retention_min(linked),
    }
    return linked, info
