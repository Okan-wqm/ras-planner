import os

import pytest
from django.urls import reverse

from planner.models import CompanyProfile, Proposal, Scenario
from ras_engine.opex import annual_opex, pump_kw


@pytest.fixture
def plan(client, db):
    client.get("/")
    sc = Scenario.objects.get()
    client.post(reverse("production", args=[sc.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "2", "horizon_unit": "years",
        "stage_name": ["Fry", "Smolt"], "stage_max": ["5", "100"], "stage_tanks": ["4", "6"], "stage_ret": ["30", "30"]})
    sc.refresh_from_db()
    return sc


def run(sc):
    from planner.views import _run_production
    return _run_production(sc)[1]


def test_pump_power_formula():
    # lifting 1 m³/h by 1 m takes 2.725 W; 360 m³/h by 3 m at 60 % efficiency = 4.905 kW
    assert pump_kw(1, 1, 100) * 1000 == pytest.approx(2.725, abs=1e-3)
    assert pump_kw(360, 3, 60) == pytest.approx(4.905)
    assert pump_kw(float("inf"), 3, 60) == 0


def lines_of(o):
    return {ln["key"]: ln for ln in o["lines"]}


def test_opex_is_one_batch_times_batches_per_year(plan):
    res = run(plan)
    o = annual_opex(res, {"prices": {"feed_price": 2, "maintenance_pct": 2}}, capex_total=500000)
    feed = sum(r["feed_kg"] for r in res["curve"]) * 365 / 30          # one batch every 30 days
    assert o["batches_per_year"] == pytest.approx(365 / 30)
    typed = annual_opex(res, {"prices": {"batches_per_year": 6}})
    assert sum(x["qty"] for x in typed["lines"] if x["group"] == "Feed") == pytest.approx(feed / (365 / 30) * 6)
    ln = lines_of(o)
    feed_lines = [x for x in o["lines"] if x["group"] == "Feed"]
    assert len(feed_lines) >= 1 and all(x["unit_price"] == 2 for x in feed_lines)
    assert sum(x["qty"] for x in feed_lines) == pytest.approx(feed)
    assert ln["maintenance"]["cost"] == pytest.approx(10000)
    assert o["total"] == pytest.approx(sum(x["cost"] for x in o["lines"]))
    # one batch every 30 days: 365/30 harvests a year, whatever falls inside the 365-day window
    per_batch = res["harvests"][0]["biomass_t"] * 1000
    assert o["harvest_kg"] == pytest.approx(per_batch * 365 / 30)
    assert o["cost_per_kg"] == pytest.approx(o["total"] / o["harvest_kg"])


def test_every_quantity_and_price_can_be_overwritten(plan):
    res = run(plan)
    base = lines_of(annual_opex(res))
    feed_name = next(x["item"] for x in base.values() if x["group"] == "Feed")
    o = annual_opex(res, {"prices": {"oxygen_price": 0.4, "electricity_price": 0.2},
                          "feed_prices": {feed_name: 3.0},
                          "qty": {"eggs": 1000, "oxygen": 50000},
                          "extras": [{"name": "Vaccination", "group": "Other", "qty": 2, "unit": "rounds", "unit_price": 1500}]})
    ln = lines_of(o)
    assert ln["eggs"]["qty"] == 1000 and ln["eggs"]["overridden"] and ln["eggs"]["qty_calc"] == base["eggs"]["qty_calc"]
    assert ln["oxygen"]["cost"] == pytest.approx(50000 * 0.4)
    assert ln[f"feed:{feed_name}"]["unit_price"] == 3.0
    assert ln["base"]["unit_price"] == 0.2
    assert ln["x:0"]["cost"] == 3000 and o["by_group"]["Other"] == 3000


def test_pumping_from_head_loss_or_from_diagram_pumps(plan):
    res = run(plan)
    est = annual_opex(res, {"units": {res["stages"][0].uid: {"head_m": 4, "eff_pct": 50}}})
    u0 = est["units"][0]
    assert u0["source"] == "head loss" and u0["kw"] == pytest.approx(pump_kw(u0["q_m3h"], 4, 50))
    drawn = annual_opex(res, {}, diagrams={0: {"pump_kw": 10.0, "pumped_m3h": 300, "makeup_m3h": 2.0}})
    days_batch = sum(1 for st in res["stage_of_age"] if st == 0)
    occupied = min(days_batch * 365 / 30, 365)        # days per batch × batches, at most the whole year
    d0 = drawn["units"][0]
    assert d0["source"] == "diagram" and d0["kwh"] == pytest.approx(10 * 24 * occupied)
    assert d0["water_m3"] == pytest.approx(2.0 * 24 * occupied) and d0["water_source"] == "diagram"
    assert drawn["units"][1]["kwh"] == pytest.approx(annual_opex(res)["units"][1]["kwh"])  # unit 2 still estimated


def test_opex_page_saves_every_field(client, plan):
    url = reverse("opex", args=[plan.pk])
    assert client.get(url).status_code == 200
    client.post(url, {"electricity_price": "0.2", "sale_price": "6", "q:eggs": "5000", "head:0": "4", "eff:1": "",
                      "fp:Starter": "3.1", "x_name": ["Vaccination", ""], "x_group": ["Other", "Other"],
                      "x_qty": ["2", "1"], "x_unit": ["rounds", ""], "x_price": ["1500", ""]})
    plan.refresh_from_db()
    o = plan.opex
    assert o["prices"] == {"electricity_price": 0.2, "sale_price": 6.0}
    assert o["qty"] == {"eggs": 5000.0} and o["units"] == {"0": {"head_m": 4.0}}
    assert o["feed_prices"] == {"Starter": 3.1}
    assert o["extras"] == [{"name": "Vaccination", "group": "Other", "qty": 2.0, "unit": "rounds", "unit_price": 1500.0}]
    page = client.get(url).content.decode()
    assert "Vaccination" in page and 'name="q:eggs" value="5000"' in page


def test_new_proposal_has_standard_texts_and_document(client, plan):
    client.post(reverse("proposals"), {"scenario": plan.pk})
    p = Proposal.objects.get()
    assert p.scenario == plan and len(p.work_packages) >= 10 and p.clauses
    assert CompanyProfile.get().name == "Suderra AS"
    # edit: customer, one design fee, VAT, drop the OPEX section
    data = {"scenario": plan.pk, "number": p.number, "title": "Smolt RAS design", "date": "2027-01-10",
            "valid_until": "2027-02-10", "currency": "EUR", "vat_pct": "25", "customer_name": "Example Fish AS",
            "terms": "Valid until {valid_until}. Supplier: {company}.", "payment": "Pay in {currency}.",
            "lump_sum": "100000", "wp_name": ["Production plan", ""], "wp_notes": ["Scenarios", ""], "wp_price": ["", ""],
            "cl_title": ["Disputes"], "cl_text": ["Oslo"], "section": ["intro", "terms", "pricing", "sizing", "plan", "capex", "payment", "clauses"]}
    r = client.post(reverse("proposal_edit", args=[p.pk]), data)
    assert r.status_code == 302
    p.refresh_from_db()
    assert p.work_packages == [{"name": "Production plan", "notes": "Scenarios", "price": None}]
    assert p.sections["opex"] is False and p.sections["sizing"] is True
    doc = client.get(reverse("proposal_doc", args=[p.pk])).content.decode()
    assert "Valid until 10 February 2027. Supplier: Suderra AS." in doc
    assert "Pay in EUR." in doc
    assert "100,000 EUR" in doc and "125,000" in doc            # fee and fee incl. 25 % VAT
    assert "Operating cost estimate" not in doc and "System sizing" in doc
    assert "Example Fish AS" in doc and "Fry" in doc and "Smolt" in doc
    assert "for the project “Smolt RAS design”" in doc          # introduction written from the scenario


@pytest.mark.e2e
def test_proposal_pdf(live_server, plan, client):
    """End to end: the PDF is made by the browser from the document page."""
    pytest.importorskip("playwright.sync_api")
    os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")
    client.post(reverse("proposals"), {"scenario": plan.pk})
    p = Proposal.objects.get()
    try:
        r = client.get(reverse("proposal_pdf", args=[p.pk]), SERVER_NAME=live_server.thread.host,
                       SERVER_PORT=str(live_server.thread.port))
    except Exception as exc:  # no browser on this machine
        pytest.skip(str(exc))
    if r.status_code == 302:
        pytest.skip("browser not available")
    assert r["Content-Type"] == "application/pdf" and r.content[:4] == b"%PDF"
    assert len(r.content) > 20000


def test_pages_open(client, plan):
    client.post(reverse("proposals"), {"scenario": plan.pk})
    p = Proposal.objects.get()
    for url in (reverse("proposals"), reverse("proposal_edit", args=[p.pk]), reverse("company"),
                reverse("opex", args=[plan.pk])):
        assert client.get(url).status_code == 200, url
    r = client.post(reverse("company"), {"name": "Suderra AS", "country": "Norway", "email": "post@example.com"})
    assert r.status_code == 302 and CompanyProfile.get().email == "post@example.com"


def test_payroll_and_shares(plan):
    res = run(plan)
    o = annual_opex(res, {"prices": {"managers": 1, "manager_salary": 70000, "technicians": 2, "technician_salary": 50000,
                                     "labour": 4, "labour_salary": 30000, "pension_pct": 10, "social_pct": 20,
                                     "medical_pct": 5, "other_payroll_pct": 5, "other_var_pct": 5,
                                     "alkalinity_per_feed": 0.2, "alkalinity_price": 0.5, "software_cost": 12000}},
                    capex_total=1_000_000)
    ln = lines_of(o)
    salaries = 70000 + 2 * 50000 + 4 * 30000
    assert o["salaries"] == salaries
    assert o["payroll"] == pytest.approx(salaries * 1.40)                   # + 10 + 20 + 5 + 5 % of salaries
    feed = sum(x["qty"] for x in o["lines"] if x["group"] == "Feed")
    assert ln["alkalinity"]["qty"] == pytest.approx(feed * 0.2) and ln["alkalinity"]["unit_price"] == 0.5
    assert ln["software"]["cost"] == 12000
    others = sum(x["cost"] for x in o["lines"] if x["key"] != "other_var")
    assert ln["other_var"]["cost"] == pytest.approx(others * 0.05)          # 5 % of all other OPEX
    assert o["total"] == pytest.approx(others * 1.05)
    assert o["variable"] + o["payroll"] == pytest.approx(o["total"])


def test_every_line_takes_a_quantity_and_a_unit_price(plan):
    """Any line's unit price can be typed for that line alone; fry are sold by the piece; the diagram's oxygen
    (solved on the busiest day) is scaled to each day of the batch."""
    res = run(plan)
    o = annual_opex(res, {"line_prices": {"base": 0.3, "insurance": 0.5}, "qty": {"insurance": 1000},
                          "prices": {"sale_price_piece": 0.2}}, capex_total=100000)
    ln = lines_of(o)
    assert ln["base"]["unit_price"] == 0.3 and ln["base"]["cost"] == pytest.approx(0.3 * ln["base"]["qty"])
    assert all(x["qty_in"]["name"] and x["price_in"]["name"] for x in o["lines"])
    assert ln["insurance"]["qty"] == 1000
    assert o["revenue"] == pytest.approx(o["harvest_pcs"] * 0.2) and o["harvest_pcs"] > 0
    peak = res["designs"][0]["peak"]
    drawn = annual_opex(res, {}, diagrams={0: {"pure_o2_kgd": peak["o2_kgd"] * 2, "ozone_kgd": 0, "sludge_tss_kgd": 0}})
    days = [res["curve"][a] for a, st in enumerate(res["stage_of_age"]) if st == 0]
    assert lines_of(drawn)["oxygen"]["qty_calc"] >= sum(r["o2_kgd"] for r in days) * 2 * 365 / 30 - 1e-6
