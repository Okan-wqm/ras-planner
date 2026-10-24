import math

import pytest
from django.urls import reverse

from planner.models import Scenario
from ras_engine import BiofilterParams, Params, design_point, link_to_simulation, simulate


@pytest.fixture(scope="module")
def rows():
    return simulate(Params())


def test_design_point_first_day_reaching_weight(rows):
    day = design_point(rows, 5)
    assert day["weight_g"] >= 5
    assert rows[day["day"] - 2]["weight_g"] < 5


def test_link_uses_simulation_values(rows):
    bp = BiofilterParams(design_weight_g=5, n_ras_units=2, n_tanks=8)
    linked, info = link_to_simulation(bp, rows)
    day = design_point(rows, 5)
    assert info["day"] == day["day"]
    assert math.isclose(linked.daily_feed_kg, day["feed_kg"] / 2 * 4 / 3)
    assert math.isclose(linked.tank_volume_m3 * 8 * 2, day["volume_m3"])
    assert linked.temperature == day["temperature"] and linked.protein == day["protein"]


def test_max_retention_is_feasibility_limit(rows):
    from ras_engine import biofilter_row, max_retention_min
    linked, info = link_to_simulation(BiofilterParams(design_weight_g=5, max_removal_pct=100), rows)
    t = max_retention_min(linked)
    assert math.isclose(info["max_retention_min"], t)
    assert biofilter_row(linked, t * 0.99)["feasible"]
    assert not biofilter_row(linked, t * 1.01)["feasible"]
    assert math.isclose(biofilter_row(linked, t)["tan_out"], 0, abs_tol=1e-9)


def test_no_link_when_weight_zero(rows):
    bp = BiofilterParams()
    linked, info = link_to_simulation(bp, rows)
    assert info is None and linked is bp


def test_weight_never_reached_uses_last_day(rows):
    _, info = link_to_simulation(BiofilterParams(design_weight_g=1e9), rows)
    assert not info["reached"] and info["day"] == rows[-1]["day"]


@pytest.mark.django_db
def test_dashboard_design_weight(client):
    client.get("/")
    sc = Scenario.objects.get()
    resp = client.post(reverse("biofilter_design", args=[sc.pk]), {"design_weight_g": "5"})
    assert resp.status_code == 302 and resp.url.endswith("#biofilter")
    sc.refresh_from_db()
    assert sc.biofilter["design_weight_g"] == 5.0
    html = client.get(reverse("dashboard", args=[sc.pk])).content.decode()
    assert "From the simulation" in html
    # linked fields are read-only on the biofilter page; saving keeps the old manual values
    page = client.get(reverse("biofilter", args=[sc.pk])).content.decode()
    assert "← from simulation" in page
