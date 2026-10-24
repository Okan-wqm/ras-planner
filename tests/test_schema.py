"""Scenario schema versioning: v1 rows keep their blobs untouched (decision DG-1), get flagged with
review notes on load, and the read-only validate_scenarios command reports per-scenario data health."""
import logging
from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse

from planner import schema
from planner.models import Scenario

pytestmark = pytest.mark.django_db

# one valid plan shared by the command tests: a single 5 g RAS unit
STAGES = [{"id": "aaa", "name": "Fry", "max_weight_g": 5, "n_tanks": 2, "retention_min": 30}]


def test_v1_row_upgrades_in_place_without_touching_blobs(caplog):
    sc = Scenario.objects.create(
        name="legacy", schema_version=1,
        params={"gas.o2_per_feed": 1.0, "growth.days": 300},
        capex={"currency": "EUR", "price_overrides": {"p0-x": 1200.0, "p0-y": 800.0}})
    params_before, capex_before = dict(sc.params), dict(sc.capex)
    with caplog.at_level(logging.WARNING, logger="planner"):
        notes = schema.upgrade(sc)
    sc.refresh_from_db()
    assert sc.schema_version == schema.CURRENT == 2
    assert sc.params == params_before and sc.capex == capex_before  # DG-1: blobs never rewritten
    assert any("v1 semantics" in n and "1.0" in n for n in notes)   # the old combined O₂ value
    assert any("2 price overrides" in n for n in notes)             # the capex count
    assert any("v1 semantics" in r.message for r in caplog.records)  # the app logger warns too


def test_post_split_value_and_dry_run_change_nothing():
    sc = Scenario.objects.create(name="ok", schema_version=1, params={"gas.o2_per_feed": 0.35})
    assert schema.upgrade(sc, save=False) == []  # 0.35 is the post-split default; nothing to review
    sc.refresh_from_db()
    assert sc.schema_version == 1  # dry run: the row was not stamped


def test_page_load_upgrades_a_v1_row(client):
    client.get("/")  # _current creates the first scenario
    sc = Scenario.objects.get()
    Scenario.objects.filter(pk=sc.pk).update(schema_version=1)  # as if written by v1 code
    client.get(reverse("dashboard", args=[sc.pk]))
    sc.refresh_from_db()
    assert sc.schema_version == schema.CURRENT


def test_new_scenarios_start_at_the_current_version(client):
    client.get("/")  # _current creates the first scenario
    assert Scenario.objects.get().schema_version == schema.CURRENT


def test_migration_installed_the_field():
    # pytest-django already ran the migrations (0016) to build the test database
    field = Scenario._meta.get_field("schema_version")
    assert field.default == schema.CURRENT  # new rows use the model default, not the historical 1


def test_validate_scenarios_reports_findings_without_touching_rows():
    messy = Scenario.objects.create(
        name="messy", params={"bogus.key": 1},
        production={"start_date": "2027-01-01", "interval_days": 30, "horizon_days": 730,
                    "stages": STAGES},
        process={"gone": {"nodes": [], "edges": []}},
        equipment={"stages": {"gone": {}, "aaa": {"pump": {"item": "999", "capacity": 5.0, "qty": 1}}}})
    legacy = Scenario.objects.create(name="legacy", schema_version=1,
                                     params={"gas.o2_per_feed": 1.0},
                                     production={"stages": STAGES})
    before = {sc.pk: {f: dict(getattr(sc, f)) for f in ("params", "process", "equipment", "capex")}
              for sc in (messy, legacy)}
    out = StringIO()
    call_command("validate_scenarios", stdout=out)
    text = out.getvalue()
    assert "bogus.key" in text   # unknown params key
    assert "gone" in text        # orphan process/equipment unit id
    assert "#999" in text        # dangling equipment standard
    assert "v1 semantics" in text  # dry-run notes of the pending upgrade
    for sc in (messy, legacy):
        sc.refresh_from_db()
        for f, value in before[sc.pk].items():
            assert getattr(sc, f) == value  # read-only: the rows are byte-identical
    assert messy.schema_version == schema.CURRENT and legacy.schema_version == 1  # nothing stamped
