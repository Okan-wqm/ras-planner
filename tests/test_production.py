import math
from datetime import date

import pytest

from ras_engine import (BiofilterParams, Params, PlanParams, RasStage, growth_curve, run_plan,
                        simulate)


@pytest.fixture(scope="module")
def curve():
    return growth_curve(Params(), harvest_weight_g=5)


def plan(curve, stages, interval=30, horizon=400):
    return run_plan(curve, stages, PlanParams(date(2026, 12, 1), interval, horizon), BiofilterParams())


def test_curve_ends_at_harvest_weight(curve):
    assert curve[-1]["weight_g"] >= 5 > curve[-2]["weight_g"]


def test_single_batch_equals_simulation(curve):
    res = plan(curve, [RasStage("Tek", 5)], interval=10_000)
    day_vol = [d["volume_m3"] for d in res["daily"][0]]
    assert day_vol[: len(curve)] == [r["volume_m3"] for r in curve]
    assert res["designs"][0]["peak"]["batches"] == 1


def test_overlapping_batches_add_up(curve):
    """Stocking every 30 days: a unit holds ceil(H/30) batches at once and their volumes add up."""
    H = len(curve)
    res = plan(curve, [RasStage("Tek", 5)], interval=30)
    assert res["designs"][0]["peak"]["batches"] == math.ceil(H / 30)
    t = 200  # a steady-state day
    expected = sum(curve[t - s]["volume_m3"] for s in range(0, t + 1, 30) if t - s < H)
    assert math.isclose(res["daily"][0][t]["volume_m3"], expected)


def test_splitting_ras_conserves_totals(curve):
    one = plan(curve, [RasStage("0-5", 5)])
    two = plan(curve, [RasStage("0-2", 2), RasStage("2-5", 5)])
    for t in range(400):
        total = two["daily"][0][t]["volume_m3"] + two["daily"][1][t]["volume_m3"]
        assert math.isclose(total, one["daily"][0][t]["volume_m3"], abs_tol=1e-9)
    # every batch is in RAS-1 then RAS-2; the move happens at 2 g
    seg = two["batches"][0]["segments"]
    assert [s["stage"] for s in seg] == [0, 1]
    assert seg[1]["w_in"] >= 2  # in RAS-2 the day after passing 2 g
    assert seg[1]["start"] == seg[0]["end"] + 1
    # the smaller unit needs less tank volume than a single unit
    assert two["designs"][0]["peak"]["volume_m3"] < one["designs"][0]["peak"]["volume_m3"]


def test_harvests(curve):
    res = plan(curve, [RasStage("Tek", 5)], interval=30, horizon=400)
    H = len(curve)
    assert [h["stock_day"] for h in res["harvests"]] == [s for s in range(0, 400, 30) if s + H - 1 < 400]
    assert all(h["weight_g"] >= 5 for h in res["harvests"])


def test_stage_biofilter_uses_stage_peak(curve):
    res = plan(curve, [RasStage("0-2", 2, n_tanks=4), RasStage("2-5", 5, n_tanks=6)])
    for d in res["designs"]:
        bp = d["biofilter_params"]
        assert math.isclose(bp.daily_feed_kg, d["peak"]["feed_kg"] * 4 / 3)
        assert math.isclose(bp.tank_volume_m3 * bp.n_tanks, d["peak"]["volume_m3"])


def _stage_params(**gas):
    p = Params()
    for k, v in gas.items():
        setattr(p.gas, k, v)
    return p


def test_ras_without_pure_oxygen():
    """RAS-1 without pure O₂ (aeration), RAS-2 with pure O₂."""
    stages = [RasStage("Hatchery", 2, params=_stage_params(pure_oxygen=False)),
              RasStage("Fingerling", 5, params=Params())]
    curve = growth_curve(Params(), 5, stages=stages)
    for r in curve:
        in_first = r["start_weight_g"] < 2
        assert r["pure_oxygen"] is (not in_first)
        if in_first:
            assert r["pure_o2_kgd"] == 0 and r["o2_lpm_actual"] == 0
    # aeration gives lower inlet saturation → much more flow for the same O₂
    aer = next(r for r in curve if r["start_weight_g"] < 2 and r["day"] > 1)
    ref = simulate(Params())[aer["day"] - 1]
    assert aer["o2_sat_in"] < ref["o2_sat_in"]
    assert aer["q_o2_m3h"] > 2 * ref["q_o2_m3h"]
    res = plan(curve, stages)
    assert not res["designs"][0]["pure_oxygen"] and res["designs"][0]["peak"]["pure_o2_kgd"] == 0
    assert res["designs"][1]["pure_oxygen"] and res["designs"][1]["peak"]["pure_o2_kgd"] > 0


def test_ras_temperature_changes_growth():
    warm = Params()
    warm.growth.temperature = 15
    base = growth_curve(Params(), 5)
    staged = growth_curve(Params(), 5, stages=[RasStage("A", 2, params=Params()),
                                                RasStage("B", 5, params=warm)])
    assert len(staged) < len(base)  # warmer unit after 2 g → 5 g sooner
    n = next(i for i, r in enumerate(base) if r["start_weight_g"] >= 2)
    assert [r["weight_g"] for r in staged[:n]] == [r["weight_g"] for r in base[:n]]


def test_unreachable_harvest_weight():
    p = Params()
    with pytest.raises(ValueError):
        growth_curve(p, harvest_weight_g=1e9, max_days=100)


def test_oxygen_is_split_into_fish_and_nitrification():
    from ras_engine import Params, simulate
    p = Params()
    p.growth.days = 30
    r = simulate(p)[-1]
    assert r["o2_kgd"] == pytest.approx(r["feed_kg"] * 0.35)                 # fish only: sets the tank flow
    assert r["o2_bio_kgd"] == pytest.approx(r["tan_kgd"] * 4.57)             # nitrification in the biofilter
    assert r["o2_total_kgd"] == pytest.approx(r["o2_kgd"] + r["o2_bio_kgd"])
    assert r["pure_o2_kgd"] == pytest.approx(r["o2_kgd"] / 0.95)             # biofilter aerated by default
    p.gas.biofilter_o2_from_pure_o2 = True
    assert simulate(p)[-1]["pure_o2_kgd"] == pytest.approx(r["o2_total_kgd"] / 0.95)
    assert r["co2_kgd"] == pytest.approx(r["o2_kgd"] * 1.375 + r["tan_kgd"] * 5.85)


def test_tgc_growth():
    from ras_engine import Params, simulate
    p = Params()
    p.growth.days, p.growth.temperature, p.growth.use_tgc = 100, 12.0, True
    w = [r["weight_g"] for r in simulate(p)]
    assert w[-1] ** (1 / 3) == pytest.approx(0.2 ** (1 / 3) + 99 * 2.5 / 1000 * 12)


def test_default_diagrams_agree_with_the_plan():
    """The starting diagram of every unit is built from the scenario's assumptions: it solves within its limits
    and the biofilter stays below the per-pass removal limit."""
    import os
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rasplan.settings")
    import django
    django.setup()
    from planner.views import _diagram_for
    from ras_engine import BiofilterParams, Params, PlanParams, RasStage, growth_curve, run_plan
    from ras_engine.process import solve
    p = Params()
    st = [RasStage("Fry", 5, 4, 30, params=p), RasStage("Grow", 100, 6, 30, params=p), RasStage("Big", 300, 8, 30, params=p)]
    res = run_plan(growth_curve(p, 300, stages=st), st, PlanParams(date(2027, 1, 1), 30, 1096), BiofilterParams())

    class NoSaved:
        def diagram_of(self, index):
            return None
    NoSaved = NoSaved()
    for i, d in enumerate(res["designs"]):
        assert d["biofilter"]["feasible"] and d["biofilter"]["efficiency_pct"] <= 70
        r = solve(_diagram_for(NoSaved, res, i)[0])
        assert r["converged"] and not [w for n in r["nodes"].values() for w in n["warnings"]]


def test_units_keep_their_data_when_a_unit_is_inserted(client, db):
    """Diagrams and equipment are saved under each unit's id, so adding a unit in front moves nothing."""
    from django.urls import reverse
    from planner.models import Scenario
    client.get("/")
    sc = Scenario.objects.get()
    base = {"start_date": "2027-01-01", "interval_days": "30", "horizon_value": "1", "horizon_unit": "years"}
    client.post(reverse("production", args=[sc.pk]), {**base, "stage_name": ["Fry", "Grow"], "stage_max": ["5", "50"],
                                                      "stage_tanks": ["4", "4"], "stage_ret": ["30", "30"]})
    sc.refresh_from_db()
    ids = sc.unit_ids()
    assert len(set(ids)) == 2 and all(len(i) == 8 for i in ids)   # every unit gets its own id
    sc.set_diagram(1, {"nodes": [{"id": "x", "type": "uv", "data": {}}], "edges": []})
    sc.save()
    # insert a hatchery unit in front (a new row has no id yet)
    client.post(reverse("production", args=[sc.pk]), {**base, "stage_name": ["Hatch", "Fry", "Grow"],
                                                      "stage_max": ["1", "5", "50"], "stage_tanks": ["2", "4", "4"],
                                                      "stage_ret": ["30", "30", "30"], "stage_id": ["", *ids]})
    sc.refresh_from_db()
    new_ids = sc.unit_ids()
    assert new_ids[1:] == ids and new_ids[0] not in ids
    assert sc.diagram_of(2)["nodes"][0]["id"] == "x" and sc.diagram_of(0) is None



def test_units_saved_before_ids_keep_their_position_as_id(client, db):
    from django.urls import reverse
    from planner.models import Scenario
    client.get("/")
    sc = Scenario.objects.get()
    sc.production = {"start_date": "2027-01-01", "interval_days": 30, "horizon_days": 365,
                     "stages": [{"name": "Fry", "max_weight_g": 5, "n_tanks": 4, "retention_min": 30}]}
    sc.process = {"0": {"nodes": [{"id": "x", "type": "uv", "data": {}}], "edges": []}}
    sc.save()
    assert sc.unit_ids() == ["0"] and sc.diagram_of(0)["nodes"][0]["id"] == "x"
    page = client.get(reverse("units", args=[sc.pk])).content.decode()
    assert 'name="stage_id" value="0"' in page    # the form sends it back, so the diagram stays attached
