"""Phase 7 UX chains.

7.1 proposal integrity: the error banner never reaches the PDF, drafts say so on the first page,
a broken plan or RASPLAN_STRICT_PDF stops the PDF, and every generated PDF archives its figures so
the proposal survives its scenario.

7.2 form re-render safety and delete safety: a rejected save keeps what was typed, deletes write a
JSON backup first, and the last scenario no longer silently resurrects on the home page.
"""
import json
from datetime import datetime

import pytest
from django.urls import reverse

from planner.models import Proposal, Scenario
from planner.views_docs import _snapshot_from_ctx, proposal_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def plan(client):
    """The default scenario with a saved two-unit production plan."""
    client.get("/")
    sc = Scenario.objects.get()
    client.post(reverse("production", args=[sc.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "2", "horizon_unit": "years",
        "stage_name": ["Fry", "Smolt"], "stage_max": ["5", "100"], "stage_tanks": ["4", "6"], "stage_ret": ["30", "30"]})
    sc.refresh_from_db()
    return sc


@pytest.fixture
def proposal(client, plan):
    """A fresh proposal: preflight incomplete (no customer, no fee, no company details)."""
    client.post(reverse("proposals"), {"scenario": plan.pk})
    return Proposal.objects.get()


def _break_plan(sc):
    """An unreachable harvest weight: the fish stop growing long before it, so the plan cannot be
    calculated (the plan reaches only ~1 t per fish)."""
    prod = sc.plan_settings()
    prod["stages"][-1]["max_weight_g"] = 1e7
    sc.production = prod
    sc.save()


# --- 7.1 error banner, draft band, PDF gates --------------------------------------------------

def test_error_banner_shows_in_preview_but_never_in_pdf(client, plan, proposal):
    _break_plan(plan)
    url = reverse("proposal_doc", args=[proposal.pk])
    preview = client.get(url).content.decode()
    assert "could not be calculated" in preview                    # the editor sees what is wrong
    as_pdf = client.get(url + "?pdf=1").content.decode()
    assert "could not be calculated" not in as_pdf                 # the customer never does


def test_draft_band_only_in_pdf_mode(client, proposal):
    url = reverse("proposal_doc", args=[proposal.pk])
    as_pdf = client.get(url + "?pdf=1&draft=1").content.decode()   # what the PDF maker renders
    assert "DRAFT — preflight incomplete" in as_pdf
    assert client.get(url).content.decode().count("DRAFT") == 0    # the preview is not stamped
    assert client.get(url + "?draft=1").content.decode().count("DRAFT") == 0


def test_pdf_refuses_a_broken_plan(client, plan, proposal):
    _break_plan(plan)
    r = client.get(reverse("proposal_pdf", args=[proposal.pk]))
    assert r.status_code == 302 and r.url == reverse("proposal_edit", args=[proposal.pk])


def test_strict_pdf_env_blocks_draft_documents(client, proposal, monkeypatch):
    monkeypatch.setenv("RASPLAN_STRICT_PDF", "1")
    r = client.get(reverse("proposal_pdf", args=[proposal.pk]))
    assert r.status_code == 302 and r.url == reverse("proposal_edit", args=[proposal.pk])


# --- 7.1 the snapshot of the generated PDF -----------------------------------------------------

def test_snapshot_from_ctx_holds_the_documents_figures(client, plan, proposal):
    ctx = proposal_context(proposal, None)
    snap = _snapshot_from_ctx(ctx)
    assert snap["number"] == proposal.number
    assert snap["scenario_pk"] == plan.pk and snap["scenario_name"] == plan.name
    datetime.fromisoformat(snap["generated_at"])                   # ISO and parseable
    assert snap["annual_harvest_t"] == ctx["res"]["annual_harvest_t"]
    assert snap["steady_state"] == ctx["res"]["steady_state"]
    assert snap["opex_total"] == ctx["o"]["total"]
    assert {s["name"] for s in snap["sizing"]} == {"Fry", "Smolt"}  # the sizing table, raw
    assert set(snap["capex"]) >= {"grand_total", "equipment_total", "extras_total", "currency"}
    assert snap["harvests_first12"] and all(set(h) == {"date", "biomass_t"} for h in snap["harvests_first12"])


def test_deleted_scenario_still_renders_from_the_archived_snapshot(client, plan, proposal):
    ctx = proposal_context(proposal, None)
    proposal.snapshot = _snapshot_from_ctx(ctx)
    proposal.save()
    plan.delete()
    proposal.refresh_from_db()
    assert proposal.scenario is None
    doc = client.get(reverse("proposal_doc", args=[proposal.pk])).content.decode()
    assert "Fry" in doc and "Smolt" in doc                         # the sizing table still renders
    assert "archived snapshot" in doc
    assert f"{ctx['o']['total']:,.0f}" in doc                      # with the OPEX total it showed


def test_form_warns_when_the_scenario_changed_after_the_pdf(client, plan, proposal):
    url = reverse("proposal_edit", args=[proposal.pk])
    ctx = proposal_context(proposal, None)
    snap = _snapshot_from_ctx(ctx)
    snap["generated_at"] = "2020-01-01T00:00:00+00:00"             # a PDF made long ago
    proposal.snapshot = snap
    proposal.save()
    assert "regenerate to refresh the figures" in client.get(url).content.decode()
    proposal.snapshot = _snapshot_from_ctx(proposal_context(proposal, None))   # a fresh one
    proposal.save()
    assert "regenerate" not in client.get(url).content.decode()


# --- 7.2 a rejected save keeps what was typed --------------------------------------------------

def test_invalid_brief_post_rerenders_with_the_typed_values(client, plan):
    url = reverse("brief", args=[plan.pk])
    before = plan.brief
    r = client.post(url, {"name": "Client A", "batches_per_year": "99", "pieces_per_batch": "500000",
                          "target_weight_g": "350", "start_date": "2027-02-01", "temperature": "12",
                          "salinity": "0", "customer": "Nordlak", "water_note": "spring water"})
    assert r.status_code == 200                                  # re-rendered, not redirected
    html = r.content.decode()
    assert "between 1 and 52" in html and 'name="batches_per_year" value="99"' in html
    assert 'value="Client A"' in html and "spring water" in html
    plan.refresh_from_db()
    assert plan.brief == before                                  # nothing was saved
    # the same for the target weight the units cannot reach
    r = client.post(url, {"name": "Client A", "batches_per_year": "6", "pieces_per_batch": "500000",
                          "target_weight_g": "4", "start_date": "2027-02-01"})
    assert r.status_code == 200 and "above the previous RAS unit" in r.content.decode()
    plan.refresh_from_db()
    assert plan.brief == before and plan.name != "Client A"


def test_invalid_units_post_rerenders_with_the_typed_rows(client, plan):
    url = reverse("units", args=[plan.pk])
    before = plan.production
    uid = plan.unit_id(0)
    r = client.post(url, {"stage_id": [uid, ""], "stage_name": ["Fry", "Nursery"],
                          "stage_max": ["5", "abc"], "stage_tanks": ["4", "2"], "stage_ret": ["30", "30"],
                          "tank_rest_days": "3",
                          f"t-{uid}-item": "custom", f"t-{uid}-cap": "12",
                          f"t-{uid}-upto": ["1", ""], f"t-{uid}-k": ["2", "9"]})
    assert r.status_code == 200
    html = r.content.decode()
    assert "must be numbers" in html
    assert 'value="Nursery"' in html and 'value="abc"' in html     # the typed rows are still there
    assert 'name="tank_rest_days" value="3"' in html
    assert '<option value="custom" selected' in html and 'value="9"' in html   # and the tank picks
    plan.refresh_from_db()
    assert plan.production == before                              # nothing was saved


def test_flow_table_post_with_a_broken_plan_keeps_the_typed_table(client, plan):
    _break_plan(plan)
    url = reverse("requirements", args=[plan.pk])
    r = client.post(url, {"form": "flows", "u0-pure": "yes", "u0-o2_in": "18", "u0-o2_out": "8",
                          "u0-tan_max": "1", "u0-tan_in": "0.4", "u0-co2_max": "15", "u0-co2_in": "5",
                          "u0-tss_max": "10", "u0-tss_in": "3"})
    assert r.status_code == 200
    html = r.content.decode()
    assert "Fix the production plan first" in html
    assert 'name="u0-o2_in" value="18"' in html and 'name="u0-tan_in" value="0.4"' in html
    assert '<option value="yes" selected' in html          # the pure-oxygen choice kept


# --- 7.2 delete safety -------------------------------------------------------------------------

def test_scenario_delete_writes_a_backup_and_cleans_the_pngs(client, plan, tmp_path, monkeypatch):
    from planner import views as planner_views, views_docs
    monkeypatch.setattr(planner_views, "_snapshot_dir", lambda: tmp_path / "backups")
    shots = tmp_path / "shots"
    shots.mkdir()
    (shots / f"{plan.pk}-0-deadbeef.png").write_bytes(b"png")
    other = Scenario.objects.create(name="other")
    (shots / f"{other.pk}-0-feed.png").write_bytes(b"png")
    monkeypatch.setattr(views_docs, "SNAPSHOT_DIR", shots)

    r = client.post(reverse("scenario_delete", args=[plan.pk]))
    assert r.status_code == 302 and r.url == reverse("dashboard_home")
    [backup] = (tmp_path / "backups").glob(f"scenario_{plan.pk}_deleted_*.json")
    payload = json.loads(backup.read_text(encoding="utf-8"))
    assert payload["name"] == plan.name and payload["schema_version"] == plan.schema_version
    assert {"params", "biofilter", "production", "equipment", "capex", "process", "opex",
            "brief"} <= set(payload)
    assert not (shots / f"{plan.pk}-0-deadbeef.png").exists()      # its drawings went with it
    assert (shots / f"{other.pk}-0-feed.png").exists()             # another scenario's stayed


def test_last_scenario_delete_redirects_to_new_instead_of_auto_creating(client, plan, tmp_path, monkeypatch):
    from planner import views as planner_views
    monkeypatch.setattr(planner_views, "_snapshot_dir", lambda: tmp_path / "backups")
    r = client.post(reverse("scenario_delete", args=[plan.pk]))
    assert r.status_code == 302 and r.url == reverse("scenario_new")
    assert Scenario.objects.count() == 0
    assert client.get(reverse("scenario_new")).status_code == 200
    assert Scenario.objects.count() == 0                            # the form page makes nothing


def test_proposal_delete_writes_a_backup(client, proposal, tmp_path, monkeypatch):
    from planner import views as planner_views
    monkeypatch.setattr(planner_views, "_snapshot_dir", lambda: tmp_path / "backups")
    r = client.post(reverse("proposal_delete", args=[proposal.pk]))
    assert r.status_code == 302 and r.url == reverse("proposals")
    [backup] = (tmp_path / "backups").glob(f"proposal_{proposal.pk}_deleted_*.json")
    payload = json.loads(backup.read_text(encoding="utf-8"))
    assert payload["number"] == proposal.number and payload["work_packages"]
    assert payload["scenario_pk"] == proposal.scenario.pk
    assert Proposal.objects.count() == 0


def test_feeds_and_standards_delete_ask_for_confirmation(client, db):
    from planner.models import EquipmentStandard, Feed
    Feed.objects.create(name="Starter", min_weight_g=0, max_weight_g=5, protein_pct=58, fcr=0.8)
    EquipmentStandard.objects.create(category="tank", name="Round tank 6 m", capacity=50)
    assert "confirm('Delete this feed?')" in client.get(reverse("feeds")).content.decode()
    assert "confirm('Delete this standard?')" in client.get(reverse("standards")).content.decode()


def test_first_visit_still_makes_a_scenario(client, db):
    r = client.get("/")
    assert r.status_code == 302
    assert Scenario.objects.count() == 1
    assert r.url == reverse("brief", args=[Scenario.objects.get().pk])
