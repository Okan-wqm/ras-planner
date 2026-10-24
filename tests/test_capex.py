import math

import pytest
from django.urls import reverse

from planner.models import EquipmentStandard, Scenario

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(client):
    client.get("/")
    sc = Scenario.objects.get()
    client.post(reverse("production", args=[sc.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "1", "horizon_unit": "years",
        "stage_name": ["Fry"], "stage_max": ["5"], "stage_tanks": ["4"], "stage_ret": ["30"]})
    tank = EquipmentStandard.objects.create(category="tank", name="Round 50", capacity=50, unit_price=4000)
    media = EquipmentStandard.objects.create(category="biomedia", name="K1 bag", capacity=1, unit_price=350)
    client.post(reverse("equipment", args=[sc.pk]), {
        "n_stages": "1", "water": "fresh",
        "s0-tank-item": str(tank.pk), "s0-tank-upto": [""], "s0-tank-k": ["2"],
        "s0-biomedia-item": str(media.pk),
        "s0-pump-item": "custom", "s0-pump-cap": "5000", "s0-pump-qty": "2"})
    sc.refresh_from_db()
    return sc


def test_biomedia_required_is_media_volume(client, setup):
    from planner.views import _equipment_units, _run_production
    _plan, res = _run_production(setup)
    units, _o, _w = _equipment_units(setup, res)
    media_row = next(r for r in units[0]["rows"] if r["key"] == "biomedia")
    assert math.isclose(media_row["required"], res["designs"][0]["biofilter"]["media_fresh_m3"])
    assert media_row["qty"] == math.ceil(media_row["required"])


def test_capex_totals(client, setup):
    sc = setup
    url = reverse("capex", args=[sc.pk])
    page = client.get(url)
    assert page.status_code == 200
    ctx = page.context
    lines = {ln["category"]: ln for ln in ctx["lines"]}
    assert lines["Tank"]["total"] == lines["Tank"]["qty"] * 4000
    assert lines["Biomedia (plastic carrier)"]["total"] == lines["Biomedia (plastic carrier)"]["qty"] * 350
    assert lines["Recirculation pump"]["total"] is None and ctx["missing"] == 1  # custom, no price yet
    equip = lines["Tank"]["total"] + lines["Biomedia (plastic carrier)"]["total"]
    assert ctx["equipment_total"] == equip

    # price the custom pump, add a building (amount) and installation (15 % of equipment)
    pump_key = lines["Recirculation pump"]["key"]
    keys = [ln["key"] for ln in ctx["lines"]]
    resp = client.post(url, {
        "currency": "EUR", "line_key": keys, f"price-{pump_key}": "12000",
        "x_name": ["Hall", "Installation", ""], "x_group": ["Building & civil works", "Installation", "Other"],
        "x_mode": ["amount", "percent", "amount"], "x_qty": ["1200", "", "1"],
        "x_price": ["300", "", ""], "x_pct": ["", "15", ""]})
    assert resp.status_code == 302
    ctx = client.get(url).context
    equip += 2 * 12000
    assert ctx["equipment_total"] == equip and ctx["missing"] == 0
    assert ctx["extras_total"] == 1200 * 300 + equip * 0.15
    assert math.isclose(ctx["grand_total"], equip + 1200 * 300 + equip * 0.15)
    if ctx["annual_harvest_t"]:
        assert math.isclose(ctx["per_tonne"], ctx["grand_total"] / ctx["annual_harvest_t"])


def _node(nid, kind, **data):
    return {"id": nid, "type": kind, "position": {"x": 0, "y": 0}, "data": {"spec": {"volume_m3": 14}, **data}}


def test_equipment_lists_what_the_diagram_holds(client, setup):
    """Equipment shows the diagram's items grouped by type with their count; deleting one there removes it here,
    and a price chosen here is written back to those items."""
    sc = setup
    nodes = [_node(f"s{k}", "tank") for k in range(3)] + [_node(f"b{k}", "tank_bottom") for k in range(3)]
    sc.set_diagram(0, {"nodes": nodes, "edges": []})
    sc.save()
    url = reverse("equipment", args=[sc.pk])
    html = client.get(url).content.decode()
    assert "Installed — from the process diagram" in html
    from planner.views import _diagram_lines
    lines = {ln["type"]: ln for ln in _diagram_lines(sc, 0, "Fry", {})}
    assert lines["tank"]["qty"] == 3 and lines["tank_bottom"]["qty"] == 3

    sc.set_diagram(0, {"nodes": nodes[:-1], "edges": []})       # one bottom-flow tank deleted in the diagram
    sc.save()
    lines = {ln["type"]: ln for ln in _diagram_lines(sc, 0, "Fry", {})}
    assert lines["tank_bottom"]["qty"] == 2

    g = lines["tank"]
    gkey = g["gkey"]  # explicit field on the line (Phase 6.2), no longer parsed from the key
    client.post(url, {"n_stages": "1", "water": "fresh", f"d0-{gkey}-nodes": ",".join(g["node_ids"]),
                      f"d0-{gkey}-std": "", f"d0-{gkey}-price": "5200"})
    sc.refresh_from_db()
    prices = {n["id"]: n["data"].get("unit_price") for n in sc.diagram_of(0)["nodes"]}
    assert all(prices[f"s{k}"] == 5200 for k in range(3)) and prices["b0"] in ("", None)
    assert "15,600" in client.get(url).content.decode()            # 3 × 5,200 for the unit


def test_settler_volume_from_flow_and_residence_time(client, setup):
    """A radial flow settler is sized by flow × residence time; UV and heat exchangers by the water through them."""
    sc = setup
    nodes = [{"id": "mk", "type": "makeup", "position": {"x": 0, "y": 0}, "data": {"spec": {"flow_m3h": 0.6}}},
             {"id": "hx", "type": "hx_plate", "position": {"x": 0, "y": 0}, "data": {"spec": {}}},
             {"id": "uv", "type": "uv", "position": {"x": 0, "y": 0}, "data": {"spec": {}}},
             {"id": "rf", "type": "rfs", "position": {"x": 0, "y": 0}, "data": {"spec": {"residence_min": 5}}},
             {"id": "ds", "type": "discharge", "position": {"x": 0, "y": 0}, "data": {}}]
    pipe = lambda a, b, sh="out": {"id": f"{a}{b}", "source": a, "sourceHandle": sh, "target": b, "targetHandle": "in"}  # noqa: E731
    sc.set_diagram(0, {"nodes": nodes, "edges": [pipe("mk", "hx"), pipe("hx", "uv"), pipe("uv", "rf"), pipe("rf", "ds"),
                                                  pipe("rf", "ds", "sludge") | {"id": "rfds2"}]})
    sc.save()
    from planner.views import _diagram_lines
    items = {ln["type"]: ln["item"] for ln in _diagram_lines(sc, 0, "Fry", {})}
    assert items["rfs"].startswith("0.05 m³ (0.6 m³/h × 5 min)")
    assert items["uv"] == "0.6 m³/h" and items["hx_plate"].startswith("0.6 m³/h")
