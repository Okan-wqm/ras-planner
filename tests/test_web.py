import pytest
from django.urls import reverse

from planner.models import Feed, Scenario

pytestmark = pytest.mark.django_db


def test_dashboard_creates_default_scenario(client):
    resp = client.get("/")
    assert resp.status_code == 302 and resp["Location"].endswith("/brief/")   # the workflow starts at the brief
    assert Scenario.objects.count() == 1
    sc = Scenario.objects.get()
    assert "Final weight" in client.get(reverse("dashboard", args=[sc.pk])).content.decode()


def test_edit_params_changes_result(client):
    client.get("/")
    sc = Scenario.objects.get()
    from ras_engine import Params
    data = {k: v for k, v in Params().to_flat().items()}
    data.update({"name": "Test", "temperature_steps": "1:12, 100:14", "use_feed_catalog": "on",
                 "growth.days": 200, "growth.max_density": 50})
    resp = client.post(reverse("scenario_edit", args=[sc.pk]), data)
    assert resp.status_code == 302
    sc.refresh_from_db()
    assert sc.engine_params().growth.days == 200
    assert sc.engine_params().growth.max_density == 50
    assert client.get(reverse("dashboard", args=[sc.pk])).status_code == 200


def test_feed_catalog_and_pages(client):
    client.get("/")
    sc = Scenario.objects.get()
    resp = client.post(reverse("feeds"), {"name": "BioMar 0-5", "min_weight_g": 0, "max_weight_g": 5,
                                          "protein_pct": 58, "fcr": 0.8})
    assert resp.status_code == 302 and Feed.objects.count() == 1
    bad = client.post(reverse("feeds"), {"name": "x", "min_weight_g": 5, "max_weight_g": 1,
                                         "protein_pct": 50, "fcr": 1})
    assert bad.status_code == 200 and Feed.objects.count() == 1
    # the catalog must cover the whole growth range: the feed-gap guard (Phase 2a) replaces the silent
    # default-FCR fallback beyond the last feed
    resp = client.post(reverse("feeds"), {"name": "BioMar 5-6000", "min_weight_g": 5, "max_weight_g": 6000,
                                          "protein_pct": 52, "fcr": 1.0})
    assert resp.status_code == 302 and Feed.objects.count() == 2
    assert "BioMar 0-5" in client.get(reverse("dashboard", args=[sc.pk])).content.decode()
    assert client.get(reverse("biofilter", args=[sc.pk])).status_code == 200
    xl = client.get(reverse("export_xlsx", args=[sc.pk]))
    assert xl.status_code == 200 and xl.content[:2] == b"PK"


def test_production_page(client):
    client.get("/")
    sc = Scenario.objects.get()
    url = reverse("production", args=[sc.pk])
    resp = client.get(url)
    assert resp.status_code == 200 and "Edit RAS units and tanks" in resp.content.decode()
    req = client.get(reverse("requirements", args=[sc.pk]))
    assert req.status_code == 200 and "Peak loads and requirements per RAS unit" in req.content.decode()
    data = {"start_date": "2027-01-01", "interval_days": "40", "horizon_days": "600",
            "stage_name": ["Hatchery", "Fingerling"], "stage_max": ["2", "5"],
            "stage_tanks": ["4", "6"], "stage_ret": ["30", "25"]}
    assert client.post(url, data).status_code == 302
    sc.refresh_from_db()
    assert sc.production["interval_days"] == 40
    assert [s["max_weight_g"] for s in sc.production["stages"]] == [2.0, 5.0]
    html = client.get(url).content.decode()
    assert "Hatchery" in html and "Harvest calendar" in html
    # per-unit constants (step 03): only values that differ from the scenario are saved
    page = client.get(reverse("units", args=[sc.pk])).content.decode()
    assert 'name="ov_gas.pure_oxygen"' in page
    from planner.forms import scenario_base_values, stage_field_specs
    base = scenario_base_values(sc)
    ov = {}
    for key, *_rest, default in stage_field_specs():
        v = base[key]
        vals = [str(v), str(v)]
        if isinstance(default, bool):
            vals = [str(bool(v)), str(bool(v))]
        ov["ov_" + key] = vals
    ov["ov_gas.pure_oxygen"] = ["False", "True"]
    ov["ov_growth.temperature"] = ["10", str(base["growth.temperature"])]
    ov["ov_bio.media_area_m2m3"] = [str(base["bio.media_area_m2m3"]), "800"]
    assert client.post(url, {**data, **ov}).status_code == 302
    sc.refresh_from_db()
    assert sc.production["stages"][0]["overrides"] == {"gas.pure_oxygen": False, "growth.temperature": 10.0}
    assert sc.production["stages"][1]["overrides"] == {"bio.media_area_m2m3": 800.0}
    plan, stages = sc.engine_plan()
    assert stages[0].params.gas.pure_oxygen is False and stages[1].params.gas.pure_oxygen is True
    assert stages[1].biofilter.media_area_m2m3 == 800.0
    html = client.get(reverse("requirements", args=[sc.pk])).content.decode()
    assert "Aeration" in html
    # non-increasing weights are rejected
    bad = dict(data, stage_max=["5", "2"])
    client.post(url, bad)
    sc.refresh_from_db()
    assert [s["max_weight_g"] for s in sc.production["stages"]] == [2.0, 5.0]


@pytest.mark.parametrize("value,unit,days", [("26", "weeks", 182), ("18", "months", 548), ("3", "years", 1096)])
def test_horizon_units(client, value, unit, days):
    client.get("/")
    sc = Scenario.objects.get()
    url = reverse("production", args=[sc.pk])
    data = {"start_date": "2027-01-01", "interval_days": "30", "horizon_value": value, "horizon_unit": unit,
            "stage_name": ["Fry"], "stage_max": ["5"], "stage_tanks": ["4"], "stage_ret": ["30"]}
    assert client.post(url, data).status_code == 302
    sc.refresh_from_db()
    assert sc.production["horizon_days"] == days
    assert sc.plan_settings()["horizon_unit"] == unit
    # more than 3 years is rejected
    client.post(url, dict(data, horizon_value="4", horizon_unit="years"))
    sc.refresh_from_db()
    assert sc.production["horizon_days"] == days


def test_standards_and_equipment(client):
    from planner.models import EquipmentStandard
    client.get("/")
    sc = Scenario.objects.get()
    client.post(reverse("production", args=[sc.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "1", "horizon_unit": "years",
        "stage_name": ["Fry", "Fingerling"], "stage_max": ["2", "5"], "stage_tanks": ["4", "4"], "stage_ret": ["30", "30"]})
    assert client.post(reverse("standards"), {"category": "tank", "name": "Round 10", "capacity": "10",
                                              "unit_price": "2500"}).status_code == 302
    assert client.post(reverse("standards"), {"category": "pump", "name": "P-50", "capacity": "50"}).status_code == 302
    bad = client.post(reverse("standards"), {"category": "pump", "name": "x", "capacity": "0"})
    assert bad.status_code == 200 and EquipmentStandard.objects.count() == 2
    tank = EquipmentStandard.objects.get(category="tank")
    pump = EquipmentStandard.objects.get(category="pump")
    url = reverse("equipment", args=[sc.pk])
    assert client.get(url).status_code == 200
    resp = client.post(url, {"n_stages": "2", "water": "fresh",
                             "s0-tank-item": str(tank.pk), "s0-tank-upto": [""], "s0-tank-k": [""],
                             "s1-tank-item": "custom", "s1-tank-cap": "3",
                             # graded: up to 3 g one tank, then four (entered out of order)
                             "s1-tank-upto": ["", "3"], "s1-tank-k": ["4", "1"],
                             "s0-pump-item": str(pump.pk)})
    assert resp.status_code == 302
    sc.refresh_from_db()
    assert sc.equipment_of(0)["tank"]["item"] == str(tank.pk)
    assert sc.equipment_of(0)["tank"]["steps"] == []
    assert sc.equipment_of(1)["tank"]["steps"] == [[3.0, 1], [None, 4]]
    html = client.get(reverse("units", args=[sc.pk])).content.decode()  # tanks are shown on step 03
    assert "Tank occupancy" in html and "Round 10" in html and "Suggested from the tank size" in html
    assert "3.0–exit g" in html  # second grading step shown


def test_biofilter_use_peak_feed(client):
    client.get("/")
    sc = Scenario.objects.get()
    from dataclasses import asdict
    from ras_engine import BiofilterParams
    data = asdict(BiofilterParams())
    data["use_peak"] = "1"
    client.post(reverse("biofilter", args=[sc.pk]), data)
    sc.refresh_from_db()
    from ras_engine import simulate, summary
    peak = summary(simulate(sc.engine_params()))["peak_feed_kg"]
    assert sc.biofilter["daily_feed_kg"] == round(peak / 2 * 4 / 3, 2)
