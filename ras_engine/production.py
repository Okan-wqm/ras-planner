"""Multi-batch production plan: batches stocked at a fixed interval move between RAS units
by weight; each RAS unit's daily load is the sum of all batches inside it."""
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date, timedelta

from .biofilter import biofilter_row, biofilter_table, max_retention_min
from .feed import Feed
from .params import BiofilterParams, Params
from .simulate import simulate

SUM_KEYS = ("count", "biomass_kg", "volume_m3", "feed_kg", "o2_kgd", "pure_o2_kgd", "o2_lpm_actual",
            "q_o2_m3h", "q_tan_m3h", "q_co2_m3h", "q_tss_m3h", "q_design_m3h", "q_no3_m3d",
            "ozone_kgd", "ozone_lpm", "tan_kgd", "co2_kgd", "tss_kgd", "o2_bio_kgd", "o2_total_kgd", "co2_bio_kgd")


@dataclass
class RasStage:
    name: str
    max_weight_g: float      # fish move to the next unit at this weight (last unit: harvest)
    n_tanks: int = 8
    retention_min: float = 30.0
    params: Params | None = None              # constants for this unit (scenario's if None)
    biofilter: BiofilterParams | None = None  # biofilter constants for this unit
    uid: str = ""                             # stable id: diagrams, equipment and costs are saved under it


@dataclass
class PlanParams:
    start_date: date
    interval_days: int = 30
    horizon_days: int = 1095
    params: "Params | None" = None      # scenario constants (design margins, diffuser efficiency)


def growth_curve(params: Params, harvest_weight_g: float, feeds: list[Feed] | None = None,
                 temperature_steps: str = "", max_days: int = 5000,
                 stages: list[RasStage] | None = None) -> list[dict]:
    """Daily curve of one batch from stocking to harvest weight.
    With stages, fish grow with each unit's own constants while in that unit."""
    p = deepcopy(params)
    p.growth.days = max_days
    stage_params = [(s.max_weight_g, s.params or params) for s in stages] if stages else None
    rows = simulate(p, feeds=feeds, temperature_steps=temperature_steps, stage_params=stage_params,
                    until_weight_g=harvest_weight_g)
    for i, r in enumerate(rows):  # no-op safety net: with until_weight_g the last row already qualifies
        if r["weight_g"] >= harvest_weight_g:
            return rows[: i + 1]
    raise ValueError(f"Fish do not reach {harvest_weight_g:g} g within {max_days} days "
                     f"(reached: {rows[-1]['weight_g']:.0f} g).")


def stage_index(stages: list[RasStage], weight_g: float) -> int:
    for i, s in enumerate(stages):
        if weight_g < s.max_weight_g:
            return i
    return len(stages) - 1  # harvest day is in the last unit


def run_plan(curve: list[dict], stages: list[RasStage], plan: PlanParams,
             biofilter: BiofilterParams | None = None) -> dict:
    n_stage, horizon = len(stages), int(plan.horizon_days)
    harvest_age = len(curve)
    # if the start-of-day weight passed the threshold, the fish are in the next unit that day (same rule as simulate)
    stage_of_age = [stage_index(stages, r["start_weight_g"]) for r in curve]

    daily = [[dict.fromkeys(SUM_KEYS, 0.0) | {"batches": 0, "protein_feed": 0.0, "temp_feed": 0.0}
              for _ in range(horizon)] for _ in range(n_stage)]
    batches, harvests = [], []
    starts = range(0, horizon, max(int(plan.interval_days), 1))

    for b, s in enumerate(starts, start=1):
        segments = {}
        for age in range(1, harvest_age + 1):
            t = s + age - 1
            if t >= horizon:
                break
            r, k = curve[age - 1], stage_of_age[age - 1]
            d = daily[k][t]
            for key in SUM_KEYS:
                d[key] += r[key]
            d["batches"] += 1
            d["protein_feed"] += r["protein"] * r["feed_kg"]
            d["temp_feed"] += r["temperature"] * r["feed_kg"]
            seg = segments.setdefault(k, {"stage": k, "start": t, "end": t,
                                          "w_in": r["start_weight_g"], "w_out": r["weight_g"]})
            seg["end"], seg["w_out"] = t, r["weight_g"]
        harvest_day = s + harvest_age - 1
        harvested = harvest_day < horizon
        if harvested:
            last = curve[-1]
            harvests.append({"batch": b, "stock_day": s, "day": harvest_day,
                             "stock_date": plan.start_date + timedelta(days=s),
                             "date": plan.start_date + timedelta(days=harvest_day),
                             "count": last["count"], "weight_g": last["weight_g"],
                             "biomass_t": last["biomass_t"]})
        batches.append({"batch": b, "stock_day": s, "stock_date": plan.start_date + timedelta(days=s),
                        "segments": [segments[k] for k in sorted(segments)],
                        "harvest_day": harvest_day if harvested else None})

    designs = [_stage_design(i, st, daily[i], curve, stage_of_age, biofilter, plan)
               for i, st in enumerate(stages)]

    last_year = [h for h in harvests if h["day"] >= horizon - 365]
    return {
        "stages": stages, "daily": daily, "batches": batches, "harvests": harvests,
        "designs": designs, "harvest_age": harvest_age, "horizon": horizon,
        "curve": curve, "stage_of_age": stage_of_age, "interval_days": max(int(plan.interval_days), 1),
        "annual_harvest_t": sum(h["biomass_t"] for h in last_year) if horizon >= 365 else None,
        "steady_state": harvest_age <= horizon - 365,
    }


def _stage_design(i, stage, days, curve, stage_of_age, base_bp, plan=None) -> dict:
    """Design values for one RAS unit: the peaks seen over the plan."""
    ages = [a for a, k in enumerate(stage_of_age) if k == i]
    if not ages or not any(d["batches"] for d in days):
        return {"index": i, "stage": stage, "empty": True}
    peak = {k: max(d[k] for d in days) for k in SUM_KEYS}
    peak["batches"] = max(d["batches"] for d in days)
    feed_day = max(days, key=lambda d: d["feed_kg"])
    feed = feed_day["feed_kg"]
    protein = feed_day["protein_feed"] / feed if feed else curve[ages[0]]["protein"]
    temperature = feed_day["temp_feed"] / feed if feed else curve[ages[0]]["temperature"]
    # safety envelope: design demands take max(calculation, rule) so every bought piece of
    # equipment sits on the safe side (the engineer's rule: e.g. oxygen for >= 1 kg O2/kg feed)
    sp = (getattr(stage, "params", None) or getattr(plan, "params", None)) if plan is not None else None
    if sp is not None:
        des_o2 = max(peak["o2_total_kgd"], sp.design.o2_per_feed * feed)
        des_co2 = max(peak["co2_kgd"], sp.design.co2_per_o2 * des_o2)
        peak["design_o2_kgd"] = des_o2
        peak["design_co2_kgd"] = des_co2
        if curve[ages[0]]["pure_oxygen"]:
            peak["design_pure_o2_kgd"] = des_o2 / sp.gas.diffuser_efficiency
        # FLOW PHILOSOPHY (the engineer): keep flows at MINIMUMS. The design envelope sizes
        # EQUIPMENT (cone, degasser, biofilter) but must NOT inflate the recirculation flow.
        # Flows stay based on the CALCULATED loads; a balanced process has Q_O2 ≈ Q_TAN ≈
        # Q_CO2 ≈ Q_TSS, and the tank retention lands in the 30-60 min band.
    design = {
        "index": i, "stage": stage, "empty": False, "peak": peak, "days_in_stage": len(ages),
        "weight_in": curve[ages[0]]["start_weight_g"], "weight_out": curve[ages[-1]]["weight_g"],
        "tank_volume_m3": peak["volume_m3"] / max(stage.n_tanks, 1),
        "protein": protein, "temperature": temperature,
        "pure_oxygen": curve[ages[0]]["pure_oxygen"],
        # what the plan assumes when quoting pure O₂ (the diagram side uses the equipment's own
        # transfer efficiency) — the compare reads this instead of a hard-coded 0.95
        "o2_diffuser_eff": (sp.gas.diffuser_efficiency if sp is not None else 0.95),
    }
    base_bp = stage.biofilter or base_bp
    if base_bp is not None:
        bp = replace(base_bp, design_weight_g=0.0, temperature=temperature, protein=protein,
                     daily_feed_kg=feed * base_bp.feed_safety_factor, n_tanks=stage.n_tanks,
                     tank_volume_m3=peak["volume_m3"] / max(stage.n_tanks, 1))
        design["biofilter_params"] = bp
        # one recirculation flow per unit: the largest of the peak design flow (O₂, TAN, CO₂, TSS), tank volume /
        # retention time, and the flow at which the biofilter needs no more than its max. removal per pass
        # (removal per pass = kp · t / TAN target, so t ≤ max. retention × max. removal)
        retention = stage.retention_min
        q_design = peak["q_design_m3h"]
        design["flow_set_by"] = "retention"
        if q_design and q_design != float("inf") and peak["volume_m3"] > 0 and peak["volume_m3"] / q_design * 60 < retention:
            retention, design["flow_set_by"] = peak["volume_m3"] / q_design * 60, "loads"
        t_bio = max_retention_min(bp) * bp.max_removal_pct / 100 * 0.999
        if t_bio < retention:
            retention, design["flow_set_by"] = t_bio, "biofilter"
        design["retention_used_min"] = retention
        design["q_unit_m3h"] = peak["volume_m3"] / retention * 60 if retention > 0 else q_design
        design["biofilter"] = biofilter_row(bp, retention, sp.design if sp else None)
        design["biofilter_table"] = biofilter_table(bp, sp.design if sp else None)
        design["max_retention_min"] = max_retention_min(bp)
    return design
