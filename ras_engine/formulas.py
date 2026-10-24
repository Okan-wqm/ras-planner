"""Individual formulas. Letters in the docstrings are the 'D. Fry' sheet columns."""
import math

from . import EngineInputError

# ---------------------------------------------------------------- growth


def length_from_weight(weight_g, cf):
    """J9: L = (W·100 / CF)^(1/3)"""
    return (weight_g * 100 / cf) ** (1 / 3)


def weight_from_length(length_cm, cf):
    """I: W = CF · L³ / 100"""
    return cf * length_cm ** 3 / 100


def daily_length_increase(temp, t_base, tu_base):
    """H: ΔL = ((T − Tbase) / Tubase) / 30.5   [cm/day]  — held at zero below Tbase: fish do not shrink"""
    return max(((temp - t_base) / tu_base) / 30.5, 0.0)


def sgr(w_today, w_yesterday, days=1):
    """E: SGR = (ln W − ln W_prev) / Δdays"""
    return (math.log(w_today) - math.log(w_yesterday)) / days


def stocking_density(length_cm, divisor, max_density):
    """N: D = L / 0.32, held at the maximum density once reached."""
    return min(length_cm / divisor, max_density)


def daily_feed(count, w_today, w_yesterday, fcr):
    """Q: feed = N · (W − W_prev) · FCR / 1000   [kg/day]  — never negative (growth is clamped at zero)"""
    return max(count * (w_today - w_yesterday) * fcr / 1000, 0.0)

# ---------------------------------------------------------------- gases


def o2_saturation(temp, salinity):
    """Benson & Krause dissolved O₂ saturation [mg/L] (the EXP term inside AJ/AK)."""
    tk = temp + 273.15
    return math.exp(
        -138.7421
        + 1.572288e5 / tk
        - 6.637149e7 / tk ** 2
        + 1.243678e10 / tk ** 3
        - 8.621061e11 / tk ** 4
        - salinity * (0.017674 - 10.754 / tk + 2140.7 / tk ** 2)
    )


def gas_lph(kg_per_hour, molar_mass, molar_volume=22.4):
    """W / AA: kg/h → L/h (ideal gas)."""
    return kg_per_hour * 1000 * molar_volume / molar_mass

# ---------------------------------------------------------------- flows (mass balance)


def _pos(v, msg):
    """A flow denominator factor: zero or negative user input is a mistake, not a limit case."""
    if v <= 0:
        raise EngineInputError(msg)
    return v


def flow_oxygen_m3h(o2_kg_day, sat_in, sat_out, transfer_eff):
    """AL: Q = O₂ / (η · (C_in − C_out))   [m³/h]"""
    _pos(transfer_eff, "Oxygen transfer efficiency must be positive")
    return o2_kg_day * 1e6 / (transfer_eff * (sat_in - sat_out)) / 24 / 1000


def flow_tan_m3h(feed_kg, protein, tan_per_protein, removal_eff, tan_max, tan_best):
    """AN: Q = TAN / (C_max − C_after_biofilter)   [m³/h]"""
    _pos(removal_eff, "TAN removal efficiency must be positive")
    _pos(tan_max - tan_best, "TAN target must be below the tank limit")
    tan_g = feed_kg * protein * tan_per_protein * 1e6
    c_after = tan_max + removal_eff * (tan_best - tan_max)
    return tan_g / (tan_max - c_after) / 24 / 1000


def flow_co2_lpm(co2_kg_day, removal_eff, co2_max, co2_best):
    """AQ: Q = CO₂ / (C_max − C_after_degasser)   [L/min]"""
    _pos(removal_eff, "CO₂ removal efficiency must be positive")
    _pos(co2_max - co2_best, "CO₂ target must be below the tank limit")
    c_after = co2_max + removal_eff * (co2_best - co2_max)
    return co2_kg_day * 1e6 / (co2_max - c_after) / 1440


def flow_tss_lpm(feed_kg, tss_per_feed, removal_eff, tss_max, tss_best):
    """AS: Q = TSS / (C_max − C_after_filter)   [L/min]"""
    _pos(removal_eff, "TSS removal efficiency must be positive")
    _pos(tss_max - tss_best, "TSS target must be below the tank limit")
    c_after = tss_max + removal_eff * (tss_best - tss_max)
    return feed_kg * tss_per_feed * 1e6 / (tss_max - c_after) / 1440


def flow_nitrate_m3d(feed_kg, protein, tan_per_protein, assimilation, denitrification_pct,
                     no3_max, no3_inlet):
    """AT: water exchange needed to hold NO₃-N at its limit   [m³/day]"""
    _pos(no3_max - no3_inlet,
         f"Make-up water nitrate ({no3_inlet:g}) is at or above the tank limit ({no3_max:g}): "
         "no water exchange can hold it")
    n_g = protein * tan_per_protein * feed_kg * 1000
    n_mg = (n_g - n_g * assimilation) * 1000 * (1 - denitrification_pct / 100)
    lpm = n_mg / (1440 * (no3_max - no3_inlet))
    return lpm * 60 / 1000 * 24
