"""Performance routing: growth_curve's early exit must not change any number, and every plan
consumer goes through the one cached path (planner.services.plan_result)."""
import json
from copy import deepcopy

import pytest
from django.urls import reverse

from planner import services
from planner.models import Scenario
from ras_engine import Params, simulate
from ras_engine.production import growth_curve

HARVEST_G = 3000.0  # default plan's last stage (Large trout)


def test_growth_curve_early_exit_matches_full_simulation():
    """The early exit returns exactly the rows the old trim-to-harvest-weight produced."""
    curve = growth_curve(Params(), HARVEST_G)  # simulate() now stops at the harvest weight
    p = Params()
    p.growth.days = 900  # enough to pass the harvest weight, like max_days but cheaper to compare
    full = simulate(p)  # no until_weight_g: every day simulated, as before
    trimmed = []
    for r in full:
        trimmed.append(r)
        if r["weight_g"] >= HARVEST_G:
            break
    assert len(curve) == len(trimmed) == 702
    assert curve == trimmed                      # every row, every field, bit for bit
    assert curve[-1]["weight_g"] >= HARVEST_G    # the harvest day is included
    assert curve[-1]["feed_cum_t"] == trimmed[-1]["feed_cum_t"]  # totals unchanged


def test_simulate_until_weight_default_none_unchanged():
    """Omitting until_weight_g and passing None explicitly are the same thing (today's behavior)."""
    p = Params()
    p.growth.days = 30
    assert simulate(p) == simulate(deepcopy(p), until_weight_g=None)


# ---------- one cached path for every plan consumer ----------

@pytest.fixture
def counting_engine(monkeypatch):
    """Count how many times the engine really runs; the cache must serve every further call."""
    calls = []
    real = services.run_production

    def run(scenario):
        calls.append(1)
        return real(scenario)

    monkeypatch.setattr(services, "run_production", run)
    services._plan_cache.clear()
    return calls


@pytest.mark.django_db
def test_plan_result_error_variant(counting_engine):
    """A broken scenario yields (None, message) — and the failure is cached, not retried."""
    sc = Scenario.objects.create(name="bad tan")
    sc.params = {**Params().to_flat(), "flow.tan_removal_eff": 0}
    sc.save()
    got, err = services.plan_result(sc)
    assert got is None and "EngineInputError" in err
    assert services.plan_cached(sc) is None  # the lamps see None, as before
    assert services.plan_result(sc) == (None, err)  # second call served from the cache
    assert len(counting_engine) == 1


@pytest.mark.django_db
def test_plan_cached_hit(counting_engine):
    """A valid scenario is computed once; the cache returns the very same object afterwards."""
    sc = Scenario.objects.create(name="ok")
    first = services.plan_cached(sc)
    assert first is not None and len(counting_engine) == 1
    assert services.plan_cached(sc) is first
    assert len(counting_engine) == 1


@pytest.mark.django_db
def test_process_save_narrow_write(client):
    """Saving a diagram writes only the process blob: no updated bump, page still shows it."""
    client.get("/")
    sc = Scenario.objects.get()
    editor = client.get(reverse("process", args=[sc.pk])).context["editor"]
    diagram = editor["diagram"]
    diagram["nodes"].append({"id": "narrowtest1", "type": "pump", "data": {"name": "Extra"},
                             "position": {"x": 10, "y": 10}})
    before = sc.updated
    resp = client.post(editor["urls"]["save"], json.dumps(diagram), content_type="application/json").json()
    assert resp["saved"]
    sc.refresh_from_db()
    assert any(n["id"] == "narrowtest1" for n in sc.diagram_of(0)["nodes"])  # the blob changed
    assert sc.updated == before  # narrow save: the plan cache key (and ordering) intentionally unchanged
    html = client.get(reverse("process", args=[sc.pk])).content
    assert b"narrowtest1" in html  # the page reflects the saved diagram (solve cache is content-keyed)


@pytest.mark.django_db
def test_prewarm_after_constants_save(client, counting_engine):
    """Saving the constants warms the plan cache: the redirected page does not recompute."""
    client.get("/")
    sc = Scenario.objects.get()
    data = {k: v for k, v in Params().to_flat().items()}
    data.update({"name": "Test", "temperature_steps": "1:12, 100:14", "use_feed_catalog": "on",
                 "growth.days": 200})
    assert client.post(reverse("scenario_edit", args=[sc.pk]), data).status_code == 302
    assert len(counting_engine) == 1  # prewarm ran the plan exactly once
    sc.refresh_from_db()
    assert services.plan_cached(sc) is not None
    assert len(counting_engine) == 1  # served warm
