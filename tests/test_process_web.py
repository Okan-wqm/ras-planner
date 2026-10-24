import json

import pytest
from django.urls import reverse

from planner.models import EquipmentStandard, Scenario

pytestmark = pytest.mark.django_db


@pytest.fixture
def sc(client):
    client.get("/")
    s = Scenario.objects.get()
    client.post(reverse("production", args=[s.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "1", "horizon_unit": "years",
        "stage_name": ["Fry", "Fingerling"], "stage_max": ["2", "5"], "stage_tanks": ["3", "4"], "stage_ret": ["20", "20"]})
    s.refresh_from_db()
    return s


def test_page_shows_automatic_layout_per_ras(client, sc):
    resp = client.get(reverse("process", args=[sc.pk]) + "?ras=1")
    assert resp.status_code == 200
    editor = resp.context["editor"]
    assert editor["isDefault"] and editor["index"] == 1
    tanks = [n for n in editor["diagram"]["nodes"] if n["type"] == "tank"]
    assert len(tanks) == 4  # tanks per RAS unit from the production plan


def test_solve_save_and_capex(client, sc):
    editor = client.get(reverse("process", args=[sc.pk])).context["editor"]
    diagram = editor["diagram"]
    solved = client.post(editor["urls"]["solve"], json.dumps(diagram), content_type="application/json").json()
    assert solved["ok"] and solved["converged"]

    # give two tanks a priced standard, one an own price, then save
    std = EquipmentStandard.objects.create(category="tank", name="Round 10", capacity=10, unit_price=2000)
    tanks = [n for n in diagram["nodes"] if n["type"] == "tank"]
    tanks[0]["data"]["standard"] = str(std.pk)
    tanks[1]["data"]["standard"] = str(std.pk)
    tanks[2]["data"]["unit_price"] = 500
    saved = client.post(editor["urls"]["save"], json.dumps(diagram), content_type="application/json").json()
    assert saved["saved"] and saved["ok"]
    sc.refresh_from_db()
    assert len(sc.diagram_of(0)["nodes"]) == len(diagram["nodes"])

    capex = client.get(reverse("capex", args=[sc.pk])).context
    ras0 = [ln for ln in capex["lines"] if ln["stage_index"] == 0]
    assert all(ln.get("from_diagram") for ln in ras0)  # diagram replaces Equipment selections
    tank_lines = [ln for ln in ras0 if ln["category"].startswith("Fish tank")]
    # identical tanks are one line: the two on the standard, the one with its own price
    assert sorted((ln["qty"], ln["unit_price"]) for ln in tank_lines) == [(1, 500), (2, 2000)]
    # connections are piping, not equipment: the default manifold (a splitter) is not a CAPEX line
    assert not [ln for ln in ras0 if ln["category"] in ("Splitter", "Connection point")]
    assert sum(ln["total"] or 0 for ln in tank_lines) == 2000 + 2000 + 500

    # deleting a tank from the diagram removes its CAPEX line
    diagram["nodes"] = [n for n in diagram["nodes"] if n["id"] != tanks[0]["id"]]
    diagram["edges"] = [e for e in diagram["edges"] if tanks[0]["id"] not in (e["source"], e["target"])]
    client.post(editor["urls"]["save"], json.dumps(diagram), content_type="application/json")
    capex = client.get(reverse("capex", args=[sc.pk])).context
    tank_lines = [ln for ln in capex["lines"] if ln["stage_index"] == 0 and ln["category"].startswith("Fish tank")]
    assert len(tank_lines) == 2

    # the equipment page lists what the diagram holds
    eq = client.get(reverse("equipment", args=[sc.pk])).context
    assert len([ln for ln in eq["units"][0]["diagram_lines"] if ln["category"].startswith("Fish tank")]) == 2

    # reset brings the automatic layout back
    client.post(reverse("process_reset", args=[sc.pk, 0]))
    sc.refresh_from_db()
    assert "0" not in sc.process


def test_library_save_load_overwrite_delete(client, sc):
    from planner.models import ProcessTemplate
    editor = client.get(reverse("process", args=[sc.pk])).context["editor"]
    url = editor["urls"]["library"]
    d = editor["diagram"]
    res = client.post(url, json.dumps({"name": "4 tanks + drum", "source": "test", "diagram": d}),
                      content_type="application/json").json()
    assert res["ok"] and res["created"] and len(res["library"]) == 1
    tid = res["id"]
    # only equipment counts: make-up, discharge and connections (splitter/junction) do not
    assert res["library"][0]["items"] == sum(1 for n in d["nodes"]
                                             if n["type"] not in ("makeup", "discharge", "splitter", "junction"))
    # same name overwrites instead of duplicating
    d2 = {**d, "nodes": d["nodes"][:3], "edges": []}
    res = client.post(url, json.dumps({"name": "4 tanks + drum", "diagram": d2}), content_type="application/json").json()
    assert not res["created"] and ProcessTemplate.objects.count() == 1
    loaded = client.get(editor["urls"]["libraryItem"].replace("{id}", str(tid))).json()
    assert len(loaded["diagram"]["nodes"]) == 3
    # the library is shared: another RAS unit sees it
    other = client.get(reverse("process", args=[sc.pk]) + "?ras=1").context["editor"]
    assert [t["name"] for t in other["library"]] == ["4 tanks + drum"]
    # empty name is refused, delete removes it
    assert client.post(url, json.dumps({"name": " ", "diagram": d}), content_type="application/json").status_code == 400
    res = client.post(editor["urls"]["libraryItem"].replace("{id}", str(tid))).json()
    assert res["library"] == [] and ProcessTemplate.objects.count() == 0


def test_saved_diagram_is_loaded_back(client, sc):
    editor = client.get(reverse("process", args=[sc.pk]) + "?ras=1").context["editor"]
    d = editor["diagram"]
    d["nodes"][0]["position"] = {"x": 1234, "y": 567}
    d["nodes"][0]["data"]["name"] = "My tank"
    client.post(editor["urls"]["save"], json.dumps(d), content_type="application/json")
    again = client.get(reverse("process", args=[sc.pk]) + "?ras=1").context["editor"]
    assert not again["isDefault"]
    n0 = again["diagram"]["nodes"][0]
    assert n0["position"] == {"x": 1234, "y": 567} and n0["data"]["name"] == "My tank"
    # RAS 0 is untouched
    assert client.get(reverse("process", args=[sc.pk])).context["editor"]["isDefault"]


def test_solver_errors_are_reported(client, sc):
    loop = {"nodes": [{"id": "a", "type": "sump", "data": {}}, {"id": "b", "type": "sump", "data": {}}],
            "edges": [{"id": "1", "source": "a", "sourceHandle": "out", "target": "b"},
                      {"id": "2", "source": "b", "sourceHandle": "out", "target": "a"}]}
    res = client.post(reverse("process_solve", args=[sc.pk]), json.dumps(loop), content_type="application/json").json()
    assert not res["ok"] and "pump" in res["error"]
