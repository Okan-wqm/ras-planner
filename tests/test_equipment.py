import math
from datetime import date

import pytest

from ras_engine import (BiofilterParams, Params, PlanParams, RasStage, growth_curve, run_plan,
                        size_item, size_tanks, stage_requirements)


@pytest.fixture(scope="module")
def res():
    stages = [RasStage("Fry", 2), RasStage("Fingerling", 5)]
    curve = growth_curve(Params(), 5, stages=stages)
    return run_plan(curve, stages, PlanParams(date(2026, 12, 1), 30, 400), BiofilterParams())


def test_requirements_match_plan(res):
    req = stage_requirements(res, 1)
    d = res["designs"][1]
    assert req["max_batches"] == d["peak"]["batches"]
    assert req["required"]["pump"] == pytest.approx(max(d["peak"]["q_design_m3h"], d["q_unit_m3h"]))   # nothing multiplied
    assert d["biofilter"]["feasible"]
    assert req["required"]["biofilter"] == d["biofilter"]["volume_fresh_m3"]
    assert stage_requirements(res, 1, "salt")["required"]["biofilter"] == d["biofilter"]["volume_salt_m3"]
    # one batch's peak volume never exceeds the unit's total peak
    assert req["batch_peak_volume_m3"] <= req["total_peak_volume_m3"] + 1e-9


def test_tanks_from_standard_size(res):
    req = stage_requirements(res, 1)
    size = req["batch_peak_volume_m3"] / 2.5  # needs 3 tanks per batch
    t = size_tanks(req, res, 1, size, [])
    assert t["auto_steps"] and t["step_input"] == [(None, 3)]
    assert t["ok"] and math.isclose(t["margin_pct"], 20)
    assert t["peak_density"] < req["max_density"]
    # one fixed step: pooled total equals 3 tanks × batches held at once
    assert t["total_tanks"] == 3 * req["max_batches"]


def test_tank_size_from_tanks_per_batch(res):
    req = stage_requirements(res, 1)
    t = size_tanks(req, res, 1, None, [(None, 4)])
    assert math.isclose(t["required_tank_volume"], req["batch_peak_volume_m3"] / 4)
    assert t["total_tanks"] == 4 * req["max_batches"]


def test_too_small_tanks_flagged(res):
    req = stage_requirements(res, 1)
    t = size_tanks(req, res, 1, req["batch_peak_volume_m3"] / 4, [(None, 3)])
    assert not t["ok"] and t["margin_pct"] < 0
    assert t["peak_density"] > req["max_density"]


@pytest.fixture(scope="module")
def dense():
    """Stocking every 10 days so several batches share the 2–5 g unit."""
    stages = [RasStage("Fry", 2), RasStage("Fingerling", 5)]
    curve = growth_curve(Params(), 5, stages=stages)
    return run_plan(curve, stages, PlanParams(date(2026, 12, 1), 10, 400), BiofilterParams())


def test_graded_steps_use_fewer_tanks(dense):
    """1 tank until 3 g, then 4 tanks (grading): fewer tanks overall than 4 per batch throughout."""
    res = dense
    req = stage_requirements(res, 1)
    assert req["max_batches"] >= 2
    graded = size_tanks(req, res, 1, None, [(3, 1), (None, 4)])
    flat = size_tanks(req, res, 1, None, [(None, 4)])
    assert graded["total_tanks"] < flat["total_tanks"] == 4 * req["max_batches"]
    first, second = graded["steps"]
    assert first["tanks"] == 1 and first["up_to"] == 3 and second["from"] == 3
    # each step is sized on its own peak
    assert first["peak_volume"] < second["peak_volume"] <= req["batch_peak_volume_m3"] + 1e-9
    assert math.isclose(graded["required_tank_volume"],
                        max(first["peak_volume"] / 1, second["peak_volume"] / 4))


def test_allocation_never_double_books(dense):
    res = dense
    t = size_tanks(stage_requirements(res, 1), res, 1, None, [(3, 1), (None, 4)])
    horizon = res["horizon"]
    for day in range(horizon):
        owners = {}
        for b in t["allocation"]["batches"]:
            i = day - b["start"]
            if 0 <= i < len(b["vols"]):
                held = [tank for tank, since in b["allocs"] if since <= day]
                assert len(held) == b["ks"][i]  # holds exactly the tanks it needs
                for tank in held:
                    assert tank not in owners, f"tank {tank} double-booked on day {day}"
                    owners[tank] = b["b"]
        assert len(owners) <= t["total_tanks"]


def test_size_item():
    s = size_item(1000, 300, None)
    assert s["qty"] == 4 and s["auto_qty"] and s["ok"] and math.isclose(s["margin_pct"], 20)
    s = size_item(1000, 300, 3)
    assert not s["ok"]
    assert size_item(0.0, 50, None).get("not_needed")
    assert size_item(None, 50, None)["qty"] is None


def test_tank_turnaround_days():
    from ras_engine.equipment import allocate_tanks
    """Harvest day frees the tanks for the next batch the same day; dry days keep them out of use."""
    p = Params()
    p.growth.temperature = 12.0
    st = [RasStage("Fingerling", 10, 6, 30, params=p)]
    res = run_plan(growth_curve(p, 10, stages=st), st, PlanParams(date(2027, 1, 1), 60, 720), BiofilterParams())
    overlap = res["harvest_age"] - 60
    assert overlap > 0                                   # a new batch arrives before the old one leaves
    steps = [(1.0, 1), (None, 6)]
    same_day, dry_3 = allocate_tanks(res, 0, steps, 0), allocate_tanks(res, 0, steps, 3)
    days_to_1g = next(i for i, r in enumerate(res["curve"]) if r["start_weight_g"] >= 1)
    if days_to_1g >= overlap:   # the new batch only grades after the old one is harvested
        assert same_day["n_tanks"] == 7
    assert dry_3["n_tanks"] >= same_day["n_tanks"]
    assert allocate_tanks(res, 0, steps, 40)["n_tanks"] >= 12   # long rest: the drying tanks are not available
