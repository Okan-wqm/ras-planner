"""The safety envelope: every design number sits on the SAFE side — demands take
max(calculation, rule). The engineer's rule: e.g. oxygen for >= 1 kg O2 per kg feed."""
import pytest
from datetime import date

from ras_engine import BiofilterParams, Params, PlanParams, RasStage, growth_curve, run_plan


def _plan(o2_per_feed=1.0):
    p = Params()
    p.design.o2_per_feed = o2_per_feed
    st = [RasStage("Fry", 5, 4, 30, params=p),
          RasStage("Grow", 100, 6, 30, params=p),
          RasStage("Big", 300, 8, 30, params=p)]
    plan = PlanParams(date(2027, 1, 1), 30, 1096, params=p)
    return run_plan(growth_curve(p, 300, stages=st), st, plan, BiofilterParams())


def test_design_envelope_floors_oxygen_at_the_rule():
    for d in _plan()["designs"]:
        peak = d["peak"]
        assert peak["design_o2_kgd"] == pytest.approx(
            max(peak["o2_total_kgd"], 1.0 * peak["feed_kg"]))
        assert peak["design_co2_kgd"] >= peak["co2_kgd"]
        assert peak["design_co2_kgd"] == pytest.approx(max(peak["co2_kgd"], 1.4 * peak["design_o2_kgd"]))
        assert peak["design_pure_o2_kgd"] == pytest.approx(peak["design_o2_kgd"] / 0.95)


def test_calculation_above_the_rule_wins():
    for d in _plan(o2_per_feed=0.1)["designs"]:
        peak = d["peak"]
        assert peak["design_o2_kgd"] == pytest.approx(peak["o2_total_kgd"])   # calc > rule


def test_design_flow_carries_the_design_loads():
    base, env = _plan(o2_per_feed=0.1)["designs"], _plan()["designs"]
    for low, high in zip(base, env):
        assert high["peak"]["q_design_m3h"] >= low["peak"]["q_design_m3h"]


def test_params_carry_the_design_group():
    flat = Params().to_flat()
    assert flat["design.o2_per_feed"] == 1.0 and flat["design.co2_per_o2"] == 1.4
    assert flat["design.biofilter_min_retention"] == 4.0 and flat["design.tan_knee"] == 2.0


def test_biofilter_safety_notes():
    from ras_engine import biofilter as bf
    bp = BiofilterParams(tan_target=1.0)          # 1.0 < 2 mg/L knee
    row = bf.biofilter_row(bp, 3.0, Params().design)   # 3 min < 4 min floor
    assert any("NO₂" in n for n in row["notes"])
    assert any("knee" in n for n in row["notes"])
    quiet = bf.biofilter_row(BiofilterParams(tan_target=3.0), 30.0, Params().design)
    assert quiet["notes"] == []


def test_compare_oxygen_target_is_calculated_fish_plus_biofilter():
    """Flow philosophy: the compare checks the CALCULATED O2 (fish + biofilter at the
    diffuser efficiency), not the design envelope — the envelope sizes equipment only."""
    from ras_engine.compare import unit_targets
    d = _plan()["designs"][1]
    t = unit_targets(d, "fresh")
    assert t["pure_o2_kgd"] == pytest.approx(d["peak"]["o2_total_kgd"] / 0.95)


def test_opex_oxygen_never_below_the_design_rule():
    from ras_engine.opex import annual_opex
    res = _plan()
    o = annual_opex(res, {}, {}, 0.0, design_o2_per_feed=1.0, diffuser_efficiency=0.95)
    oxy = next(ln for ln in o["lines"] if ln["key"] == "oxygen")
    feed_total = sum(ln["qty"] or 0.0 for ln in o["lines"] if ln["group"] == "Feed")
    assert oxy["qty_calc"] == pytest.approx(1.0 * feed_total / 0.95)
    assert "design rule" in oxy["note"]


def test_requirements_page_shows_the_envelope(client, db):
    from django.urls import reverse
    client.get("/")
    from planner.models import Scenario
    sc = Scenario.objects.get()
    client.post(reverse("production", args=[sc.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "1",
        "horizon_unit": "years", "stage_name": ["Fry"], "stage_max": ["5"],
        "stage_tanks": ["4"], "stage_ret": ["30"]})
    page = client.get(reverse("requirements", args=[sc.pk])).content.decode()
    assert "Design O₂ (safety envelope)" in page and "Design CO₂ (safety envelope)" in page


def test_heater_node_heats_to_set_point():
    from ras_engine.process import solve
    from tests.test_process import node, once_through
    r = once_through(node("ht", "heater", {"setpoint_c": 14, "cop": 4}), {"flow_m3h": 10, "temp": 8})
    ht = r["nodes"]["ht"]
    assert ht["heating_kw"] == pytest.approx(10 * 1.163 * 6)
    assert ht["heater_power_kw"] == pytest.approx(10 * 1.163 * 6 / 4)
    assert r["edges"]["ht.out>dout"]["c"]["temp"] == pytest.approx(14)
    # already warm: no heating
    r = once_through(node("ht", "heater", {"setpoint_c": 12}), {"flow_m3h": 10, "temp": 15})
    assert r["nodes"]["ht"]["heating_kw"] == 0 and r["edges"]["ht.out>dout"]["c"]["temp"] == pytest.approx(15)
    # too small: heats as far as it can
    r = once_through(node("ht", "heater", {"setpoint_c": 14, "capacity_kw": 30}), {"flow_m3h": 10, "temp": 8})
    assert r["edges"]["ht.out>dout"]["c"]["temp"] == pytest.approx(8 + 30 / 11.63)
    assert r["nodes"]["ht"].get("heating_short_kw", 0) > 0


def test_opex_quotes_heating_before_recovery():
    from ras_engine.opex import annual_opex
    res = _plan()
    p = Params()
    o = annual_opex(res, {}, {}, 0.0, design_params=p.design, water_temp=p.growth.temperature)
    heat = next((ln for ln in o["lines"] if ln["key"] == "heating"), None)
    assert heat, "heating line missing"
    water = next(ln["qty"] for ln in o["lines"] if ln["group"] == "Water")
    assert heat["qty_calc"] == pytest.approx(water * 1.163 * (12 - 8) / 3.0)   # COP 3
    # losses kick in when the UA coefficient is set and the hall is colder than the water
    p.design.tank_ua_w_per_k = 100.0
    p.design.room_temp = 5.0
    o2 = annual_opex(res, {}, {}, 0.0, design_params=p.design, water_temp=p.growth.temperature)
    heat2 = next(ln for ln in o2["lines"] if ln["key"] == "heating")
    assert heat2["qty_calc"] > heat["qty_calc"]
    # warm inlet: no heating line at all
    p.design.tank_ua_w_per_k = 0.0
    p.design.inlet_water_temp = 15.0
    o3 = annual_opex(res, {}, {}, 0.0, design_params=p.design, water_temp=p.growth.temperature)
    assert not any(ln["key"] == "heating" for ln in o3["lines"])


def test_unit_targets_pure_o2_uses_diffuser_efficiency_from_design():
    from ras_engine.compare import unit_targets
    d = {"empty": False, "pure_oxygen": True, "o2_diffuser_eff": 0.80,
         "peak": {"o2_total_kgd": 8.0, "q_design_m3h": 40.0, "tan_kgd": 1.0, "tss_kgd": 5.0,
                  "o2_kgd": 5.0, "co2_kgd": 10.0, "q_no3_m3d": 120.0}}
    t = unit_targets(d, "fresh")
    assert t["pure_o2_kgd"] == pytest.approx(8.0 / 0.80)
    assert t["o2_diffuser_eff"] == pytest.approx(0.80)
    d2 = {**d, "o2_diffuser_eff": 0.95}   # the old hard-coded value stays the fallback default
    assert unit_targets(d2, "fresh")["pure_o2_kgd"] == pytest.approx(8.0 / 0.95)
