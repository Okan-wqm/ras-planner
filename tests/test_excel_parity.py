"""Compare ras_engine results with the values trout.xlsx computed."""
import json
import math
from pathlib import Path

import pytest

from ras_engine import BiofilterParams, Params, biofilter_table, simulate

FIX = Path(__file__).parent / "fixtures"
REL = 1e-9

# Deliberate/manual Excel deviations: (day, column)
# Day 214: Excel pins density to the previous day for one row (N222 = N221).
KNOWN_DFRY = {(214, "density"), (214, "volume_m3")}
EXCEL_MAX_DENSITY = 72.00477470241182


def excel_params():
    """The assumptions of trout.xlsx (the app's defaults have moved on from them)."""
    params = Params()
    g, gas, fl = params.growth, params.gas, params.flow
    g.use_tgc, g.condition_factor = False, 1.7
    gas.o2_per_feed, gas.o2_per_tan, gas.co2_per_tan, gas.salinity = 0.5, 0.0, 0.0, 3.0
    fl.tss_per_feed, fl.no3_inlet = 0.35, 20.0
    return params


@pytest.fixture(scope="module")
def excel_days():
    return json.loads((FIX / "dfry.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def engine_days(excel_days):
    overrides = {
        "temperature": [r["temperature"] for r in excel_days],
        "protein": [r["protein"] for r in excel_days],
        "fcr": [r["fcr"] for r in excel_days],
        "inlet_o2_saturation": [1 + r["cone_factor"] for r in excel_days],  # Excel: cone gauge + 1 bar
    }
    params = excel_params()
    params.growth.days = len(excel_days)
    # Excel freezes the cap at the value of the day it passed 72 (L/0.32 = 72.0048)
    params.growth.max_density = EXCEL_MAX_DENSITY
    # Excel raises the ozone dose to 30 after day 55 (5.59 g → 5.82 g); the note says 5 g.
    params.gas.ozone_weight_threshold = 5.7
    return simulate(params, overrides=overrides)


def test_day_count(excel_days, engine_days):
    assert len(engine_days) == len(excel_days) == 728


@pytest.mark.parametrize("col", [
    "length_inc", "length_cm", "weight_g", "count", "biomass_kg", "biomass_t", "density",
    "volume_m3", "sgr", "feed_kg", "feed_rate", "feed_cum_t",
    "o2_lpm", "o2_lpm_actual", "o2_lph", "o2_kgh", "o2_kgd",
    "co2_lpm", "co2_lph", "co2_kgh", "co2_kgd", "ozone_kgd", "ozone_gh", "ozone_lpm",
    "o2_sat_in", "o2_sat_out", "q_o2_m3h", "q_o2_lpm", "q_tan_m3h", "q_tan_lpm",
    "q_co2_m3h", "q_co2_lpm", "q_tss_m3h", "q_tss_lpm", "q_no3_m3d", "water_per_feed",
    "backflush60_m3d", "backflush30_m3d",
])
def test_daily_column(col, excel_days, engine_days):
    mismatches = []
    total_rows = len(excel_days)
    compared = skipped_none = skipped_known = 0
    for ex, en in zip(excel_days, engine_days):
        expected = ex[col]
        if expected is None:
            skipped_none += 1
            continue
        if (en["day"], col) in KNOWN_DFRY:
            skipped_known += 1
            continue
        compared += 1
        if not math.isclose(en[col], expected, rel_tol=REL, abs_tol=1e-12):
            mismatches.append((en["day"], expected, en[col]))
    assert not mismatches, f"{col}: {len(mismatches)} fark, ilk: {mismatches[:3]}"
    # coverage guard (additive): a column whose fixtures turn mostly None would pass the loop above
    # vacuously — that silent coverage loss must fail instead. Today every column has at most one
    # None (day 0: length_inc, sgr, feed_cum_t, backflush*), well inside the 5 % allowance.
    allowance = max(1, int(0.05 * total_rows))
    assert skipped_none <= allowance, f"{col}: {skipped_none}/{total_rows} fixture rows are None"
    assert compared >= total_rows - skipped_known - allowance, \
        f"{col}: only {compared}/{total_rows} rows actually compared"


def test_density_cap_reached(engine_days):
    assert max(r["density"] for r in engine_days) == EXCEL_MAX_DENSITY
    assert engine_days[-1]["density"] == EXCEL_MAX_DENSITY


def test_density_cap_default():
    rows = simulate(Params())
    assert max(r["density"] for r in rows) == 72.0
    capped = [r["day"] for r in rows if r["density"] == 72.0]
    assert capped == list(range(capped[0], len(rows) + 1))  # stays at 72 once reached


def test_ozone_dose_by_weight():
    rows = simulate(Params())
    for r in rows:
        assert r["ozone_dose"] == (15.0 if r["weight_g"] < 5.0 else 30.0)


def test_feed_catalog_selection():
    from ras_engine import Feed
    feeds = [Feed("Starter", 0, 5, 0.58, 0.8), Feed("Grower", 5, 100, 0.50, 1.0),
             Feed("Finisher", 100, 10000, 0.45, 1.2)]
    rows = simulate(excel_params(), feeds=feeds)
    for r in rows[1:]:
        prev_w = rows[r["day"] - 2]["weight_g"]
        expected = "Starter" if prev_w < 5 else "Grower" if prev_w < 100 else "Finisher"
        assert r["feed_name"] == expected
    assert {r["fcr"] for r in rows if r["feed_name"] == "Grower"} == {1.0}


@pytest.fixture(scope="module")
def biofilter():
    data = json.loads((FIX / "biofilter.json").read_text(encoding="utf-8"))
    bp = BiofilterParams(**data["inputs"])
    bp.retention_times = ",".join(str(r["retention_min"]) for r in data["rows"])
    return data["rows"], biofilter_table(bp)


@pytest.mark.parametrize("col", [
    "q_tank_m3h", "q_biofilter_m3h", "tan_out", "efficiency_pct", "monod_rate", "k_temp",
    "k_media", "rate_salt", "rate_fresh", "media_salt_m3", "media_fresh_m3",
    "volume_salt_m3", "volume_fresh_m3", "residence_salt_min",
])
def test_biofilter_column(col, biofilter):
    excel, engine = biofilter
    for ex, en in zip(excel, engine):
        assert math.isclose(en[col], ex[col], rel_tol=REL), (ex["retention_min"], col)


def test_biofilter_fresh_residence(biofilter):
    """Excel column V uses the flow from the row above (V14 = T14/H13·60).
    The engine uses the right row; we check the Excel value differs only by that shift."""
    excel, engine = biofilter
    assert math.isclose(engine[0]["residence_fresh_min"], excel[0]["residence_fresh_min"], rel_tol=REL)
    for i in range(1, len(excel)):
        shifted = excel[i]["volume_fresh_m3"] / excel[i - 1]["q_biofilter_m3h"] * 60
        assert math.isclose(shifted, excel[i]["residence_fresh_min"], rel_tol=REL)
