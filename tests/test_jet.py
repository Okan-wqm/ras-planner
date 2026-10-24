"""Jet platform oxygenator: the web calculator gives the numbers of the command-line tool (tools/jet_tasarim.py)."""
import re
import subprocess
import sys
from pathlib import Path

import pytest
from django.urls import reverse

from ras_engine import jet

TOOL = Path(__file__).resolve().parent.parent / "tools" / "jet_tasarim.py"


def cli(*args):
    out = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, encoding="utf-8",
                         env={"PYTHONIOENCODING": "utf-8", "SYSTEMROOT": "C:\\Windows"}).stdout
    rows = {}
    for line in out.splitlines():
        m = re.match(r"\s+(\d+\.\d)\s+(\d+)\s+([\d.]+)( \*)?\s+([\d.]+|ulaşılamaz)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+(\d+)\s+([\d.]+)\s+([\d.]+)", line)
        if m:
            rows[float(m.group(1))] = {"ae": float(m.group(3)), "kgh": float(m.group(6)), "gas": float(m.group(7)),
                                       "vent": float(m.group(8)), "tgp": float(m.group(10)), "kwh": float(m.group(11))}
    holes = int(re.search(r"plakada (\d+) delik", out).group(1))
    return holes, rows


@pytest.mark.parametrize("args,kw", [
    ((), {}),
    (("--debi", "180", "--dusu", "1.1", "--tuzluluk", "33", "--giris-o2", "9", "--hedef", "14", "16", "18"),
     dict(flow_m3h=180, head_m=1.1, salinity=33, o2_in=9, targets=[14, 16, 18])),
    (("--debi", "250", "--bolme", "3", "--o2-saflik", "99.5", "--batiklik", "0.8"),
     dict(flow_m3h=250, compartments=3, o2_purity_pct=99.5, submergence_m=0.8)),
])
def test_same_numbers_as_the_command_line_tool(args, kw):
    args = args or ("--debi", "100")
    kw = kw or dict(flow_m3h=100)
    kw.setdefault("submergence_m", 0.5)
    holes, rows = cli(*args)
    r = jet.design(**kw)
    assert r["nozzle"]["holes"] == holes
    web = {x["target"]: x for x in r["rows"] if x["reachable"]}
    assert set(rows) <= set(web)
    for t, c in rows.items():
        w = web[t]
        assert round(w["ae_pct"], 1) == c["ae"] and round(w["o2_kgh"], 2) == c["kgh"]
        assert round(w["gas_nlmin"], 1) == c["gas"] and round(w["vent_nlmin"], 2) == c["vent"]
        assert round(w["tgp_pct"], 1) == c["tgp"] and round(w["kwh_per_kg"], 2) == c["kwh"]


def test_input_errors():
    with pytest.raises(jet.JetInputError):
        jet.design(flow_m3h=100, head_m=0.2)
    with pytest.raises(jet.JetInputError):
        jet.design(flow_m3h=0)


@pytest.mark.django_db
def test_jet_page(client):
    url = reverse("jet_oxygenator")
    assert client.get(url).status_code == 200
    html = client.get(url, {"flow_m3h": "100", "flow_min_m3h": "60", "head_m": "1.0", "water_depth_m": "1.6",
                            "targets": "14 16"}).content.decode()
    assert "Oxygen transfer by target" in html and "Gate valves for the flow range" in html
    assert "Jet oxygenator" in html   # in the library
    bad = client.get(url, {"flow_m3h": "100", "head_m": "0.2"}).content.decode()
    assert "at least 5 cm" in bad


# reference values from the review (current script, 5 decimals)
@pytest.mark.parametrize("kw,expect", [
    (dict(flow_m3h=100, targets=[13, 15, 18], submergence_m=0.5),
     {13: 0.97968, 15: 0.95336, 18: 0.91915}),
    (dict(flow_m3h=180, head_m=0.6, salinity=33, o2_in=9, targets=[14, 16, 18], submergence_m=0.5),
     {14: 0.81000, 16: 0.66578, 18: 0.42823}),
])
def test_reference_efficiencies(kw, expect):
    r = jet.design(**kw)
    got = {x["target"]: x["ae_pct"] / 100 for x in r["rows"]}
    for t, ae in expect.items():
        assert got[t] == pytest.approx(ae, abs=5e-6)


def test_altitude_lowers_saturation_and_gas_pressure():
    sea, high = jet.design(flow_m3h=100), jet.design(flow_m3h=100, altitude_m=1000)
    assert sea["water"]["O_sat"] == pytest.approx(10.2993, abs=1e-3)
    ratio = high["water"]["O_sat"] / sea["water"]["O_sat"]
    assert 0.87 < ratio < 0.90                       # about 11 % less at 1000 m
    assert high["transfer"]["P_atm"] < sea["transfer"]["P_atm"] - 0.1


def test_default_targets_start_right_above_the_no_vent_point():
    r = jet.design(flow_m3h=100, head_m=0.3, submergence_m=1.0)  # deep box: higher gas pressure
    ts = [x["target"] for x in r["rows"]]
    assert len(ts) >= 3 and ts[0] > r["transfer"]["O_novent"] and ts[-1] < r["transfer"]["O_max"]


def test_gate_valves_use_the_compartments_that_have_water():
    r = jet.design(flow_m3h=100, flow_min_m3h=50, E=0.5)
    for b in r["valves"]["bands"]:
        assert b["some_closed"] == (b["open_rows"] < 4)


@pytest.mark.parametrize("kw", [dict(temp_c=45), dict(salinity=60), dict(cd=1.5), dict(nozzle_mm=5),
                                dict(cushion_m=0.05), dict(compartments=12), dict(submergence_m=0.05), dict(plate_below_m=-0.1),
                                dict(n2_in_pct=200)])
def test_more_input_checks(kw):
    with pytest.raises(jet.JetInputError):
        jet.design(flow_m3h=100, **kw)


@pytest.mark.parametrize("shape", ["rect", "round"])
def test_plate_drawing(shape):
    r = jet.design(flow_m3h=100, shape=shape)
    dr = r["drawing"]
    holes = dr["holes"]
    open_holes = [h for h in holes if not h[3]]
    assert len(holes) == r["nozzle"]["holes"] and len(open_holes) == dr["open"] >= r["nozzle"]["holes_needed"]
    assert {h[2] for h in holes} == set(range(4))                         # every compartment has holes
    for x, y, _c, _spare in holes:                                      # inside the plate
        if shape == "round":
            assert ((x - dr["D"] / 2) ** 2 + (y - dr["D"] / 2) ** 2) ** 0.5 < dr["D"] / 2
        else:
            assert 0 < x < dr["W"] and 0 < y < dr["L"]
    assert r["nozzle"]["U"] <= r["nozzle"]["U_max"] * 1.0001


def test_round_plate_page(client, db):
    html = client.get(reverse("jet_oxygenator"), {"flow_m3h": "150", "shape": "round"}).content.decode()
    assert "Plan view of the nozzle plate" in html and '"shape": "round"' in html


def test_shallow_tank_and_given_baffles():
    shallow = jet.design(flow_m3h=100, water_depth_m=1.0)
    assert shallow["fit"]["ok"] is False and shallow["fit"]["options"]
    assert any("too shallow" in m for m in shallow["msgs"])
    ok = jet.design(flow_m3h=100, water_depth_m=1.6)
    assert ok["fit"]["ok"] and ok["levels"]["plate"] == pytest.approx(-0.10)   # the cushion stays inside the tank water
    assert ok["levels"]["submergence"] == pytest.approx(0.30)
    given = jet.design(flow_m3h=100, baffle_m=0.30, skirt_m=0.45)
    assert given["jets"]["baffle_m"] == 0.30 and given["levels"]["baffle"] == pytest.approx(-0.30 - 0.30)
    assert any("gas may pass under them" in m for m in given["msgs"])   # shorter than the bubble cloud
