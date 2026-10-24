"""Phase 9 data-safety chains.

9.1 orphan pruning and optimistic concurrency: deleting a RAS unit on step 03 removes that unit's
saved diagram and equipment picks (with a note), inserting one removes nothing, and a units or
CAPEX form saved from a stale tab is refused with 409 instead of silently overwriting; the plan
cache no longer serves a stale plan when two saves share one `updated` tick.

9.2 localhost guard and security settings: RASPlanner refuses requests from other machines, the
secret key can come from the environment, and the response headers stop sniffing and framing.
"""
import pytest
from django.urls import reverse

from planner.models import Scenario

pytestmark = pytest.mark.django_db


@pytest.fixture
def two_units(client):
    """The default scenario with a saved two-unit production plan."""
    client.get("/")
    sc = Scenario.objects.get()
    client.post(reverse("production", args=[sc.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "2", "horizon_unit": "years",
        "stage_name": ["Fry", "Grow"], "stage_max": ["5", "500"], "stage_tanks": ["4", "6"],
        "stage_ret": ["30", "30"]})
    sc.refresh_from_db()
    return sc


def _give_units_private_data(sc, ids):
    """A saved diagram and an equipment pick per unit, under each unit's stable id."""
    for i, uid in enumerate(ids):
        sc.set_diagram(i, {"nodes": [{"id": f"x{i}", "type": "uv", "data": {}}], "edges": []})
    sc.equipment = {**(sc.equipment or {}), "stages": {
        ids[0]: {"tank": {"item": "custom", "capacity": 12, "steps": [[None, 4]]}},
        ids[1]: {"tank": {"item": "custom", "capacity": 40, "steps": [[None, 6]]}}}}
    sc.save()
    sc.refresh_from_db()


# --- deleting a unit takes its saved data with it -----------------------------------------------

def test_deleting_a_unit_on_step_03_removes_its_saved_data(client, two_units):
    sc = two_units
    ids = sc.unit_ids()
    _give_units_private_data(sc, ids)
    # the form posts only the first row: the user deleted the second unit
    resp = client.post(reverse("units", args=[sc.pk]), follow=True, data={
        "stage_id": [ids[0]], "stage_name": ["Fry"], "stage_max": ["5"],
        "stage_tanks": ["4"], "stage_ret": ["30"]})
    html = resp.content.decode()
    assert "Removed data for 1 deleted RAS unit" in html                  # the user is told
    sc.refresh_from_db()
    assert set(sc.process or {}) == {ids[0]}                              # its diagram is gone
    assert set((sc.equipment or {}).get("stages") or {}) == {ids[0]}      # and its equipment pick
    assert sc.equipment_of(0)["tank"]["capacity"] == 12                   # the live unit kept its own


def test_inserting_a_unit_on_step_03_keeps_every_units_data(client, two_units):
    """The mirror of the delete: a new row inserted in front moves nothing (ids are stable)."""
    sc = two_units
    ids = sc.unit_ids()
    _give_units_private_data(sc, ids)
    client.post(reverse("units", args=[sc.pk]), {
        "stage_id": ["", *ids], "stage_name": ["Hatch", "Fry", "Grow"],
        "stage_max": ["1", "5", "500"], "stage_tanks": ["2", "4", "6"], "stage_ret": ["30", "30", "30"]})
    sc.refresh_from_db()
    new_ids = sc.unit_ids()
    assert new_ids[1:] == ids and new_ids[0] not in ids
    assert set(sc.process or {}) == set(ids)                              # nothing was pruned
    assert set((sc.equipment or {}).get("stages") or {}) == set(ids)
    assert sc.diagram_of(2)["nodes"][0]["id"] == "x1" and sc.diagram_of(0) is None
    assert "Removed data" not in client.get(reverse("units", args=[sc.pk])).content.decode()


# --- optimistic concurrency: a stale tab cannot silently overwrite ------------------------------

def test_units_post_with_a_stale_updated_is_refused_with_409(client, two_units):
    sc = two_units
    url = reverse("units", args=[sc.pk])
    assert 'name="updated"' in client.get(url).content.decode()           # the form carries the token
    before = sc.production
    r = client.post(url, {"updated": "2020-01-01T00:00:00+00:00",
                          "stage_id": sc.unit_ids(), "stage_name": ["Fry", "Grow"],
                          "stage_max": ["5", "600"], "stage_tanks": ["4", "6"], "stage_ret": ["30", "30"]})
    assert r.status_code == 409
    html = r.content.decode()
    assert "saved by another tab" in html and "Reload the page and re-apply" in html
    assert 'value="600"' in html                                          # what was typed is still shown
    sc.refresh_from_db()
    assert sc.production == before                                        # but nothing was saved
    # the fresh token from the re-rendered page saves as before
    r = client.post(url, {"updated": sc.updated.isoformat(),
                          "stage_id": sc.unit_ids(), "stage_name": ["Fry", "Grow"],
                          "stage_max": ["5", "600"], "stage_tanks": ["4", "6"], "stage_ret": ["30", "30"]})
    assert r.status_code == 302
    sc.refresh_from_db()
    assert sc.production["stages"][1]["max_weight_g"] == 600.0


def test_capex_post_with_a_stale_updated_is_refused_with_409(client, two_units):
    from planner.models import EquipmentStandard
    sc = two_units
    tank = EquipmentStandard.objects.create(category="tank", name="Round 50", capacity=50, unit_price=4000)
    client.post(reverse("equipment", args=[sc.pk]), {
        "n_stages": "2", "water": "fresh", f"s0-tank-item": str(tank.pk), f"s1-tank-item": str(tank.pk)})
    sc.refresh_from_db()
    url = reverse("capex", args=[sc.pk])
    assert 'name="updated"' in client.get(url).content.decode()
    key = client.get(url).context["lines"][0]["key"]
    post = {"currency": "EUR", "line_key": [key], f"price-{key}": "999",
            "x_name": [""], "x_group": ["Other"], "x_mode": ["amount"],
            "x_qty": [""], "x_price": [""], "x_pct": [""]}
    before = sc.capex
    r = client.post(url, {"updated": "2020-01-01T00:00:00+00:00", **post})
    assert r.status_code == 409 and "saved by another tab" in r.content.decode()
    assert 'value="999"' in r.content.decode()                            # typed, not saved
    sc.refresh_from_db()
    assert sc.capex == before
    r = client.post(url, {"updated": sc.updated.isoformat(), **post})
    assert r.status_code == 302
    sc.refresh_from_db()
    assert sc.capex["price_overrides"] == {key: 999.0}


# --- the plan cache: two saves in one clock tick ------------------------------------------------

def test_prewarm_after_two_saves_in_one_clock_tick_serves_the_second_state(client, db):
    """`updated` is the cache key, but SQLite can land two saves on the same tick; prewarm now
    evicts the scenario first, so the entry under that shared key cannot survive as stale."""
    from planner import services
    client.get("/")
    sc = Scenario.objects.get()
    services._plan_cache.clear()
    services.prewarm(sc)
    (plan1, _res), _err = services.plan_result(sc)
    assert plan1.interval_days == 30                                      # the default plan, warm
    tick = sc.updated
    # a second save inside the same clock tick: the blob changes, `updated` cannot move
    Scenario.objects.filter(pk=sc.pk).update(production={"interval_days": 17}, updated=tick)
    sc.refresh_from_db()
    assert sc.updated.isoformat() == tick.isoformat()                     # same key, new state
    services.prewarm(sc)
    (plan2, _res), _err = services.plan_result(sc)
    assert plan2.interval_days == 17                                      # the SECOND state, not the cached first


# --- 9.2 the localhost guard and security settings ----------------------------------------------

def test_requests_from_other_machines_are_refused(client, db):
    r = client.get("/", REMOTE_ADDR="192.168.1.5")
    assert r.status_code == 403
    assert "RASPlanner is a localhost-only tool." in r.content.decode()
    # localhost (the test client's default REMOTE_ADDR) still runs the normal workflow
    r = client.get("/")
    assert r.status_code == 302 and Scenario.objects.count() == 1
    assert client.get("/", REMOTE_ADDR="::1").status_code == 302


def test_security_settings_and_secret_key_from_env():
    """The hardening flags are on, and the secret key falls back to the dev-only default."""
    import os
    from django.conf import settings
    assert settings.SECURE_CONTENT_TYPE_NOSNIFF is True
    assert settings.X_FRAME_OPTIONS == "DENY"
    assert settings.SECRET_KEY == os.environ.get("RASPLAN_SECRET_KEY", "dev-only-rasplan-local")


def test_ai_settings_page_suggests_environment_keys(client, db):
    from django.urls import reverse
    html = client.get(reverse("ai_settings")).content.decode()
    assert "environment variables (RASPLAN_*_KEY)" in html
