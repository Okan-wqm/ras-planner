"""Phase 6 consistency chains: each commit's behavior change gets its regression test here.

6.1 O₂ source consistency — the plan's pure-O₂ target and the diagram's dosed O₂ measure different
things (fish-only ÷ 0.95 vs all consumers ÷ ~0.90); the compare row must say so instead of a silent
ok, and the OPEX scaling must use the like-for-like pure-O₂ profile.
"""
import json

import pytest
from django.urls import reverse

from planner.models import Scenario
from planner.views import _run_production
from ras_engine.compare import compare
from ras_engine.opex import annual_opex
from ras_engine.process import solve

pytestmark = pytest.mark.django_db


@pytest.fixture
def sc(client):
    """The default scenario with a saved production plan (two RAS units), as on a real farm."""
    client.get("/")
    s = Scenario.objects.get()
    client.post(reverse("production", args=[s.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "1", "horizon_unit": "years",
        "stage_name": ["Fry", "Fingerling"], "stage_max": ["2", "5"], "stage_tanks": ["3", "4"], "stage_ret": ["20", "20"]})
    s.refresh_from_db()
    return s


def _solved(client, sc, with_targets=True):
    """The default (automatic) diagram of RAS unit 0, solved with the plan's targets attached."""
    editor = client.get(reverse("process", args=[sc.pk])).context["editor"]
    diagram = editor["diagram"]
    if with_targets:
        diagram["targets"] = editor["targets"]
    solved = client.post(editor["urls"]["solve"], json.dumps(diagram), content_type="application/json").json()
    assert solved["ok"], solved.get("error")
    return editor, diagram, solved


# --- 6.1 O₂ source consistency ---------------------------------------------------------------

def test_pure_o2_row_ok_when_target_includes_biofilter(client, sc):
    """Flow philosophy: the compare target is fish + biofilter O2 (what the cone
    actually supplies), so the default diagram matches and the row stays ok/warn."""
    _, _, solved = _solved(client, sc)
    row = next(r for r in solved["compare"] if r["check"] == "Pure oxygen dissolved")
    assert row["status"] in ("ok", "warn")


def test_pure_o2_row_ok_when_diagram_matches_plan(client, sc):
    """A diagram whose dosed O₂ equals the plan target (ratio 1) stays a plain ok row."""
    editor, _, solved = _solved(client, sc)
    matched = dict(solved)
    matched["totals"] = dict(solved["totals"], pure_o2_kgd=editor["targets"]["pure_o2_kgd"])
    row = next(r for r in compare(editor["targets"], matched) if r["check"] == "Pure oxygen dissolved")
    assert row["status"] == "ok" and row["note"] == "oxygen equipment"


def test_diagram_o2_scaled_by_the_pure_o2_profile(sc):
    """A diagram dosing exactly each unit's peak pure O₂ must land on the batch's own pure-O₂ sum
    (fish O₂ ÷ 0.95 diffuser efficiency) per year — scaling by the fish-only profile loses the 0.95."""
    res = _run_production(sc)[1]
    diagrams, want = {}, 0.0
    for k in range(len(res["stages"])):
        peak = res["designs"][k]["peak"]
        diagrams[k] = {"pure_o2_kgd": peak["pure_o2_kgd"], "ozone_kgd": 0.0, "sludge_tss_kgd": 0.0}
        days = [res["curve"][a] for a, st in enumerate(res["stage_of_age"]) if st == k]
        want += sum(r["pure_o2_kgd"] for r in days)
    assert want > 0
    o = annual_opex(res, {}, diagrams=diagrams)
    oxygen = next(l for l in o["lines"] if l["key"] == "oxygen")
    assert oxygen["qty"] == pytest.approx(want * o["batches_per_year"])
    assert "process diagrams" in oxygen["note"]  # the O₂ line names its diagram source


# --- 6.2 CAPEX stable keys, orphan pruning, source badge -------------------------------------


def _tank_node(nid, std_pk=None, price=None):
    data = {"spec": {"volume_m3": 14}}
    if std_pk is not None:
        data["standard"] = str(std_pk)
    if price is not None:
        data["unit_price"] = price
    return {"id": nid, "type": "tank", "position": {"x": 0, "y": 0}, "data": data}


def _diagram_with_tank(sc, std):
    sc.set_diagram(0, {"nodes": [_tank_node("s0", std_pk=std.pk), _tank_node("s1", std_pk=std.pk)], "edges": []})
    sc.save()


def test_line_key_survives_a_standard_price_change(client, sc):
    """The line key says what is installed (type, standard, nodes) — a standard's price change must not
    re-key the line, so a saved unit-price override stays attached (was: silently orphaned)."""
    from planner.models import EquipmentStandard
    std = EquipmentStandard.objects.create(category="tank", name="Round 14", capacity=14, unit_price=2000)
    _diagram_with_tank(sc, std)
    url = reverse("capex", args=[sc.pk])
    lines = client.get(url).context["lines"]
    tank = next(ln for ln in lines if ln["category"].startswith("Fish tank"))
    key = tank["key"]

    client.post(url, {"currency": "EUR", "line_key": [key], f"price-{key}": "2600"})  # save an override
    std.unit_price = 3300
    std.save()
    tank = next(ln for ln in client.get(url).context["lines"] if ln["category"].startswith("Fish tank"))
    assert tank["key"] == key                       # same equipment -> same key
    assert tank["overridden"] and tank["price"] == 2600 and tank["total"] == 2 * 2600


def test_orphaned_override_is_pruned_with_a_warning(client, sc):
    """A posted override for a key that matches no CAPEX line is dropped with a warning, not stored."""
    from planner.models import EquipmentStandard
    std = EquipmentStandard.objects.create(category="tank", name="Round 14", capacity=14, unit_price=2000)
    _diagram_with_tank(sc, std)
    url = reverse("capex", args=[sc.pk])
    live = [ln["key"] for ln in client.get(url).context["lines"]]
    dead = "p0-dead000000"
    resp = client.post(url, {"currency": "EUR", "line_key": live + [dead], f"price-{dead}": "99000",
                             "price-" + live[0]: "100"}, follow=True)
    sc.refresh_from_db()
    saved = sc.capex["price_overrides"]
    assert dead not in saved and live[0] in saved
    assert any("no longer match a CAPEX line" in str(m) for m in resp.context["messages"])


def test_capex_page_badges_diagram_lines(client, sc):
    """Each line says where the unit's equipment comes from; a drawn unit shows 'from diagram'."""
    from planner.models import EquipmentStandard
    std = EquipmentStandard.objects.create(category="tank", name="Round 14", capacity=14, unit_price=2000)
    client.post(reverse("equipment", args=[sc.pk]), {
        "n_stages": "2", "water": "fresh",
        "s0-tank-item": str(std.pk), "s0-tank-upto": [""], "s0-tank-k": ["2"],
        "s1-tank-item": str(std.pk), "s1-tank-upto": [""], "s1-tank-k": ["2"]})
    url = reverse("capex", args=[sc.pk])
    html = client.get(url).content.decode()
    assert "from equipment" in html and "from diagram" not in html      # no diagram drawn yet
    _diagram_with_tank(sc, std)
    html = client.get(url).content.decode()
    assert "from diagram" in html
    # the intro no longer claims equipment always comes from the Equipment page
    assert "(whichever is drawn)" in html


# --- 6.3 lamp semantics ----------------------------------------------------------------------
from planner.services import step_status  # noqa: E402
from test_process import node, pipe  # noqa: E402


def test_unsolvable_diagram_is_an_alarm_not_blank(client, sc):
    """A drawn diagram that cannot be solved (here: a loop without a pump) lights lamp 05 red;
    a unit with nothing drawn keeps it unlit."""
    from planner.services import diagram_results
    assert step_status(sc)["05"] == ""                      # nothing drawn yet
    sc.set_diagram(0, {"nodes": [node("s", "sump"), node("t", "tank_bottom", {"tan_kgd": 1.0})],
                       "edges": [pipe("s", "out", "t"), pipe("t", "bottom", "s")]})
    sc.save()
    assert diagram_results(sc.diagram_of(0)) is None        # drawn but unsolvable
    assert step_status(sc)["05"] == "alarm"


def test_non_settling_solve_warns_lamp_05(client, sc):
    """A diagram that solves without settling (NO₃/TSS pile up in a closed loop) makes lamp 05 at
    least a warning — its concentrations are a hint, not an answer."""
    loop = {"nodes": [node("s", "sump"), node("p", "pump", {"flow_m3h": 100}),
                      node("t", "tank_bottom", {"tan_kgd": 1.0, "tss_kgd": 2.0, "o2_kgd": 3.0}),
                      node("b", "biofilter", {"tan_removal_pct": 50, "media_m3": 3})],
            "edges": [pipe("s", "out", "p"), pipe("p", "out", "t"), pipe("t", "bottom", "b"),
                      pipe("b", "out", "s")]}
    from planner.services import diagram_results
    r = diagram_results(loop)
    assert r is not None and r["converged"] is False        # solves, but never settles
    sc.set_diagram(0, loop)
    sc.save()
    assert step_status(sc)["05"] in ("warn", "alarm")


def test_bad_plan_keeps_the_rail_informative(client, sc):
    """When the plan itself fails, lamps 03-09 say what the saved settings hold instead of going
    dark: 02 alarms, 04 warns, 05 stays unlit, equipment/OPEX presence still lights 06/08."""
    sc.params = {**(sc.params or {}), "flow.tan_removal_eff": 0}
    sc.equipment = {"water": "fresh", "stages": {sc.unit_id(0): {"pump": {"item": "custom", "capacity": 5}}}}
    sc.opex = {"prices": {"feed_price": 2}}
    sc.save()
    # the plan cache keys on scenario.updated (second-resolution clock ticks can repeat), so empty it:
    # this test is about step_status, not the cache
    from planner import services
    services._plan_cache.clear()
    st = step_status(sc)
    assert st["02"] == "alarm" and st["04"] == "warn" and st["05"] == ""
    assert st["06"] == "ok" and st["08"] == "ok" and st["09"] == ""
    assert any(st[k] for k in ("03", "05", "06", "07", "08", "09"))    # the rail is not all blank
    assert st["06"] == "ok" and st["08"] == "ok" and st["09"] == ""
    assert any(st[k] for k in ("03", "05", "06", "07", "08", "09"))    # the rail is not all blank


def test_lamp_07_lights_for_priced_content_without_capex(client, sc):
    """Lamp 07 also turns ok when a drawn item carries its own price, or an equipment selection
    references a priced standard — no CAPEX save needed."""
    from planner.models import EquipmentStandard
    std = EquipmentStandard.objects.create(category="tank", name="Round 14", capacity=14, unit_price=2000)
    sc.set_diagram(0, {"nodes": [_tank_node("s0", price=900)], "edges": []})
    sc.save()
    assert step_status(sc)["07"] == "ok"                    # priced item drawn in the diagram
    sc.set_diagram(0, None)
    sc.equipment = {"water": "fresh", "stages": {sc.unit_id(0): {"tank": {"item": str(std.pk)}}}}
    sc.save()
    assert step_status(sc)["07"] == "ok"                    # selection points at a priced standard


# --- 6.4 gas safety ----------------------------------------------------------------------------


def _spec_node(nid, kind, **spec):
    return {"id": nid, "type": kind, "position": {"x": 0, "y": 0}, "data": {"spec": spec}}


def _link(a, b):
    return {"id": a + b, "source": a, "sourceHandle": "out" if a.startswith(("mk", "cn")) else "bottom",
            "target": b, "targetHandle": "in"}


def test_auto_o2_set_point_is_capped_at_60(client, sc):
    """A cone on auto facing a heavy load on little flow would ask for supersaturated inlet water;
    the set point caps at 60 mg/L and says so (GBT risk), instead of silently dosing 250 mg/L."""
    fish = {"feed_kgd": 24.0, "biomass_kg": 1000.0, "feed_now_kgh": 2.0,
            "loads": {"tss_kgd": 24.0, "tan_kgd": 1.2, "o2_kgd": 12.0, "co2_kgd": 16.5}}
    r = solve({"nodes": [_spec_node("mk", "makeup", flow_m3h=2, o2=8), _spec_node("cn", "cone"),
                         _spec_node("t", "tank_bottom", volume_m3=10), _spec_node("ds", "discharge")],
               "edges": [_link("mk", "cn"), _link("cn", "t"), _link("t", "ds")],
               "limits": {"o2": {"min": 7.0}}, "basis": {"fish": fish}})
    cone = r["nodes"]["cn"]
    assert cone["o2_target_mgl"] <= 60.0                     # uncapped it would ask 7 + 250 = 257 mg/L
    assert any("capped at 60 mg/L" in w for w in cone["warnings"])


def test_tank_at_its_limit_is_not_outside_it(client, sc):
    """A tank 0.003 mg/L under its O₂ minimum is at the limit, not outside it: the auto-O₂ loop's
    own tolerance applies to the limit warnings too (was a fake 'O₂ 6.00 < 6' alarm at 1e-9)."""
    r = solve({"nodes": [_spec_node("mk", "makeup", flow_m3h=10, o2=5.997),
                         _spec_node("t", "tank_bottom", volume_m3=10), _spec_node("ds", "discharge")],
               "edges": [_link("mk", "t"), _link("t", "ds")], "limits": {"o2": {"min": 6.0}}})
    assert r["edges"]["tds"]["c"]["o2"] == pytest.approx(5.997)
    assert r["nodes"]["t"]["warnings"] == []                 # no 'tank water outside limits'
    targets = {"design_flow_m3h": 10.0, "tan_kgd": 0.0, "tss_kgd": 0.0, "o2_fish_kgd": 0.0, "co2_kgd": 0.0,
               "pure_o2_kgd": None, "makeup_m3h": None, "media_m3": None}
    row = next(x for x in compare(targets, r) if x["check"] == "Tank O₂ (worst tank)")
    assert row["status"] == "ok"                            # the same tolerance in compare


def test_default_jet_design_warns_about_tgp(client, sc):
    """The default jet design's first rows already sit at ~103% total gas pressure: the row loop
    must flag gas-bubble trauma risk instead of presenting the table as safe."""
    from ras_engine import jet
    d = jet.design()
    assert any(x["tgp_pct"] > 103 for x in d["rows"])
    assert any("gas-bubble trauma risk" in m and "TGP" in m for m in d["msgs"])


# --- 6.5 mass-balance closure ------------------------------------------------------------------


def test_solids_shares_over_100_close_the_balance(client, sc):
    """Tank solids shares that sum over 100% used to create mass; they are normalized to 100% and
    the tank says so — everything that enters still leaves."""
    nodes = [_spec_node("mk", "makeup", flow_m3h=10, tss=0, o2=9, temp=12),
             _spec_node("t", "tank_3", volume_m3=10, tss_kgd=24.0, bottom_tss_pct=100, center_tss_pct=30),
             _spec_node("ds", "discharge")]
    edges = [{"id": f"{a}{sh}{b}", "source": a, "sourceHandle": sh, "target": b, "targetHandle": "in"}
             for a, sh, b in (("mk", "out", "t"), ("t", "bottom", "ds"), ("t", "center", "ds"), ("t", "side", "ds"))]
    r = solve({"nodes": nodes, "edges": edges})
    out_tss = sum(e["q"] * e["c"]["tss"] for e in r["edges"].values()) * 24 / 1000
    assert out_tss == pytest.approx(24.0)                  # 10 m³/h × 0 in + the tank's 24 kg/day
    assert any("solids shares sum to 130% — normalized to 100%" in w for w in r["nodes"]["t"]["warnings"])


def test_unsettled_solve_compare_leads_with_convergence_alarm(client, sc):
    """A diagram that does not settle opens the compare table with an alarm row saying the other
    rows are unreliable (consistent with 6.3's lamp 05 warning)."""
    loop = {"nodes": [_spec_node("s", "sump"), _spec_node("p", "pump", flow_m3h=100),
                      _spec_node("t", "tank_bottom", tan_kgd=1.0, tss_kgd=2.0, o2_kgd=3.0),
                      _spec_node("b", "biofilter", tan_removal_pct=50, media_m3=3)],
            "edges": [{"id": f"{a}{b}", "source": a, "sourceHandle": "out" if a != "t" else "bottom",
                       "target": b, "targetHandle": "in"}
                      for a, b in (("s", "p"), ("p", "t"), ("t", "b"), ("b", "s"))]}
    r = solve(loop)
    assert r["converged"] is False
    targets = {"design_flow_m3h": 100.0, "tan_kgd": 1.0, "tss_kgd": 2.0, "o2_fish_kgd": 3.0, "co2_kgd": 0.0,
               "pure_o2_kgd": None, "makeup_m3h": None, "media_m3": None}
    rows = compare(targets, r)
    assert rows[0]["check"] == "Solver convergence" and rows[0]["status"] == "alarm"
    assert "did not settle" in rows[0]["actual"]


def test_implausible_water_warns_globally(client, sc):
    """Water no fish should sit in — -30 °C, or O₂ far above saturation — raises a global warning
    naming the check to run (units, load)."""
    nodes = [_spec_node("mk", "makeup", flow_m3h=10, temp=-30, o2=9, tss=0),
             _spec_node("t", "tank_bottom", volume_m3=10), _spec_node("ds", "discharge")]
    r = solve({"nodes": nodes, "edges": [{"id": "mkt", "source": "mk", "sourceHandle": "out", "target": "t",
                                          "targetHandle": "in"},
                                         {"id": "tds", "source": "t", "sourceHandle": "bottom", "target": "ds",
                                          "targetHandle": "in"}]})
    assert any("water temperature -30.0 °C outside 0–40 °C" in w for w in r["warnings"])
    nodes = [_spec_node("mk", "makeup", flow_m3h=10, temp=12, o2=30, tss=0),
             _spec_node("t", "tank_bottom", volume_m3=10), _spec_node("ds", "discharge")]
    r = solve({"nodes": nodes, "edges": [{"id": "mkt", "source": "mk", "sourceHandle": "out", "target": "t",
                                          "targetHandle": "in"},
                                         {"id": "tds", "source": "t", "sourceHandle": "bottom", "target": "ds",
                                          "targetHandle": "in"}]})
    assert any("far above saturation — check units/load" in w for w in r["warnings"])


# --- 6.6 biofilter UI truth ----------------------------------------------------------------------


def test_biofilter_page_shows_reason_and_both_limits(client, sc):
    """The retention banner names both limits (hard = TAN math, design = what one pass can remove),
    and infeasible rows say why instead of a generic 'flow too low' line."""
    from dataclasses import asdict
    from django.urls import reverse as rev
    from ras_engine import BiofilterParams
    data = asdict(BiofilterParams())
    data["retention_times"] = "25, 1000"        # 1000 min: far beyond the hard limit -> infeasible
    client.post(rev("biofilter", args=[sc.pk]), data)
    html = client.get(rev("biofilter", args=[sc.pk])).content.decode()
    assert "Not feasible:" in html
    assert "the flow cannot keep tank TAN at the target" in html      # the row's own reason
    assert "hard limit:" in html and "design limit (" in html         # two-part banner
    assert "% per pass)" in html


def test_biofilter_safety_notes_are_rendered(client, sc):
    """biofilter_row()'s advisory notes (residence below the design minimum, target TAN below the
    knee) reach the page — computed by the engine but once rendered, deduplicated: the knee note is
    identical in every row so it must not repeat once per retention time."""
    from dataclasses import asdict
    from django.urls import reverse as rev
    from ras_engine import BiofilterParams
    data = asdict(BiofilterParams())
    data["retention_times"] = "3, 30"        # 3 min < 4 min design floor
    data["tan_target"] = 1.0                # below the ~2 mg/L knee
    client.post(rev("biofilter", args=[sc.pk]), data)
    html = client.get(rev("biofilter", args=[sc.pk])).content.decode()
    assert "NO₂ production risk" in html                             # min-retention note
    assert "knee" in html and "media volume explodes" in html        # TAN-knee note
    assert html.count("knee") == 1                                   # deduplicated, not per row
    # the dashboard's biofilter table shows the same advisories when linked to the simulation
    client.post(rev("biofilter_design", args=[sc.pk]), {"design_weight_g": "5"})
    html = client.get(reverse("dashboard", args=[sc.pk])).content.decode()
    assert "NO₂ production risk" in html and "media volume explodes" in html


# --- 6.7 unit labels -------------------------------------------------------------------------------


def test_gas_flows_are_labelled_normlitres(client, sc):
    """O₂/ozone gas flows are normal litres (molar volume 22.4 L/mol → 0 °C): the dashboard, the
    requirements table and the Excel export headers say Nl/min — the numbers themselves unchanged."""
    html = client.get(reverse("dashboard", args=[sc.pk])).content.decode()
    assert "O₂ Nl/min (0 °C)" in html and "Ozone Nl/min (0 °C, pure O₃)" in html
    assert "L/min" not in html
    html = client.get(reverse("requirements", args=[sc.pk])).content.decode()
    assert "Nl/min (0 °C, pure O₃)" in html
    from io import BytesIO
    from openpyxl import load_workbook
    xl = client.get(reverse("export_xlsx", args=[sc.pk]))
    header = [c.value for c in load_workbook(BytesIO(xl.content))["Daily"][1]]
    assert "O₂ Nl/min (0 °C)" in header and "Ozone Nl/min (0 °C, pure O₃)" in header
    assert not [h for h in header if h and "L/min" in h]
