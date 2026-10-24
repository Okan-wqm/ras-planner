"""Input guards (Phase 2): invalid inputs raise EngineInputError instead of dividing by zero or
looping on impossible values; growth never reverses below Tbase."""
import re
import time

import pytest

from ras_engine import (BiofilterParams, EngineInputError, Feed, Params, biofilter_table, jet,
                        simulate, summary)
from ras_engine.formulas import (daily_length_increase, flow_co2_lpm, flow_nitrate_m3d,
                                 flow_oxygen_m3h, flow_tan_m3h, flow_tss_lpm)


def test_engine_input_error_is_a_value_error():
    """Existing `except ValueError` handlers keep working."""
    assert issubclass(EngineInputError, ValueError)


@pytest.mark.parametrize("call, message", [
    (lambda: flow_oxygen_m3h(1.0, 12, 8, 0), "Oxygen transfer efficiency must be positive"),
    (lambda: flow_tan_m3h(100, 0.5, 0.092, 0, 1.0, 0.0), "TAN removal efficiency must be positive"),
    (lambda: flow_tan_m3h(100, 0.5, 0.092, 0.65, 1.0, 1.0), "TAN target must be below the tank limit"),
    (lambda: flow_co2_lpm(1.0, 0, 15, 0.5), "CO₂ removal efficiency must be positive"),
    (lambda: flow_co2_lpm(1.0, 0.66, 15, 15.0), "CO₂ target must be below the tank limit"),
    (lambda: flow_tss_lpm(100, 0.35, 0, 10.5, 0.5), "TSS removal efficiency must be positive"),
    (lambda: flow_tss_lpm(100, 0.35, 0.5, 10.5, 10.5), "TSS target must be below the tank limit"),
    (lambda: flow_nitrate_m3d(100, 0.5, 0.092, 0, 0, 100, 100),
     "Make-up water nitrate (100) is at or above the tank limit (100)"),
])
def test_flow_denominators_guarded(call, message):
    with pytest.raises(EngineInputError, match=re.escape(message)):
        call()


@pytest.mark.parametrize("key, value, message", [
    ("growth.initial_count", 0, "Fish stocked per batch must be above zero"),
    ("growth.initial_weight_g", 0, "Stocking weight must be above zero"),
    ("growth.condition_factor", 0, "Condition factor (CF) must be above zero"),
    ("growth.tu_base", 0, "Tubase must be above zero"),
    ("growth.density_length_divisor", 0, "Density coefficient (L / x) must be above zero"),
    ("growth.max_density", 0, "Maximum stocking density must be above zero"),
])
def test_simulate_rejects_impossible_constants(key, value, message):
    with pytest.raises(EngineInputError, match=re.escape(message)):
        simulate(Params.from_flat({key: value}))


def test_daily_length_increase_clamped_at_zero():
    assert daily_length_increase(-2, 0, 5.12) == 0.0
    assert daily_length_increase(0, 0, 5.12) == 0.0  # exactly at Tbase: no growth either
    assert daily_length_increase(12, 0, 5.12) == pytest.approx(12 / 5.12 / 30.5)


def test_simulation_survives_400_days_below_tbase():
    """Whole run below Tbase: nothing crashes, feed never goes negative, fish never lose weight."""
    rows = simulate(Params.from_flat({"growth.days": 400}), temperature_steps="1:-2")
    assert len(rows) == 400
    assert all(r["feed_kg"] >= 0 for r in rows)
    weights = [r["weight_g"] for r in rows]
    assert all(b >= a - 1e-9 for a, b in zip(weights, weights[1:]))  # never shrinks (round-trip aside)
    assert weights[-1] >= weights[0] - 1e-9  # stays at the stocking weight


def test_parse_steps_accepts_calendar_dates():
    """'1 Jan:10, 10 Feb:12' parses anywhere steps are parsed — one format family, one parser."""
    from ras_engine.params import parse_steps, step_value_interp
    steps = parse_steps("1 Jan:10, 10 Feb:12, 10 May:14, 1 Sep:12, 1 Nov:8")
    assert steps == [(1, 10.0), (41, 12.0), (130, 14.0), (244, 12.0), (305, 8.0)]
    assert step_value_interp(steps, 20, 12.0) == pytest.approx(10.95)   # ramps between steps
    assert step_value_interp(steps, 400, 12.0) == pytest.approx(8.0)    # holds the last step
    # mixed formats and plain day numbers keep working
    assert parse_steps("1:15, 56:30") == [(1, 15.0), (56, 30.0)]
    assert parse_steps("10-02:12") == [(41, 12.0)]


def test_feed_catalog_gap_raises():
    feeds = [Feed("Fry", 0, 100, 0.55, 1.0)]
    with pytest.raises(EngineInputError, match="extend the feed table"):
        simulate(Params.from_flat({"growth.days": 300}), feeds=feeds)


def test_feed_catalog_interior_gap_message_lists_the_ranges():
    """A hole between two ranges names both bounds — not just the table's last end."""
    feeds = [Feed("Fry", 0, 100, 0.55, 1.0), Feed("Growout", 150, 6000, 0.5, 1.1)]
    with pytest.raises(EngineInputError, match=r"covers 120 g.*ranges: 0–100, 150–6000 g"):
        simulate(Params.from_flat({"growth.initial_weight_g": 120}), feeds=feeds)


def test_feed_catalog_full_range_passes():
    feeds = [Feed("Fry", 0, 100, 0.55, 1.0), Feed("Growout", 100, 6000, 0.5, 1.1)]
    rows = simulate(Params.from_flat({"growth.days": 300}), feeds=feeds)
    assert rows[-1]["feed_name"] == "Growout"


def test_no_catalog_uses_default_fcr():
    rows = simulate(Params.from_flat({"growth.days": 10}))
    assert all(r["feed_name"] == "" and r["fcr"] == Params().feed.default_fcr for r in rows)


def test_economic_fcr_none_for_degenerate_plan():
    """Mortality 1 %/day: the feed/biomass ratio is absurd (~32) — reported as None, not as a number."""
    rows = simulate(Params.from_flat({"growth.daily_mortality": 0.01}))
    assert summary(rows)["economic_fcr"] is None


def test_economic_fcr_none_when_no_biomass_gained():
    rows = simulate(Params.from_flat({"growth.days": 100, "growth.temperature": 0,
                                      "growth.daily_mortality": 0.01}))
    assert summary(rows)["economic_fcr"] is None


def test_economic_fcr_normal_plan():
    assert summary(simulate(Params()))["economic_fcr"] == pytest.approx(1.14, abs=0.01)


def test_retention_times_must_be_positive_numbers():
    bp = BiofilterParams()
    bp.retention_times = "25-30"
    with pytest.raises(EngineInputError, match="'25-30' is not valid"):
        biofilter_table(bp)
    bp.retention_times = "25, 0"
    with pytest.raises(EngineInputError, match="'0' is not valid"):
        biofilter_table(bp)


def test_jet_rejects_flow_above_calibration_range_quickly():
    t0 = time.monotonic()
    with pytest.raises(jet.JetInputError, match="out of the calibration range"):
        jet.design(flow_m3h=1e9, head_m=3)
    assert time.monotonic() - t0 < 2  # raises at once, no huge layout loop


def test_jet_rejects_excessive_hole_count():
    with pytest.raises(jet.JetInputError, match="more than 50,000 holes"):
        jet.design(flow_m3h=10_000, head_m=0.26, nozzle_mm=8)


# ---------------------------------------------------------------- site lock / status lamps (Phase 2b)

@pytest.mark.django_db
def test_bad_constants_show_alarm_lamp_not_a_500(client):
    """A scenario whose plan cannot be calculated lights the step-02 alarm lamp instead of a 500."""
    from django.urls import reverse
    from planner.models import Scenario

    client.get("/")
    sc = Scenario.objects.get()
    sc.params = {**(sc.params or {}), "flow.tan_removal_eff": 0}
    sc.save()
    for url in ("/", reverse("brief", args=[sc.pk])):
        resp = client.get(url, follow=True)
        assert resp.status_code == 200
        assert "lamp alarm" in resp.content.decode()


# ---------------------------------------------------------------- HTTP boundary (Phase 2c)

@pytest.mark.django_db
def test_process_endpoints_reject_bad_json_bodies(client):
    """Unparsable and non-object JSON bodies are a 400 JSON answer, not a 500."""
    from django.urls import reverse
    from planner.models import Scenario

    client.get("/")
    sc = Scenario.objects.get()
    for body in ("not json", "[1, 2]"):
        resp = client.post(reverse("process_solve", args=[sc.pk]), body,
                           content_type="application/json")
        assert resp.status_code == 400
        assert resp.json() == {"ok": False, "error": "invalid JSON body"}
    resp = client.post(reverse("process_save", args=[sc.pk, 0]), "[1, 2]",
                       content_type="application/json")
    assert resp.status_code == 400
    resp = client.post(reverse("process_library"), "[1, 2]", content_type="application/json")
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


@pytest.mark.django_db
def test_process_endpoints_survive_junk_diagram_shapes(client):
    """Parseable JSON whose diagram shape is junk (non-dict node/edge entries, a node without an id,
    non-list nodes, junk targets/limits) answers {'ok': False} like any solver error — never a 500."""
    from django.urls import reverse
    from planner.models import Scenario

    client.get("/")
    sc = Scenario.objects.get()
    for body in ({"nodes": ["junk"]}, {"nodes": [{"type": "pump"}]},
                 {"nodes": [], "edges": ["junk"]}, {"nodes": 5},
                 {"nodes": [{"id": "a", "type": "sump", "data": {}}], "targets": {"media_m3": "junk"}},
                 {"limits": "junk", "nodes": []}):
        resp = client.post(reverse("process_solve", args=[sc.pk]), body,
                           content_type="application/json")
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
    # junk that is merely ignored (a non-dict targets) still solves; saves and the library
    # whitelist junk away instead of crashing
    assert client.post(reverse("process_solve", args=[sc.pk]), {"targets": "junk"},
                       content_type="application/json").status_code == 200
    assert client.post(reverse("process_save", args=[sc.pk, 0]), {"nodes": ["junk"]},
                       content_type="application/json").status_code == 200
    assert client.post(reverse("process_library"), {"name": "x", "diagram": "junk"},
                       content_type="application/json").status_code == 200


@pytest.mark.django_db
def test_equipment_post_with_bad_n_stages_is_not_a_500(client):
    from django.urls import reverse
    from planner.models import Scenario

    client.get("/")
    sc = Scenario.objects.get()
    resp = client.post(reverse("equipment", args=[sc.pk]), {"n_stages": "abc", "water": "fresh"},
                       follow=True)
    assert resp.status_code == 200


@pytest.mark.django_db
def test_jet_page_rejects_huge_flow_with_error_not_a_500(client):
    from django.urls import reverse

    resp = client.get(reverse("jet_oxygenator"), {"flow_m3h": "1e9", "head_m": "3"})
    assert resp.status_code == 200
    assert "out of the calibration range" in resp.content.decode()


@pytest.mark.django_db
def test_loan_page_survives_junk_numbers(client):
    from django.urls import reverse

    resp = client.get(reverse("loan_calculator"), {"per_year": "abc", "grace_months": "nan",
                                                   "years": "inf"})
    assert resp.status_code == 200
