"""The 9-step workflow: brief → plan → units → requirements → process diagram as the source of truth."""
import json

import pytest
from django.urls import reverse

from planner.models import Scenario
from ras_engine.compare import compare, summary_status, unit_targets
from ras_engine.process import solve
from test_process import node, pipe

pytestmark = pytest.mark.django_db


@pytest.fixture
def sc(client):
    client.get("/")
    s = Scenario.objects.get()
    client.post(reverse("production", args=[s.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "2", "horizon_unit": "years",
        "stage_name": ["Fry"], "stage_max": ["5"], "stage_tanks": ["4"], "stage_ret": ["30"]})
    s.refresh_from_db()
    return s


def loop_diagram():
    """makeup → sump → pump 100 → tank (TAN 1, TSS 2 kg/d) → drum → biofilter(media 3) → sump; sludge to discharge."""
    nodes = [node("mk", "makeup", {"flow_m3h": 2, "tss": 0, "tan": 0, "co2": 0, "o2": 9, "no3": 0}),
             node("s", "sump"), node("p", "pump", {"flow_m3h": 100}),
             node("t", "tank_bottom", {"tan_kgd": 1.0, "tss_kgd": 2.0, "o2_kgd": 3.0}),
             node("d", "drum_filter", {"tss_removal_pct": 60}, {"sludge": {"mode": "m3h", "value": 2}}),
             node("b", "biofilter", {"tan_removal_pct": 50, "media_m3": 3}), node("w", "discharge")]
    edges = [pipe("mk", "out", "s"), pipe("s", "out", "p"), pipe("p", "out", "t"), pipe("t", "bottom", "d"),
             pipe("d", "out", "b"), pipe("d", "sludge", "w"), pipe("b", "out", "s")]
    return {"nodes": nodes, "edges": edges}


def test_solve_totals_close_the_balance():
    r = solve(loop_diagram())
    tot = r["totals"]
    assert tot["recirculated_m3h"] == pytest.approx(100)
    assert tot["fish_load"]["tan_kgd"] == 1.0 and tot["media_m3"] == 3
    # everything the tank makes leaves somewhere: nitrified or with the sludge water
    sludge_tan = r["edges"]["d.sludge>w"]["q"] * r["edges"]["d.sludge>w"]["c"]["tan"] * 24 / 1000
    assert tot["tan_nitrified_kgd"] + sludge_tan == pytest.approx(1.0, rel=1e-4)
    # all solids leave with the sludge water: what the drum holds back plus its flow share of the rest
    assert tot["sludge_tss_kgd"] == pytest.approx(2.0, rel=1e-4)
    assert 0 < tot["tss_removed_kgd"] < tot["sludge_tss_kgd"]
    assert tot["sludge_m3h"] == pytest.approx(2)
    assert tot["o2_nitrification_kgd"] == pytest.approx(tot["tan_nitrified_kgd"] * 4.57)
    assert tot["flow_by_type"]["drum_filter"] == pytest.approx(100)
    assert set(tot["tanks"]) == {"t"} and tot["tanks"]["t"]["c"]["tan"] > 0


def test_compare_marks_shortfalls():
    r = solve(loop_diagram())
    targets = {"design_flow_m3h": 150.0, "tan_kgd": 1.0, "tss_kgd": 2.0, "o2_fish_kgd": 3.0, "co2_kgd": 0.0,
               "pure_o2_kgd": None, "makeup_m3h": 1.0, "media_m3": 5.0}
    rows = {row["check"]: row for row in compare(targets, r)}
    assert rows["Recirculated flow"]["status"] == "alarm"        # 100 of 150 m³/h
    assert rows["Biofilter media"]["status"] == "alarm"          # 3 of 5 m³
    assert rows["Make-up water"]["status"] == "ok"
    assert rows["TAN load on the tanks"]["status"] == "ok"
    assert summary_status(list(rows.values())) == "alarm"


def test_brief_sets_interval_harvest_weight_and_stocking(client, sc):
    url = reverse("brief", args=[sc.pk])
    assert client.get(url).status_code == 200
    client.post(url, {"name": "Client A", "batches_per_year": "6", "pieces_per_batch": "500000", "target_weight_g": "5",
                      "initial_weight_g": "0.2", "start_date": "2027-02-01", "temperature": "12", "salinity": "0",
                      "set_stocking": "on"})
    sc.refresh_from_db()
    plan = sc.plan_settings()
    assert plan["interval_days"] == 61 and plan["stages"][-1]["max_weight_g"] == 5
    assert sc.brief["pieces_per_batch"] == 500000 and sc.name == "Client A"
    stocked = sc.params["growth.initial_count"]
    from planner.views import _run_production
    _plan, res = _run_production(sc)
    assert res["harvests"][0]["count"] == pytest.approx(500000, rel=1e-5)   # what survives is what was asked for
    assert stocked > 500000


def test_units_page_saves_tanks_under_the_unit_id(client, sc):
    uid = sc.unit_id(0)
    url = reverse("units", args=[sc.pk])
    assert client.get(url).status_code == 200
    client.post(url, {"stage_id": [uid], "stage_name": ["Fry"], "stage_max": ["5"], "stage_tanks": ["4"], "stage_ret": ["30"],
                      f"t-{uid}-item": "custom", f"t-{uid}-cap": "12", f"t-{uid}-upto": ["1", ""], f"t-{uid}-k": ["1", "4"]})
    sc.refresh_from_db()
    tank = sc.equipment_of(0)["tank"]
    assert tank["capacity"] == 12 and tank["steps"] == [[1.0, 1], [None, 4]]
    # saving the equipment page keeps the tanks chosen on step 03
    client.post(reverse("equipment", args=[sc.pk]), {"n_stages": "1", "water": "fresh"})
    sc.refresh_from_db()
    assert sc.equipment_of(0)["tank"]["steps"] == [[1.0, 1], [None, 4]]


def test_equipment_requirements_come_from_the_diagram(client, sc):
    from planner.views import _equipment_units, _run_production
    _plan, res = _run_production(sc)
    before = _equipment_units(sc, res)[0][0]["req"]
    assert before.get("source") is None
    sc.set_diagram(0, loop_diagram())
    sc.save()
    req = _equipment_units(sc, res)[0][0]["req"]
    assert req["source"] == "diagram"
    assert req["required"]["pump"] == pytest.approx(100) and req["required"]["drum_filter"] == pytest.approx(100)
    assert req["required"]["degasser"] == 0 and req["required"]["biomedia"] == 3


def test_process_solve_returns_the_checks(client, sc):
    page = client.get(reverse("process", args=[sc.pk]))
    editor = page.context["editor"]
    assert editor["targets"]["design_flow_m3h"] > 0 and editor["limits"]["tan"]["max"] == 1.0
    body = {**loop_diagram(), "targets": editor["targets"]}
    r = client.post(editor["urls"]["solve"], json.dumps(body), content_type="application/json").json()
    assert r["ok"] and r["compare"] and r["compare_status"] in ("ok", "warn", "alarm")


def test_rail_shows_the_nine_steps(client, sc):
    html = client.get(reverse("requirements", args=[sc.pk])).content.decode()
    for label in ("Brief", "Production plan", "Units &amp; tanks", "Requirements", "Process design", "Equipment",
                  "CAPEX", "OPEX", "Proposal", "All assumptions"):
        assert label in html
    assert 'aria-current="page"' in html and "Step 05" in html and "Step 03" in html   # prev / next


def test_unit_targets_follow_the_feed_without_extra_factors(sc):
    from planner.views import _run_production
    _plan, res = _run_production(sc)
    d = res["designs"][0]
    t = unit_targets(d)
    # the larger of the loads' flow and the flow the biofilter / tank retention ask for — nothing multiplied
    assert t["design_flow_m3h"] == pytest.approx(max(d["peak"]["q_design_m3h"], d["q_unit_m3h"]))
    assert d["biofilter"]["feasible"] and d["biofilter"]["efficiency_pct"] <= 70.0 + 1e-6
    assert t["tan_kgd"] == d["peak"]["tan_kgd"] and "peak_factor" not in d


def test_flow_table_sets_limits_and_inlet_water(client, sc):
    """Step 04: type the tank limit and the water entering per parameter; the flows follow Q = load / ΔC."""
    from planner.views import _run_production
    from ras_engine.formulas import o2_saturation
    url = reverse("requirements", args=[sc.pk])
    html = client.get(url).content.decode()
    assert "flow by parameter" in html and 'name="u0-o2_in"' in html
    client.post(url, {"form": "flows", "u0-pure": "yes", "u0-o2_in": "18", "u0-o2_out": "8",
                      "u0-tan_max": "1", "u0-tan_in": "0.4", "u0-co2_max": "15", "u0-co2_in": "5",
                      "u0-tss_max": "10", "u0-tss_in": "3"})
    sc.refresh_from_db()
    _plan, res = _run_production(sc)
    d = res["designs"][0]
    csat = o2_saturation(d["temperature"], sc.engine_params().gas.salinity)
    g, f = sc.engine_params().gas, sc.engine_params().flow
    assert g.pure_oxygen and g.inlet_o2_saturation * csat == pytest.approx(18, rel=1e-3)
    assert g.tank_outlet_saturation * csat == pytest.approx(8, rel=1e-3)
    assert f.tan_max + f.tan_removal_eff * (f.tan_best - f.tan_max) == pytest.approx(0.4, abs=1e-3)
    assert f.co2_max == 15 and f.co2_max + f.co2_removal_eff * (f.co2_best - f.co2_max) == pytest.approx(5, abs=1e-3)
    peak = d["peak"]
    assert peak["q_tan_m3h"] == pytest.approx(peak["tan_kgd"] * 1000 / 24 / 0.6, rel=1e-3)
    assert peak["q_co2_m3h"] == pytest.approx(peak["co2_kgd"] * 1000 / 24 / 10, rel=1e-3)
    assert peak["q_tss_m3h"] == pytest.approx(peak["tss_kgd"] * 1000 / 24 / 7, rel=1e-3)
