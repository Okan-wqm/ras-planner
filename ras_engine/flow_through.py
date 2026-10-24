"""Flow-through (akarsu) system sizing: dilution-based instead of treatment-based.

In a flow-through system there is NO recirculation — fresh water enters, passes through
the tank (optionally via an oxygenator), and exits. Water quality is maintained purely by
dilution: the make-up flow must be large enough that the fish loads are diluted below the
tank limits. This module calculates the required make-up flow for each parameter and the
total flow-through requirement.
"""
from .formulas import o2_saturation


def flow_through_needs(peak: dict, fp, gp) -> dict:
    """Required make-up water flow for a flow-through (single-pass) system.

    peak: the plan's peak-day values (feed_kg, tan_kgd, co2_kgd, tss_kgd, o2_kgd).
    fp: FlowParams (tan_max, co2_max, tss_max, removal effs).
    gp: GasParams (o2_per_feed, salinity, inlet_o2_saturation, tank_outlet_saturation).

    Returns a dict with per-parameter flows (m³/h) and the total flow-through requirement.
    """
    feed = peak.get("feed_kg") or 0.0
    tan_kgd = peak.get("tan_kgd") or 0.0
    co2_kgd = peak.get("co2_kgd") or 0.0
    tss_kgd = peak.get("tss_kgd") or 0.0
    o2_fish_kgd = peak.get("o2_kgd") or 0.0
    temp = peak.get("temperature") or 14.0

    if feed <= 0:
        return {"total_m3h": 0, "limiting": None, "flows": {}, "note": "no feed load"}

    csat = o2_saturation(temp, gp.salinity)

    # Inlet water: fresh source (TAN=0, TSS=0, CO₂≈2 mg/L from groundwater/reservoir)
    tan_inlet = 0.0
    co2_inlet = 2.0
    tss_inlet = 0.0

    # O₂ inlet depends on whether an oxygenator (jet/cone) is used
    # Without: inlet = saturation (aeration only)
    # With jet/cone: inlet = csat × inlet_o2_saturation (e.g. 1.4 × sat ≈ 14 mg/L)
    o2_in_with_oxy = csat * gp.inlet_o2_saturation
    o2_in_no_oxy = csat  # plain aeration can't exceed saturation
    o2_out = csat * gp.tank_outlet_saturation

    # Per-parameter dilution flows (m³/h): Q = load / (ΔC × 24)
    # (loads are kg/day, concentrations are g/m³ = mg/L; Q = kg/d × 10⁶ / (mg/L × 24) / 10³
    #  = kg/d × 1000 / (mg/L × 24) = kg/d / (mg/L × 0.024))

    flows = {}

    # TAN: must dilute below tan_max
    dc_tan = fp.tan_max - tan_inlet
    if dc_tan > 0:
        flows["tan"] = tan_kgd / (dc_tan * 0.024)
    else:
        flows["tan"] = float("inf")

    # CO₂: dilute below co2_max (no degasser — fresh water CO₂ is low)
    dc_co2 = fp.co2_max - co2_inlet
    if dc_co2 > 0:
        flows["co2"] = co2_kgd / (dc_co2 * 0.024)
    else:
        flows["co2"] = float("inf")

    # TSS: dilute below tss_max (no drum filter — all solids go with the water)
    dc_tss = fp.tss_max - tss_inlet
    if dc_tss > 0:
        flows["tss"] = tss_kgd / (dc_tss * 0.024)
    else:
        flows["tss"] = float("inf")

    # O₂: must supply enough oxygen between inlet and outlet
    # Total O₂ = fish + nitrification (if any TAN is nitrified in-tank, but flow-through
    # has no biofilter, so only fish O₂)
    o2_need_kgd = o2_fish_kgd
    dc_o2_with = o2_in_with_oxy - o2_out  # with oxygenator
    dc_o2_without = o2_in_no_oxy - o2_out  # plain aeration only

    if dc_o2_with > 0:
        flows["o2_with_oxy"] = o2_need_kgd / (dc_o2_with * 0.024)
    else:
        flows["o2_with_oxy"] = float("inf")

    if dc_o2_without > 0:
        flows["o2_no_oxy"] = o2_need_kgd / (dc_o2_without * 0.024)
    else:
        flows["o2_no_oxy"] = float("inf")

    # Total flow-through: the MAXIMUM of all dilution flows
    # (without O₂ oxygenator vs with — show both)
    finite = [v for v in flows.values() if v != float("inf")]
    total_no_oxy = max([flows.get("o2_no_oxy", 0)] + [flows[k] for k in ("tan", "co2", "tss")])
    total_with_oxy = max([flows.get("o2_with_oxy", 0)] + [flows[k] for k in ("tan", "co2", "tss")])

    # Which parameter is limiting?
    all_flows_no = {"tan": flows.get("tan", 0), "co2": flows.get("co2", 0),
                    "tss": flows.get("tss", 0), "o2": flows.get("o2_no_oxy", 0)}
    all_flows_with = {"tan": flows.get("tan", 0), "co2": flows.get("co2", 0),
                      "tss": flows.get("tss", 0), "o2": flows.get("o2_with_oxy", 0)}
    limiting_no = max(all_flows_no, key=all_flows_no.get)
    limiting_with = max(all_flows_with, key=all_flows_with.get)

    return {
        "total_no_oxy_m3h": total_no_oxy,
        "total_with_oxy_m3h": total_with_oxy,
        "limiting_no_oxy": limiting_no,
        "limiting_with_oxy": limiting_with,
        "flows": flows,
        "o2_inlet_no_oxy_mgl": o2_in_no_oxy,
        "o2_inlet_with_oxy_mgl": o2_in_with_oxy,
        "o2_outlet_mgl": o2_out,
        "csat_mgl": csat,
        "water_per_feed_l": total_with_oxy * 24 * 1000 / feed if feed > 0 else 0,
        "comparison": "flow-through vs RAS: RAS recirculates ~{} m³/h with {} m³/h make-up; "
                      "flow-through needs {} m³/h ALL fresh ({}× more)".format(
                          round(peak.get("q_design_m3h", 0)),
                          round(peak.get("q_no3_m3d", 0) / 24, 1),
                          round(total_with_oxy),
                          round(total_with_oxy / max(peak.get("q_no3_m3d", 1) / 24, 0.1), 0)),
    }
