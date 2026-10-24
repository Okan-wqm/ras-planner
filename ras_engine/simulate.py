"""Day-by-day production simulation (the 'D. Fry' sheet)."""
from . import EngineInputError, formulas as fx
from .feed import Feed, select_feed
from .params import Params, parse_steps, step_value, step_value_interp

O2, CO2, O3 = 32, 44, 48  # molar masses


def _pick(overrides, key, day, default):
    values = (overrides or {}).get(key)
    if values and day - 1 < len(values) and values[day - 1] is not None:
        return values[day - 1]
    return default


def params_for_weight(stage_params, weight_g: float, default: Params) -> Params:
    """stage_params: [(exit weight, Params), ...] — constants of the RAS unit the weight falls in."""
    if not stage_params:
        return default
    for max_w, p in stage_params:
        if weight_g < max_w:
            return p
    return stage_params[-1][1]


def simulate(params: Params, feeds: list[Feed] | None = None,
             temperature_steps: str = "", overrides: dict | None = None,
             stage_params: list[tuple[float, Params]] | None = None,
             until_weight_g: float | None = None) -> list[dict]:
    """Return one row per day.

    temperature_steps: stepped temperature such as '1:12, 100:13' (empty: the unit's constant temperature).
    overrides: per-day lists that replace inputs
               {'temperature': [...], 'protein': [...], 'fcr': [...], 'inlet_o2_saturation': [...]}.
    stage_params: per-unit constants; each day uses the constants of the unit the fish are in,
                  by start-of-day weight (growth and mass balance).
                  Stocking count/weight and day count always come from `params`.
    until_weight_g: stop after the first day whose end weight reaches it (None = simulate every day).
                    Same cut as growth_curve's trim: that day included, so the rows are identical.
    Day-1 feed: there is no previous day to compute it from, so day 1 copies day 2's feed
    (Excel: Q9 = Q10); a run of a single day gets 0 kg. The copy feeds day 1's mass balance too.
    """
    g0 = params.growth
    if g0.initial_count <= 0:
        raise EngineInputError("Fish stocked per batch must be above zero.")
    if g0.initial_weight_g <= 0:
        raise EngineInputError("Stocking weight must be above zero.")
    if g0.condition_factor <= 0:
        raise EngineInputError("Condition factor (CF) must be above zero.")
    if g0.tu_base <= 0:
        raise EngineInputError("Tubase must be above zero.")
    if g0.density_length_divisor <= 0:
        raise EngineInputError("Density coefficient (L / x) must be above zero.")
    if g0.max_density <= 0:
        raise EngineInputError("Maximum stocking density must be above zero.")
    temp_steps = parse_steps(temperature_steps) if temperature_steps else []

    rows, day_params = [], []
    for day in range(1, int(params.growth.days) + 1):
        prev = rows[-1] if rows else None
        start_weight = prev["weight_g"] if prev else params.growth.initial_weight_g
        p_day = params_for_weight(stage_params, start_weight, params)
        g, fp, gas = p_day.growth, p_day.feed, p_day.gas
        day_params.append(p_day)
        temp = _pick(overrides, "temperature", day, step_value_interp(temp_steps, day, g.temperature))

        # --- growth
        if prev is None:
            weight = params.growth.initial_weight_g
            length = fx.length_from_weight(weight, g.condition_factor)
            length_inc = None
            count = params.growth.initial_count
        elif g.use_tgc:  # thermal growth coefficient: W^(1/3) += TGC/1000 · T
            weight = (prev["weight_g"] ** (1 / 3) + g.tgc / 1000 * max(temp - g.t_base, 0.0)) ** 3
            length = fx.length_from_weight(weight, g.condition_factor)
            length_inc = length - prev["length_cm"]
            count = prev["count"] * (1 - g.daily_mortality)
        else:
            length_inc = fx.daily_length_increase(temp, g.t_base, g.tu_base)
            length = prev["length_cm"] + length_inc
            weight = fx.weight_from_length(length, g.condition_factor)
            count = prev["count"] * (1 - g.daily_mortality)

        biomass = count * weight / 1000
        density = fx.stocking_density(length, g.density_length_divisor, g.max_density)

        # --- feed (picked by start-of-day weight)
        feed = select_feed(feeds, start_weight) if feeds else None
        if feeds and feed is None:  # an active catalog must cover the whole weight range
            ranges = ", ".join(f"{f.min_weight_g:g}–{f.max_weight_g:g}"
                               for f in sorted(feeds, key=lambda f: f.min_weight_g))
            raise EngineInputError(
                f"No feed in the catalog covers {start_weight:g} g — extend the feed table "
                f"(ranges: {ranges} g)")  # listing every range: the gap may be inside the table
        fcr = _pick(overrides, "fcr", day, feed.fcr if feed else fp.default_fcr)
        protein = _pick(overrides, "protein", day, feed.protein if feed else fp.default_protein)

        rows.append({
            "day": day, "temperature": temp, "length_inc": length_inc, "start_weight_g": start_weight,
            "length_cm": length, "weight_g": weight, "count": count,
            "biomass_kg": biomass, "biomass_t": biomass / 1000,
            "density": density, "volume_m3": biomass / density,
            "fcr": fcr, "protein": protein, "feed_name": feed.name if feed else "",
            "sgr": fx.sgr(weight, prev["weight_g"]) if prev else None,
            "feed_kg": (fx.daily_feed(count, weight, prev["weight_g"], fcr) if prev else None),
            "inlet_o2_saturation": _pick(overrides, "inlet_o2_saturation", day, gas.inlet_o2_saturation),
        })
        if until_weight_g is not None and weight >= until_weight_g:
            break  # growth_curve's trim rule applied as an early exit: this day included

    # In Excel day 1 feed equals day 2 feed (Q9 = Q10)
    if len(rows) > 1:
        rows[0]["feed_kg"] = rows[1]["feed_kg"]
    elif rows:
        rows[0]["feed_kg"] = 0.0

    cumulative = 0.0
    for r, p_day in zip(rows, day_params):
        _mass_balance(r, p_day)
        cumulative += r["feed_kg"]
        r["feed_cum_t"] = cumulative / 1000
    return rows


def _mass_balance(r: dict, params: Params) -> None:
    gas, fl = params.gas, params.flow
    feed, protein = r["feed_kg"], r["protein"]
    r["feed_rate"] = feed / r["biomass_kg"]

    r["tan_kgd"] = feed * protein * fl.tan_per_protein

    # oxygen: the fish (sets the tank flow) and nitrification in the biofilter (all TAN produced is nitrified)
    r["o2_kgh"] = feed * gas.o2_per_feed / 24
    r["o2_kgd"] = r["o2_kgh"] * 24
    r["o2_bio_kgd"] = r["tan_kgd"] * gas.o2_per_tan
    r["o2_total_kgd"] = r["o2_kgd"] + r["o2_bio_kgd"]
    r["o2_lph"] = fx.gas_lph(r["o2_kgh"], O2, gas.molar_volume)
    r["o2_lpm"] = r["o2_lph"] / 60
    # pure O₂ (flow meter) only when pure oxygen is dosed through a cone/diffuser
    r["pure_oxygen"] = gas.pure_oxygen
    pure_need = r["o2_kgd"] + (r["o2_bio_kgd"] if gas.biofilter_o2_from_pure_o2 else 0.0)
    r["o2_lpm_actual"] = (fx.gas_lph(pure_need / 24, O2, gas.molar_volume) / 60 / gas.diffuser_efficiency
                          if gas.pure_oxygen else 0.0)
    r["pure_o2_kgd"] = pure_need / gas.diffuser_efficiency if gas.pure_oxygen else 0.0

    # carbon dioxide: fish respiration plus nitrification, both stripped by the degasser
    r["co2_bio_kgd"] = r["tan_kgd"] * gas.co2_per_tan
    r["co2_kgh"] = r["o2_kgh"] * gas.co2_per_o2 + r["co2_bio_kgd"] / 24
    r["co2_kgd"] = r["co2_kgh"] * 24
    r["co2_lph"] = fx.gas_lph(r["co2_kgh"], CO2, gas.molar_volume)
    r["co2_lpm"] = r["co2_lph"] / 60

    # ozone: lower dose for fry below the threshold weight
    dose = gas.ozone_dose_fry if r["weight_g"] < gas.ozone_weight_threshold else gas.ozone_dose
    r["ozone_dose"] = dose
    r["ozone_kgd"] = feed * dose / 1000
    r["ozone_gh"] = r["ozone_kgd"] * 1000 / 24
    r["ozone_lpm"] = r["ozone_gh"] * gas.molar_volume / O3 / 60

    # O₂ saturation
    csat = fx.o2_saturation(r["temperature"], gas.salinity)
    if gas.pure_oxygen:
        r["o2_sat_in"] = csat * r["inlet_o2_saturation"]   # supersaturated by the oxygen cone
    else:
        r["o2_sat_in"] = csat * gas.aeration_saturation  # aeration cannot exceed saturation
    r["o2_sat_out"] = csat * gas.tank_outlet_saturation

    # flows
    if r["o2_sat_in"] > r["o2_sat_out"]:
        r["q_o2_m3h"] = fx.flow_oxygen_m3h(r["o2_kgd"], r["o2_sat_in"], r["o2_sat_out"],
                                           gas.o2_transfer_efficiency)
    else:  # inlet water holds less O₂ than the outlet: no flow is enough
        r["q_o2_m3h"] = float("inf")
    r["q_o2_lpm"] = r["q_o2_m3h"] * 1000 / 60
    r["q_tan_m3h"] = fx.flow_tan_m3h(feed, protein, fl.tan_per_protein, fl.tan_removal_eff,
                                     fl.tan_max, fl.tan_best)
    r["q_tan_lpm"] = r["q_tan_m3h"] * 1000 / 60
    r["q_co2_lpm"] = fx.flow_co2_lpm(r["co2_kgd"], fl.co2_removal_eff, fl.co2_max, fl.co2_best)
    r["q_co2_m3h"] = r["q_co2_lpm"] * 60 / 1000
    r["q_tss_lpm"] = fx.flow_tss_lpm(feed, fl.tss_per_feed, fl.tss_removal_eff, fl.tss_max,
                                     fl.tss_best)
    r["q_tss_m3h"] = r["q_tss_lpm"] * 60 / 1000
    r["q_no3_m3d"] = fx.flow_nitrate_m3d(feed, protein, fl.tan_per_protein, fl.no3_assimilation,
                                         fl.denitrification_pct, fl.no3_max, fl.no3_inlet)
    r["water_per_feed"] = r["q_no3_m3d"] * 1000 / feed if feed else 0.0
    r["tss_kgd"] = feed * fl.tss_per_feed
    r["backflush60_m3d"] = feed * fl.backflush_60_l_per_kg / 1000
    r["backflush30_m3d"] = feed * fl.backflush_30_l_per_kg / 1000
    # design flow: the largest flow demand
    r["q_design_m3h"] = max(r["q_o2_m3h"], r["q_tan_m3h"], r["q_co2_m3h"], r["q_tss_m3h"])


def weekly(rows: list[dict]) -> list[dict]:
    """Like 'Sayfa10': every 7th day."""
    return [r for r in rows if r["day"] % 7 == 0]


def summary(rows: list[dict]) -> dict:
    if not rows:
        return {}
    peak = lambda k: max(r[k] for r in rows)
    feed_by_type: dict[str, float] = {}
    for r in rows:
        feed_by_type[r["feed_name"] or "Default"] = feed_by_type.get(
            r["feed_name"] or "Default", 0.0) + r["feed_kg"]
    last = rows[-1]
    gained_t = last["biomass_t"] - rows[0]["biomass_t"]
    economic_fcr = last["feed_cum_t"] / gained_t if gained_t > 0 else None
    if economic_fcr is not None and economic_fcr > 3:  # absurd ratio: the plan is degenerate
        economic_fcr = None
    return {
        "final_weight_g": last["weight_g"], "final_count": last["count"],
        "final_biomass_t": last["biomass_t"], "total_feed_t": last["feed_cum_t"],
        "peak_feed_kg": peak("feed_kg"), "peak_volume_m3": peak("volume_m3"),
        "peak_q_o2_m3h": peak("q_o2_m3h"), "peak_q_tan_m3h": peak("q_tan_m3h"),
        "peak_q_co2_m3h": peak("q_co2_m3h"), "peak_q_tss_m3h": peak("q_tss_m3h"),
        "peak_q_design_m3h": peak("q_design_m3h"), "peak_o2_kgd": peak("o2_kgd"),
        "peak_ozone_kgd": peak("ozone_kgd"), "peak_no3_m3d": peak("q_no3_m3d"),
        "peak_o2_lpm_actual": peak("o2_lpm_actual"), "peak_ozone_lpm": peak("ozone_lpm"),
        "peak_pure_o2_kgd": peak("pure_o2_kgd"),
        "economic_fcr": economic_fcr,
        "feed_by_type": feed_by_type,
    }
