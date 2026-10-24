import pytest
from django.urls import reverse

from planner.models import Scenario
from ras_engine.opex import DEFAULT_PRICES, annual_opex, ramped_cashflow


@pytest.fixture
def plan(client, db):
    client.get("/")
    sc = Scenario.objects.get()
    client.post(reverse("production", args=[sc.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "2", "horizon_unit": "years",
        "stage_name": ["Fry", "Smolt"], "stage_max": ["5", "100"], "stage_tanks": ["4", "6"],
        "stage_ret": ["30", "30"]})
    sc.refresh_from_db()
    return sc


def run(sc):
    from planner.views import _run_production
    return _run_production(sc)[1]


def lines_of(o):
    return {ln["key"]: ln for ln in o["lines"]}


# ---------- 8.1: make-up water is a priced line, free until a price is entered ----------

def test_water_line_exists_free_by_default(plan):
    res = run(plan)
    o = annual_opex(res)
    ln = lines_of(o)["water"]
    assert ln["qty"] > 0 and ln["unit"] == "m³" and ln["cost"] == 0.0
    assert ln["unit_price"] == DEFAULT_PRICES["water_price"] == 0.0
    # free by default: the total is exactly what it was without the line
    assert o["total"] == pytest.approx(sum(x["cost"] for x in o["lines"] if x["key"] != "water"))
    assert "NO₃ exchange" in ln["note"]


def test_water_line_priced(plan):
    res = run(plan)
    base = annual_opex(res)
    o = annual_opex(res, {"prices": {"water_price": 0.5}})
    ln = lines_of(o)["water"]
    assert ln["unit_price"] == 0.5 and ln["qty"] == pytest.approx(lines_of(base)["water"]["qty"])
    assert ln["cost"] == pytest.approx(ln["qty"] * 0.5)
    # the total grows by the water cost plus its 5 % share of "other variable costs"
    assert o["total"] == pytest.approx(base["total"] + ln["cost"] * 1.05)


def test_water_line_diagram_quantity_wins(plan):
    """A unit with a drawn diagram prices the water it shows, not the plan's NO₃ exchange."""
    res = run(plan)
    occupied = min(sum(1 for st in res["stage_of_age"] if st == 0) * 365 / 30, 365)
    base = annual_opex(res)
    drawn = annual_opex(res, {}, diagrams={0: {"pump_kw": 1.0, "makeup_m3h": 2.0}})
    ln = lines_of(drawn)["water"]
    assert "diagram" in ln["note"]
    assert ln["qty"] == pytest.approx(2.0 * 24 * occupied + base["units"][1]["water_m3"])


def test_negative_water_need_is_shown_as_zero(plan):
    res = run(plan)
    o = annual_opex(res, {}, diagrams={k: {"pump_kw": 1.0, "makeup_m3h": -5.0} for k in (0, 1)})
    ln = lines_of(o)["water"]
    assert ln["qty_calc"] == 0.0 and ln["qty"] == 0.0 and ln["cost"] == 0.0
    assert "shown as 0" in ln["note"]


# ---------- 8.2: first-years ramp, NPV and payback ----------

@pytest.fixture
def slow_plan(client, db):
    """Fish take longer than a year to grow out, so year 1 has no harvest at all."""
    client.get("/")
    sc = Scenario.objects.get()
    client.post(reverse("production", args=[sc.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "2", "horizon_unit": "years",
        "stage_name": ["Fry", "Grow-out"], "stage_max": ["5", "3000"], "stage_tanks": ["4", "6"],
        "stage_ret": ["30", "30"]})
    sc.refresh_from_db()
    return sc


def test_ramped_first_year_has_no_harvest_before_the_first_one(slow_plan):
    res = run(slow_plan)
    assert res["harvests"] and res["harvests"][0]["day"] >= 365   # the premise of this test
    o = annual_opex(res, {"prices": {"sale_price": 5}}, capex_total=1_000_000)
    r = ramped_cashflow(res, o["total"], 1_000_000, 5.0)
    y1 = r["years"][0]
    assert y1["harvest_t"] == 0 and y1["revenue"] == 0 and y1["net"] == -o["total"]
    # while the steady-state table one screen up already assumes the full harvest
    assert o["harvest_kg"] > 0 and o["revenue"] > 0
    assert any(y["harvest_t"] > 0 for y in r["years"])


def test_ramped_npv_and_payback_hand_check():
    """Three years of 10 t at 1/kg against 1000 of OPEX, 5000 of CAPEX, 10 % discount — by hand."""
    res = {"interval_days": 365, "horizon": 1095, "batches": [1, 2, 3],
           "harvests": [{"day": 100, "biomass_t": 10.0}, {"day": 465, "biomass_t": 10.0},
                        {"day": 830, "biomass_t": 10.0}]}
    r = ramped_cashflow(res, opex_total=1000.0, capex_total=5000.0, price_per_kg=1.0, discount_pct=10.0)
    # each year: 10 000 revenue - 1 000 costs = 9 000 net; cumulative 4 000 / 13 000 / 22 000
    assert [y["net"] for y in r["years"]] == [9000.0, 9000.0, 9000.0]
    assert [y["cumulative"] for y in r["years"]] == [4000.0, 13000.0, 22000.0]
    assert r["npv"] == pytest.approx(-5000 + 9000 / 1.1 + 9000 / 1.1 ** 2 + 9000 / 1.1 ** 3)
    assert r["payback_ramped_years"] == pytest.approx(5000 / 9000)   # crossed inside year 1
    # steady state after the plan: one batch a year, so 10 000 revenue a year
    assert r["steady_state_revenue"] == pytest.approx(10000.0)


def test_payback_with_ramp_is_slower_than_the_naive_one(plan):
    res = run(plan)
    o = annual_opex(res, {"prices": {"sale_price": 8}}, capex_total=2_000_000)
    assert o["payback_years"] is not None
    r = ramped_cashflow(res, o["total"], 2_000_000, 8.0)
    assert r["payback_ramped_years"] is not None
    assert r["payback_ramped_years"] > o["payback_years"]


def test_ramped_payback_none_when_never_paid_back(plan):
    res = run(plan)
    o = annual_opex(res)          # no sale price: no revenue, never paid back
    r = ramped_cashflow(res, o["total"], 2_000_000, 0.0)
    assert r["payback_ramped_years"] is None and r["npv"] < 0


def test_opex_page_shows_ramp_card_and_labels(client, plan):
    url = reverse("opex", args=[plan.pk])
    page = client.get(url).content.decode()
    assert "First years (ramp)" in page
    assert "Cost per year (at full capacity)" in page                # steady-state table labelled
    assert "Payback at full capacity" in page                        # both payback figures, side by side
    assert "payback with the ramp" in page
    assert "not re-sized per option" in page                         # note at the batches comparison
    assert 'name="discount_pct"' in page and 'placeholder="8"' in page
    client.post(url, {"discount_pct": "12", "sale_price": "5"})
    plan.refresh_from_db()
    assert plan.opex["prices"]["discount_pct"] == 12.0 and plan.opex["prices"]["sale_price"] == 5.0
    page = client.get(url).content.decode()
    assert 'value="12"' in page and "12.0 %" in page          # the typed discount rate round-trips


# ---------- 8.3: stoichiometric bicarbonate, payroll benchmark band ----------

def test_alkalinity_default_follows_stoichiometry(plan):
    res = run(plan)
    o = annual_opex(res)
    ln = lines_of(o)["alkalinity"]
    feed = sum(x["qty"] for x in o["lines"] if x["group"] == "Feed")
    # 46 g TAN/kg feed × 7.14 g CaCO₃/g N × 84/50 = 0.5518 → 0.552 kg NaHCO₃/kg feed
    assert ln["qty_calc"] == pytest.approx(feed * 0.552)
    assert round(0.046 * 7.14 * 84 / 50, 3) == DEFAULT_PRICES["alkalinity_per_feed"]


def test_alkalinity_override_keeps_the_old_behaviour(plan):
    res = run(plan)
    o = annual_opex(res, {"prices": {"alkalinity_per_feed": 0.25}})   # the pre-8.3 default, typed
    ln = lines_of(o)["alkalinity"]
    feed = sum(x["qty"] for x in o["lines"] if x["group"] == "Feed")
    assert ln["qty"] == pytest.approx(feed * 0.25)


def test_payroll_band_note_on_the_page(client, plan):
    page = client.get(reverse("opex", args=[plan.pk])).content.decode()
    assert "typically 10–20% of OPEX" in page and "this plan:" in page
    assert "placeholders pending a real staffing plan" in page
    assert "%" in page  # the share itself is rendered next to the band


# ---------- 8.4: TGC warning on the constants page (default unchanged) ----------

def constants_data(sc, **growth):
    from ras_engine import Params
    data = {k: str(v) for k, v in Params().to_flat().items()}
    data.update({"name": sc.name, "temperature_steps": "", "use_feed_catalog": "on"})
    data.update({f"growth.{k}": str(v) for k, v in growth.items()})
    return data


def test_tgc_default_stays_2_5():
    from ras_engine.params import Params
    g = Params().growth
    assert g.tgc == 2.5 and g.use_tgc is False


def test_tgc_warning_shown_only_when_fast_tgc_is_on(client, plan):
    url = reverse("scenario_edit", args=[plan.pk])
    warn = "reaches harvest ~30% faster"
    assert client.post(url, constants_data(plan, use_tgc="True", tgc="2.5")).status_code == 302
    assert warn in client.get(url).content.decode()
    assert client.post(url, constants_data(plan, use_tgc="True", tgc="1.8")).status_code == 302
    assert warn not in client.get(url).content.decode()
    assert client.post(url, constants_data(plan, use_tgc="False", tgc="2.5")).status_code == 302
    assert warn not in client.get(url).content.decode()   # the Excel length model: no claim made


# ---------- 8.5: biofilter media-rate scaling capped at 1000 m²/m³ ----------

def bio_data(media_area):
    from dataclasses import fields as dfields
    from ras_engine.params import BiofilterParams
    bp = BiofilterParams()
    return {f.name: str(getattr(bp, f.name)) for f in dfields(BiofilterParams)
            if f.name != "media_area_m2m3"} | {"media_area_m2m3": str(media_area)}


def test_media_area_above_1000_sizes_like_1000():
    from dataclasses import replace
    from ras_engine.params import BiofilterParams
    from ras_engine.biofilter import biofilter_table
    sizing = ("media_salt_m3", "media_fresh_m3", "volume_salt_m3", "volume_fresh_m3",
              "residence_salt_min", "residence_fresh_min", "rate_salt", "rate_fresh")
    at_1000 = biofilter_table(BiofilterParams(media_area_m2m3=1000.0))
    at_1500 = biofilter_table(replace(BiofilterParams(), media_area_m2m3=1500.0))
    for r1000, r1500 in zip(at_1000, at_1500):
        assert all(r1000[k] == r1500[k] for k in sizing)
        assert r1500["media_area_capped"] is True and r1000["media_area_capped"] is False


def test_media_area_600_matches_the_formula_directly():
    import math
    from ras_engine.params import BiofilterParams
    from ras_engine.biofilter import MAX_MEDIA_AREA_M2M3, biofilter_row
    bp = BiofilterParams(media_area_m2m3=600.0)
    assert MAX_MEDIA_AREA_M2M3 == 1000.0
    for t in (25.0, 30.0, 40.0):
        r = biofilter_row(bp, t)
        monod = bp.monod_max * r["tan_out"] / (r["tan_out"] + bp.monod_ks)
        k_media = min(bp.media_area_m2m3, MAX_MEDIA_AREA_M2M3) / bp.reference_area
        assert r["rate_salt"] == pytest.approx(monod * math.exp(bp.temp_coefficient * (bp.temperature - 20))
                                               * k_media)
        assert r["rate_fresh"] == pytest.approx(r["rate_salt"] * bp.freshwater_factor)


def test_biofilter_page_notes_the_cap_and_feeding_hours(client, plan):
    url = reverse("biofilter", args=[plan.pk])
    page = client.get(url).content.decode()
    assert "Excel semantics: TAN load scales ×24/feeding_hours" in page and "keep 24" in page
    assert "capped at 1000" not in page                      # default 1000 sits on the cap, not above it
    assert client.post(url, bio_data(1500)).status_code == 302
    page = client.get(url).content.decode()
    assert "capped at 1000" in page                          # the note appears once the area is above it
