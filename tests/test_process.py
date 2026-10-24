"""Mass-balance solver checked against hand calculations."""
import math

import pytest

from ras_engine.process import DiagramError, default_diagram, solve


def node(nid, typ, spec=None, outlets=None):
    return {"id": nid, "type": typ, "position": {"x": 0, "y": 0},
            "data": {"name": nid, "spec": spec or {}, "outlets": outlets or {}}}


def pipe(src, port, dst):
    return {"id": f"{src}.{port}>{dst}", "source": src, "sourceHandle": port, "target": dst, "targetHandle": "in"}


def loop(extra_nodes=(), after_pump="tank", tank_spec=None):
    """makeup 10 → sump → pump 100 → [extra…] → tank; tank side 90 → sump, bottom 10 → discharge."""
    nodes = [node("mk", "makeup", {"flow_m3h": 10, "tss": 0, "tan": 0, "co2": 0, "o2": 0, "no3": 0}),
             node("sump", "sump"), node("pump", "pump", {"flow_m3h": 100}),
             node("tank", "tank", {"tan_kgd": 2.4, "co2_kgd": 0, "tss_kgd": 0, "o2_kgd": 0, **(tank_spec or {})},
                  {"bottom": {"mode": "m3h", "value": 10}, "side": {"mode": "rest"}}),
             node("out", "discharge"), *extra_nodes]
    chain = ["pump", *[n["id"] for n in extra_nodes], "tank"]
    edges = [pipe("mk", "out", "sump"), pipe("sump", "out", "pump"),
             pipe("tank", "side", "sump"), pipe("tank", "bottom", "out")]
    edges += [pipe(a, "out", b) for a, b in zip(chain, chain[1:])]
    return {"nodes": nodes, "edges": edges}


def test_flow_balance_and_tan_by_exchange():
    r = solve(loop())
    assert r["converged"]
    e = r["edges"]
    assert e["tank.side>sump"]["q"] == pytest.approx(90)
    assert e["tank.bottom>out"]["q"] == pytest.approx(10)
    assert r["nodes"]["pump"]["pump_balance"] == pytest.approx(0)
    # 2.4 kg/day = 100 g/h TAN leaves only with 10 m³/h of discharge -> 10 mg/L
    assert e["tank.side>sump"]["c"]["tan"] == pytest.approx(10, rel=1e-5)


def test_biofilter_loop_matches_hand_calculation():
    r = solve(loop([node("bio", "biofilter", {"tan_removal_pct": 50})]))
    assert r["converged"]
    c_tank = r["edges"]["tank.side>sump"]["c"]
    # C_t = 0.5·C_s + 100/100, C_s = 0.9·C_t  ->  C_t = 1 / 0.55
    assert c_tank["tan"] == pytest.approx(1 / 0.55, rel=1e-5)
    # all nitrified N leaves as NO3 with the discharge
    nitrified_g_h = r["nodes"]["bio"]["tan_removed_kgd"] * 1000 / 24
    assert nitrified_g_h == pytest.approx(10 * c_tank["no3"], rel=1e-5)
    assert any("TAN" in x for x in r["edges"]["tank.side>sump"]["exceed"])  # 1.82 > 1 mg/L


def test_slow_exchange_still_converges():
    """1 m³/h of exchange on a 1000 m³/h loop needs acceleration, not thousands of passes."""
    d = loop()
    d["nodes"][0]["data"]["spec"]["flow_m3h"] = 1
    d["nodes"][2]["data"]["spec"]["flow_m3h"] = 1000
    d["nodes"][3]["data"]["outlets"]["bottom"] = {"mode": "m3h", "value": 1}
    r = solve(d)
    assert r["converged"] and r["iterations"] < 50
    assert r["edges"]["tank.side>sump"]["c"]["tan"] == pytest.approx(100, rel=1e-4)


def test_split_by_m3h_and_rest():
    nodes = [node("p1", "pump", {"flow_m3h": 10}), node("p2", "pump", {"flow_m3h": 15}),
             node("p3", "pump", {"flow_m3h": 20}),
             node("sp", "splitter", outlets={"a": {"mode": "m3h", "value": 5}, "b": {"mode": "rest"},
                                             "c": {"mode": "pct", "value": 0}}),
             node("d1", "discharge"), node("d2", "discharge")]
    edges = [pipe("p1", "out", "sp"), pipe("p2", "out", "sp"), pipe("p3", "out", "sp"),
             pipe("sp", "a", "d1"), pipe("sp", "b", "d2")]
    r = solve({"nodes": nodes, "edges": edges})
    assert r["edges"]["sp.a>d1"]["q"] == pytest.approx(5)
    assert r["edges"]["sp.b>d2"]["q"] == pytest.approx(40)


def test_outlets_exceeding_inflow_warn():
    nodes = [node("p", "pump", {"flow_m3h": 10}),
             node("sp", "splitter", outlets={"a": {"mode": "m3h", "value": 8}, "b": {"mode": "m3h", "value": 8},
                                             "c": {"mode": "pct", "value": 0}}),
             node("d", "discharge")]
    r = solve({"nodes": nodes, "edges": [pipe("p", "out", "sp"), pipe("sp", "a", "d"), pipe("sp", "b", "d")]})
    assert any("only 10.0" in w for w in r["nodes"]["sp"]["warnings"])


def test_loop_without_pump_is_rejected():
    nodes = [node("a", "sump"), node("b", "sump")]
    with pytest.raises(DiagramError):
        solve({"nodes": nodes, "edges": [pipe("a", "out", "b"), pipe("b", "out", "a")]})


def test_pump_shortfall_reported():
    d = loop()
    d["nodes"][0]["data"]["spec"]["flow_m3h"] = 5  # make-up smaller than the 10 m³/h discharged
    r = solve(d)
    assert r["nodes"]["pump"]["pump_balance"] == pytest.approx(-5)
    assert any("make-up" in w for w in r["nodes"]["pump"]["warnings"])


def test_drum_filter_sends_solids_to_backwash():
    nodes = [node("p", "pump", {"flow_m3h": 100}), node("mk", "makeup", {"flow_m3h": 0}),
             node("t", "tank", {"tss_kgd": 24, "tan_kgd": 0, "co2_kgd": 0, "o2_kgd": 0},
                  {"bottom": {"mode": "pct", "value": 0}, "side": {"mode": "rest"}}),
             node("drum", "drum_filter", {"tss_removal_pct": 60}, {"sludge": {"mode": "m3h", "value": 1}, "out": {"mode": "rest"}}),
             node("d", "discharge")]
    edges = [pipe("p", "out", "t"), pipe("t", "side", "drum"), pipe("drum", "out", "d"), pipe("drum", "sludge", "d")]
    r = solve({"nodes": nodes, "edges": edges})
    tss_in_g_h = 24 * 1000 / 24  # 1000 g/h
    sludge, clean = r["edges"]["drum.sludge>d"], r["edges"]["drum.out>d"]
    removed = 0.6 * tss_in_g_h
    assert sludge["q"] * sludge["c"]["tss"] == pytest.approx(removed + 0.4 * tss_in_g_h * 1 / 100, rel=1e-6)
    assert clean["q"] * clean["c"]["tss"] == pytest.approx(0.4 * tss_in_g_h * 99 / 100, rel=1e-6)


def test_cone_pure_oxygen():
    nodes = [node("p", "pump", {"flow_m3h": 100}), node("cone", "cone", {"o2_target_mgl": 20, "transfer_eff_pct": 80}),
             node("d", "discharge")]
    r = solve({"nodes": nodes, "edges": [pipe("p", "out", "cone"), pipe("cone", "out", "d")]})
    # pump water carries 0 mg/L; 100 m³/h × 20 g/m³ = 2000 g/h = 48 kg/day, / 0.8
    assert r["nodes"]["cone"]["pure_o2_kgd"] == pytest.approx(60)
    assert r["edges"]["cone.out>d"]["c"]["o2"] == pytest.approx(20)


def _tank_rig(kind, outlets, spec_extra=None):
    """pump 100 → one tank of `kind` (24 kg/day TSS = 1000 g/h) → each outlet to its own discharge."""
    from ras_engine.process import NODE_TYPES
    ports = [p for p, _ in NODE_TYPES[kind]["outlets"]]
    nodes = [node("p", "pump", {"flow_m3h": 100}),
             node("t", kind, {"tss_kgd": 24, "tan_kgd": 2.4, "co2_kgd": 0, "o2_kgd": 0, **(spec_extra or {})}, outlets)]
    nodes += [node(f"d{p}", "discharge") for p in ports]
    edges = [pipe("p", "out", "t")] + [pipe("t", p, f"d{p}") for p in ports]
    r = solve({"nodes": nodes, "edges": edges})
    get = lambda p: r["edges"][f"t.{p}>d{p}"]  # noqa: E731
    return r, get


def test_tank_bottom_only_takes_everything():
    r, get = _tank_rig("tank_bottom", {})
    assert get("bottom")["q"] == pytest.approx(100)
    assert get("bottom")["q"] * get("bottom")["c"]["tss"] == pytest.approx(1000)


def test_raceway_carries_everything_out_the_far_end():
    """No bottom drain to split solids over: flow, TSS and TAN all leave through the single end outlet."""
    r, get = _tank_rig("raceway", {})
    assert get("out")["q"] == pytest.approx(100)
    assert get("out")["q"] * get("out")["c"]["tss"] == pytest.approx(1000)   # all 24 kg/day TSS
    assert get("out")["c"]["tan"] == pytest.approx(2.4 * 1000 / 24 / 100)    # dissolved follows the flow


def test_tank_with_centre_pipe_splits_solids():
    r, get = _tank_rig("tank_center", {"bottom": {"mode": "pct", "value": 15}, "center": {"mode": "rest"}},
                       {"bottom_tss_pct": 80})
    b, c = get("bottom"), get("center")
    assert b["q"] == pytest.approx(15) and c["q"] == pytest.approx(85)
    assert b["q"] * b["c"]["tss"] == pytest.approx(800)
    assert c["q"] * c["c"]["tss"] == pytest.approx(200)
    assert b["c"]["tan"] == pytest.approx(c["c"]["tan"])  # dissolved TAN follows the flow


def test_three_outlet_tank():
    r, get = _tank_rig("tank_3", {"bottom": {"mode": "pct", "value": 10}, "center": {"mode": "pct", "value": 20},
                                  "side": {"mode": "rest"}}, {"bottom_tss_pct": 70, "center_tss_pct": 20})
    masses = {p: get(p)["q"] * get(p)["c"]["tss"] for p in ("bottom", "center", "side")}
    assert masses == pytest.approx({"bottom": 700, "center": 200, "side": 100})
    assert get("side")["q"] == pytest.approx(70)


def test_two_inlets_are_summed():
    nodes = [node("p1", "pump", {"flow_m3h": 30}), node("p2", "pump", {"flow_m3h": 70}),
             node("t", "tank_bottom", {"tss_kgd": 0, "tan_kgd": 0, "co2_kgd": 0, "o2_kgd": 0}), node("d", "discharge")]
    nodes[2]["data"]["inlets"] = 2
    edges = [pipe("p1", "out", "t"), {**pipe("p2", "out", "t"), "targetHandle": "in2"}, pipe("t", "bottom", "d")]
    r = solve({"nodes": nodes, "edges": edges})
    assert r["edges"]["t.bottom>d"]["q"] == pytest.approx(100)


def test_connection_point_sums_flows_and_loads():
    nodes = [node("mk1", "makeup", {"flow_m3h": 10, "tan": 1, "tss": 0, "co2": 0, "o2": 0, "no3": 0}),
             node("mk2", "makeup", {"flow_m3h": 30, "tan": 3, "tss": 0, "co2": 0, "o2": 0, "no3": 0}),
             node("mk3", "makeup", {"flow_m3h": 20, "tan": 0, "tss": 0, "co2": 0, "o2": 0, "no3": 0}),
             node("mk4", "makeup", {"flow_m3h": 40, "tan": 2, "tss": 0, "co2": 0, "o2": 0, "no3": 0}),
             node("j", "junction"), node("d", "discharge")]
    edges = [pipe(f"mk{i}", "out", "j") for i in range(1, 5)] + [pipe("j", "out", "d")]
    r = solve({"nodes": nodes, "edges": edges})
    out = r["edges"]["j.out>d"]
    assert out["q"] == pytest.approx(100)
    assert out["c"]["tan"] == pytest.approx((10 * 1 + 30 * 3 + 0 + 40 * 2) / 100)


def test_connection_point_as_header_tap():
    """Pump 100 m³/h into a connection point: 30 m³/h tapped to tank 1 from the bottom port,
    the rest carries on from the right port to the next tap."""
    j1 = node("j1", "junction", outlets={"bottom": {"mode": "m3h", "value": 30}, "right": {"mode": "rest"}})
    j2 = node("j2", "junction")  # defaults: left/top in, right/bottom out, both 'rest' -> equal split
    nodes = [node("p", "pump", {"flow_m3h": 100}), j1, j2,
             node("t1", "tank_bottom"), node("t2", "tank_bottom"), node("t3", "tank_bottom"), node("d", "discharge")]
    edges = [{**pipe("p", "out", "j1"), "targetHandle": "left"},
             pipe("j1", "bottom", "t1"), {**pipe("j1", "right", "j2"), "targetHandle": "left"},
             pipe("j2", "bottom", "t2"), pipe("j2", "right", "t3"),
             pipe("t1", "bottom", "d"), pipe("t2", "bottom", "d"), pipe("t3", "bottom", "d")]
    r = solve({"nodes": nodes, "edges": edges})
    e = r["edges"]
    assert e["j1.bottom>t1"]["q"] == pytest.approx(30)
    assert e["j1.right>j2"]["q"] == pytest.approx(70)
    assert e["j2.bottom>t2"]["q"] == pytest.approx(35) and e["j2.right>t3"]["q"] == pytest.approx(35)


def test_connection_point_roles_can_be_swapped():
    j = node("j", "junction", outlets={"left": {"mode": "rest"}})
    j["data"]["roles"] = {"top": "in", "right": "in", "bottom": "in", "left": "out"}
    nodes = [node("p", "pump", {"flow_m3h": 50}), j, node("d", "discharge")]
    edges = [{**pipe("p", "out", "j"), "targetHandle": "right"}, pipe("j", "left", "d"),
             pipe("j", "bottom", "d")]  # bottom is an inlet now: this pipe is ignored with a warning
    r = solve({"nodes": nodes, "edges": edges})
    assert r["edges"]["j.left>d"]["q"] == pytest.approx(50)
    assert "j.bottom>d" not in r["edges"]
    assert any("inlet" in w for w in r["nodes"]["j"]["warnings"])


def test_pump_vfd_follows_affinity_laws():
    p = node("p", "pump", {"flow_m3h": 100, "vfd": True, "nominal_hz": 50, "frequency_hz": 45, "power_kw": 10})
    r = solve({"nodes": [p, node("d", "discharge")], "edges": [pipe("p", "out", "d")]})
    assert r["edges"]["p.out>d"]["q"] == pytest.approx(90)                 # Q ∝ f
    assert r["nodes"]["p"]["pump"]["power_kw"] == pytest.approx(10 * 0.9 ** 3)   # P ∝ f³
    assert r["pump_power_kw"] == pytest.approx(7.29)
    # without a VFD the operating frequency is ignored
    p["data"]["spec"]["vfd"] = False
    r = solve({"nodes": [p, node("d", "discharge")], "edges": [pipe("p", "out", "d")]})
    assert r["edges"]["p.out>d"]["q"] == pytest.approx(100)


def test_duty_and_standby_pumps():
    """Sump feeds a duty pump and a standby pump from two outlets: only the duty pump gets water."""
    nodes = [node("mk", "makeup", {"flow_m3h": 80, "tss": 0, "tan": 0, "co2": 0, "o2": 0, "no3": 0}),
             node("s", "sump"),
             node("duty", "pump", {"flow_m3h": 80}),
             node("stby", "pump", {"flow_m3h": 80, "active": False}),
             node("d", "discharge")]
    edges = [pipe("mk", "out", "s"), pipe("s", "out", "duty"), pipe("s", "out2", "stby"),
             pipe("duty", "out", "d"), pipe("stby", "out", "d")]
    r = solve({"nodes": nodes, "edges": edges})
    assert r["edges"]["s.out>duty"]["q"] == pytest.approx(80)
    assert r["edges"]["s.out2>stby"]["q"] == 0 and r["edges"]["stby.out>d"]["q"] == 0
    assert not r["nodes"]["stby"]["warnings"] and not r["nodes"]["duty"]["warnings"]
    assert r["nodes"]["stby"]["pump"]["running"] is False
    # swap: standby runs, duty stops
    nodes[2]["data"]["spec"]["active"] = False
    nodes[3]["data"]["spec"]["active"] = True
    r = solve({"nodes": nodes, "edges": edges})
    assert r["edges"]["s.out2>stby"]["q"] == pytest.approx(80) and r["edges"]["s.out>duty"]["q"] == 0


def test_sump_three_in_three_out_only_connected_outlets_take_water():
    nodes = [node("p1", "pump", {"flow_m3h": 30}), node("p2", "pump", {"flow_m3h": 30}), node("p3", "pump", {"flow_m3h": 40}),
             node("s", "sump"), node("d1", "discharge"), node("d2", "discharge")]
    edges = [{**pipe("p1", "out", "s")}, {**pipe("p2", "out", "s"), "targetHandle": "in2"},
             {**pipe("p3", "out", "s"), "targetHandle": "in3"},
             pipe("s", "out", "d1"), pipe("s", "out3", "d2")]   # outlet 2 left unconnected
    r = solve({"nodes": nodes, "edges": edges})
    assert r["edges"]["s.out>d1"]["q"] == pytest.approx(50) and r["edges"]["s.out3>d2"]["q"] == pytest.approx(50)
    assert not any("not connected" in w for w in r["nodes"]["s"]["warnings"])


def test_sand_filter_backwash_takes_the_solids():
    nodes = [node("p", "pump", {"flow_m3h": 100}),
             node("t", "tank_bottom", {"tss_kgd": 24, "tan_kgd": 0, "co2_kgd": 0, "o2_kgd": 0}),
             node("sf", "sand_filter", {"tss_removal_pct": 70}, {"backwash": {"mode": "m3h", "value": 2}, "out": {"mode": "rest"}}),
             node("d", "discharge"), node("w", "discharge")]
    edges = [pipe("p", "out", "t"), pipe("t", "bottom", "sf"), pipe("sf", "out", "d"), pipe("sf", "backwash", "w")]
    r = solve({"nodes": nodes, "edges": edges})
    bw, clean = r["edges"]["sf.backwash>w"], r["edges"]["sf.out>d"]
    assert bw["q"] == pytest.approx(2) and clean["q"] == pytest.approx(98)
    assert bw["q"] * bw["c"]["tss"] + clean["q"] * clean["c"]["tss"] == pytest.approx(1000)   # mass conserved
    assert clean["q"] * clean["c"]["tss"] == pytest.approx(300 * 98 / 100)                    # 70 % to backwash


def test_incubator_adds_egg_loads():
    nodes = [node("mk", "makeup", {"flow_m3h": 10, "tss": 0, "tan": 0, "co2": 2, "o2": 9, "no3": 0}),
             node("inc", "incubator", {"o2_kgd": 0.24, "tan_kgd": 0.024, "co2_kgd": 0.24}), node("d", "discharge")]
    r = solve({"nodes": nodes, "edges": [pipe("mk", "out", "inc"), pipe("inc", "out", "d")]})
    c = r["edges"]["inc.out>d"]["c"]
    # 10 m³/h: 0.24 kg/day = 10 g/h -> 1 mg/L
    assert c["o2"] == pytest.approx(9 - 1) and c["co2"] == pytest.approx(2 + 1) and c["tan"] == pytest.approx(0.1)


def test_degasser_four_inlets_are_summed():
    nodes = [node(f"p{i}", "pump", {"flow_m3h": q}) for i, q in enumerate([10, 20, 30, 40], 1)]
    nodes += [node("dg", "degasser"), node("d", "discharge")]
    edges = [{**pipe(f"p{i}", "out", "dg"), "targetHandle": h} for i, h in enumerate(["in", "in2", "in3", "in4"], 1)]
    edges.append(pipe("dg", "out", "d"))
    r = solve({"nodes": nodes, "edges": edges})
    assert r["edges"]["dg.out>d"]["q"] == pytest.approx(100)


def test_drains_are_closed_by_default_and_open_on_request():
    nodes = [node("p", "pump", {"flow_m3h": 100}), node("uv", "uv"), node("d", "discharge"), node("w", "discharge")]
    edges = [pipe("p", "out", "uv"), pipe("uv", "out", "d"), pipe("uv", "drain", "w")]
    r = solve({"nodes": nodes, "edges": edges})
    assert r["edges"]["uv.out>d"]["q"] == pytest.approx(100) and r["edges"]["uv.drain>w"]["q"] == 0
    nodes[1]["data"]["outlets"] = {"drain": {"mode": "m3h", "value": 2}}
    r = solve({"nodes": nodes, "edges": edges})
    assert r["edges"]["uv.out>d"]["q"] == pytest.approx(98) and r["edges"]["uv.drain>w"]["q"] == pytest.approx(2)


def test_fbbr_nitrifies_and_traps_solids():
    nodes = [node("p", "pump", {"flow_m3h": 100}),
             node("t", "tank_bottom", {"tan_kgd": 2.4, "tss_kgd": 24, "co2_kgd": 0, "o2_kgd": 0}),
             node("f", "fbbr", {"tan_removal_pct": 60, "tss_removal_pct": 30}), node("d", "discharge")]
    edges = [pipe("p", "out", "t"), pipe("t", "bottom", "f"), pipe("f", "out", "d")]
    r = solve({"nodes": nodes, "edges": edges})
    out = r["edges"]["f.out>d"]["c"]
    assert out["tan"] == pytest.approx(1.0 * 0.4)      # 100 g/h TAN in 100 m³/h, 60 % nitrified
    assert out["no3"] == pytest.approx(1.0 * 0.6)
    assert out["tss"] == pytest.approx(10 * 0.7)       # 1000 g/h in 100 m³/h, 30 % trapped
    assert r["nodes"]["f"]["tss_captured_kgd"] == pytest.approx(7.2)


def test_ozone_generator_feeds_contactor_by_gas_line():
    gen = node("g", "ozone_generator", {"output_g_h": 40})
    nodes = [node("p", "pump", {"flow_m3h": 200}), node("oz", "ozone"), gen, node("d", "discharge")]
    edges = [pipe("p", "out", "oz"), pipe("oz", "out", "d"), {**pipe("g", "gas", "oz"), "targetHandle": "gas"}]
    r = solve({"nodes": nodes, "edges": edges})
    assert r["converged"]
    assert r["nodes"]["oz"]["ozone_g_h"] == pytest.approx(40)
    assert r["nodes"]["oz"]["ozone_mg_per_l"] == pytest.approx(0.2)
    assert r["edges"]["g.gas>oz"]["gas"] and r["edges"]["oz.out>d"]["q"] == pytest.approx(200)  # gas adds no water
    assert r["nodes"]["g"]["output_kgd"] == pytest.approx(0.96)
    # an explicit dose on the contactor wins over the generator
    nodes[1]["data"]["spec"] = {"dose_g_h": 10}
    assert solve({"nodes": nodes, "edges": edges})["nodes"]["oz"]["ozone_g_h"] == pytest.approx(10)
    # an unconnected generator is flagged
    r = solve({"nodes": [gen], "edges": []})
    assert any("not connected" in w for w in r["nodes"]["g"]["warnings"])


@pytest.mark.parametrize("n_tanks", [1, 3, 4])
def test_default_diagram_solves(n_tanks):
    d = default_diagram("Fingerling", 400, {"tan_kgd": 2, "co2_kgd": 20, "tss_kgd": 10, "o2_kgd": 15}, n_tanks)
    r = solve(d)
    assert r["converged"]
    into_tanks = sum(v["q"] for k, v in r["edges"].items() if k.startswith("e-split-"))
    assert into_tanks == pytest.approx(400)
    tanks = [k for k in r["edges"] if k.startswith("e-split-")]
    assert len(tanks) == n_tanks
    for k in tanks:  # every tank gets the same share
        assert r["edges"][k]["q"] == pytest.approx(400 / n_tanks)
    # make-up replaces exactly the water lost with sludge: the pump is balanced
    assert r["nodes"]["pump"]["pump_balance"] == pytest.approx(0, abs=1e-2)
    assert r["makeup_m3h"] == pytest.approx(r["discharge_m3h"], abs=1e-2)


# --- temperature: chiller and heat exchangers

def once_through(middle, makeup_spec, outlets=("out",)):
    nodes = [node("mk", "makeup", makeup_spec), middle] + [node(f"d{p}", "discharge") for p in outlets]
    edges = [pipe("mk", "out", middle["id"])] + [pipe(middle["id"], p, f"d{p}") for p in outlets]
    return solve({"nodes": nodes, "edges": edges})


def test_chiller_cools_to_set_point_within_capacity():
    r = once_through(node("ch", "chiller", {"setpoint_c": 12, "cop": 3}), {"flow_m3h": 10, "temp": 20})
    ch = r["nodes"]["ch"]
    assert ch["cooling_kw"] == pytest.approx(10 * 1.163 * 8)
    assert ch["chiller_power_kw"] == pytest.approx(10 * 1.163 * 8 / 3)
    assert r["edges"]["ch.out>dout"]["c"]["temp"] == pytest.approx(12)
    assert r["cooling_kw"] == pytest.approx(ch["cooling_kw"]) and not ch["warnings"]
    # too small: cools as far as it can and says so
    r = once_through(node("ch", "chiller", {"setpoint_c": 12, "capacity_kw": 50}), {"flow_m3h": 10, "temp": 20})
    assert r["edges"]["ch.out>dout"]["c"]["temp"] == pytest.approx(20 - 50 / 11.63)
    assert any("short of the set point" in w for w in r["nodes"]["ch"]["warnings"])
    # water already colder than the set point is not heated
    r = once_through(node("ch", "chiller", {"setpoint_c": 12}), {"flow_m3h": 10, "temp": 9})
    assert r["nodes"]["ch"]["cooling_kw"] == 0 and r["edges"]["ch.out>dout"]["c"]["temp"] == pytest.approx(9)


def test_plate_heat_exchanger_with_utility_from_specification():
    hx = node("hx", "hx_plate", {"utility_temp_c": 6, "utility_flow_m3h": 20})
    r = once_through(hx, {"flow_m3h": 20, "temp": 18}, outlets=("out", "out2"))
    res = r["nodes"]["hx"]
    assert res["mode"] == "cooling" and res["duty_kw"] == pytest.approx(0.85 * 20 * 1.163 * 12)
    assert r["edges"]["hx.out>dout"]["c"]["temp"] == pytest.approx(18 - 0.85 * 12)
    assert r["edges"]["hx.out>dout"]["q"] == pytest.approx(20) and r["edges"]["hx.out2>dout2"]["q"] == 0


def test_shell_and_tube_keeps_the_two_circuits_apart():
    nodes = [node("a", "makeup", {"flow_m3h": 10, "temp": 20, "tan": 2}),
             node("b", "makeup", {"flow_m3h": 5, "temp": 4, "tan": 0}),
             node("hx", "hx_tube"), node("d1", "discharge"), node("d2", "discharge")]
    utility_in = {**pipe("b", "out", "hx"), "targetHandle": "in2"}
    r = solve({"nodes": nodes, "edges": [pipe("a", "out", "hx"), utility_in,
                                         pipe("hx", "out", "d1"), pipe("hx", "out2", "d2")]})
    proc, util = r["edges"]["hx.out>d1"], r["edges"]["hx.out2>d2"]
    assert proc["q"] == pytest.approx(10) and util["q"] == pytest.approx(5)
    assert proc["c"]["tan"] == pytest.approx(2) and util["c"]["tan"] == 0  # no water crosses
    # ε = 60 %, C_min on the utility side (5 m³/h)
    assert util["c"]["temp"] == pytest.approx(4 + 0.6 * 16)
    assert proc["c"]["temp"] == pytest.approx(20 - 0.6 * 5 * 16 / 10)
    assert r["nodes"]["hx"]["duty_kw"] == pytest.approx(0.6 * 5 * 1.163 * 16)
    assert r["nodes"]["hx"]["c_in"]["temp"] == pytest.approx(20)  # process side only


def test_pump_and_tank_heat_are_removed_by_the_chiller():
    nodes = [node("p", "pump", {"flow_m3h": 100, "power_kw": 11.63}),
             node("t", "tank_bottom", {"heat_kw": 5.815}), node("ch", "chiller", {"setpoint_c": 12})]
    r = solve({"nodes": nodes, "edges": [pipe("p", "out", "t"), pipe("t", "bottom", "ch"), pipe("ch", "out", "p")]})
    assert r["converged"]
    assert r["edges"]["t.bottom>ch"]["c"]["temp"] == pytest.approx(12.15)
    assert r["nodes"]["ch"]["cooling_kw"] == pytest.approx(11.63 + 5.815)


def test_reservoir_stores_river_water_and_settles_solids():
    nodes = [node("river", "makeup", {"flow_m3h": 50, "tss": 20}),
             node("res", "reservoir", {"volume_m3": 400, "tss_removal_pct": 50},
                  {"drain": {"mode": "m3h", "value": 1}}),
             node("d1", "discharge"), node("d2", "discharge")]
    r = solve({"nodes": nodes, "edges": [pipe("river", "out", "res"), pipe("res", "out", "d1"), pipe("res", "drain", "d2")]})
    res = r["nodes"]["res"]
    assert res["retention_h"] == pytest.approx(8) and not res["warnings"]
    assert r["edges"]["res.out>d1"]["q"] == pytest.approx(49)       # the unpiped Outlet 2 takes nothing
    assert r["edges"]["res.out>d1"]["c"]["tss"] == pytest.approx(490 / 49)
    assert r["edges"]["res.drain>d2"]["c"]["tss"] == pytest.approx(510)


@pytest.mark.parametrize("kind,port", [("tank", "side"), ("tank_bottom", "bottom"), ("tank_center", "center"),
                                       ("tank_3", "side"), ("raceway", "out")])
def test_tank_limits_checked_for_every_tank_type(kind, port):
    """The tank water (not the bottom drain's sludge) is checked once per tank, whatever its outlets."""
    outlets = {"bottom": {"mode": "pct", "value": 20}} if kind not in ("tank_bottom", "raceway") else {}
    nodes = [node("mk", "makeup", {"flow_m3h": 10, "tss": 0, "tan": 0, "co2": 0, "o2": 9, "no3": 0}),
             node("t", kind, {"tan_kgd": 1.0, "tss_kgd": 0.5}, outlets), node("d", "discharge")]
    edges = [pipe("mk", "out", "t")] + [pipe("t", p, "d") for p, _ in
                                        __import__("ras_engine.process", fromlist=["NODE_TYPES"]).NODE_TYPES[kind]["outlets"]]
    r = solve({"nodes": nodes, "edges": edges})
    warns = [w for w in r["nodes"]["t"]["warnings"] if "outside limits" in w]
    assert len(warns) == 1 and "TAN" in warns[0]           # 1 kg/day in 10 m³/h = 4.2 mg/L > 1
    assert r["edges"][f"t.{port}>d"]["exceed"]


def test_bottom_drain_solids_are_not_tank_water():
    nodes = [node("mk", "makeup", {"flow_m3h": 100, "tss": 0, "tan": 0, "co2": 0, "o2": 9, "no3": 0}),
             node("t", "tank", {"tss_kgd": 10.0, "bottom_tss_pct": 80}, {"bottom": {"mode": "pct", "value": 10}}),
             node("d", "discharge")]
    r = solve({"nodes": nodes, "edges": [pipe("mk", "out", "t"), pipe("t", "bottom", "d"), pipe("t", "side", "d")]})
    assert r["edges"]["t.bottom>d"]["c"]["tss"] > 10.5          # sludge is concentrated…
    assert not r["nodes"]["t"]["warnings"]                       # …but the tank water (side outlet) is fine


@pytest.mark.parametrize("kind", ["jet_rect", "jet_round"])
def test_jet_oxygenator_node_uses_the_jet_engine(kind):
    from ras_engine import jet
    nodes = [node("mk", "makeup", {"flow_m3h": 100, "o2": 8, "temp": 14, "tss": 0, "tan": 0, "co2": 0, "no3": 0}),
             node("j", kind, {"o2_target_mgl": 15}), node("d", "discharge")]
    r = solve({"nodes": nodes, "edges": [pipe("mk", "out", "j"), pipe("j", "out", "d")]})
    res = r["nodes"]["j"]
    assert r["edges"]["j.out>d"]["c"]["o2"] == pytest.approx(15)
    ref = jet.design(flow_m3h=100, o2_in=8, temp_c=14, targets=[15], shape="round" if kind == "jet_round" else "rect")
    assert res["jet_ae_pct"] == pytest.approx(ref["rows"][0]["ae_pct"])
    assert res["pure_o2_kgd"] == pytest.approx(100 * 7 * 24 / 1000 / (ref["rows"][0]["ae_pct"] / 100))
    assert r["totals"]["pure_o2_kgd"] == pytest.approx(res["pure_o2_kgd"])   # counted with the oxygen supply
    # a target above what the platform can reach is flagged
    nodes[1]["data"]["spec"]["o2_target_mgl"] = 60
    r = solve({"nodes": nodes, "edges": [pipe("mk", "out", "j"), pipe("j", "out", "d")]})
    assert any("cannot reach" in w for w in r["nodes"]["j"]["warnings"])


def test_biofilters_share_the_required_media():
    """Three biofilters: one has its own media, one takes 50 % of what is left, the third the rest;
    vessel = media / fill ratio. Each shows TAN in, TAN out and its efficiency."""
    from ras_engine.process import solve
    def node(nid, kind, **spec):
        return {"id": nid, "type": kind, "position": {"x": 0, "y": 0}, "data": {"spec": spec}}
    nodes = [node("mk", "makeup", flow_m3h=10, tan=2.0), node("a", "biofilter", media_m3=4),
             node("b", "fbbr", share_pct=50, fill_pct=90), node("c", "biofilter"), node("ds", "discharge")]
    pipe = lambda a, b: {"id": a + b, "source": a, "sourceHandle": "out", "target": b, "targetHandle": "in"}  # noqa: E731
    r = solve({"nodes": nodes, "edges": [pipe("mk", "a"), pipe("a", "b"), pipe("b", "c"), pipe("c", "ds")],
               "basis": {"media_m3": 24.0, "fill_pct": 40.0}})
    n = r["nodes"]
    assert n["a"]["media_m3"] == 4 and n["b"]["media_m3"] == pytest.approx(10) and n["c"]["media_m3"] == pytest.approx(10)
    assert n["b"]["vessel_m3"] == pytest.approx(10 / 0.9) and n["c"]["vessel_m3"] == pytest.approx(25)
    assert r["totals"]["media_m3"] == pytest.approx(24)
    assert n["a"]["tan_in_mgl"] == pytest.approx(2.0) and n["a"]["tan_out_mgl"] == pytest.approx(1.0)
    assert n["b"]["tan_in_mgl"] == pytest.approx(1.0) and n["b"]["tan_eff_pct"] == 60


def test_tank_loads_from_feed_and_solids_followed_through_pipes():
    """Feed 24 kg/day shared over two tanks by volume; TSS 0.5 kg/kg feed, 70 % of it out of the bottom drain to a
    settler that takes 50 %: the pipes carry the right TSS (mass balance by hand)."""
    from ras_engine.process import solve
    def node(nid, kind, **spec):
        return {"id": nid, "type": kind, "position": {"x": 0, "y": 0}, "data": {"spec": spec}}
    nodes = [node("mk", "makeup", flow_m3h=20, tss=0, tan=0, co2=0, o2=10), node("sp", "splitter"),
             node("t1", "tank", volume_m3=30, bottom_tss_pct=70), node("t2", "tank", volume_m3=10, bottom_tss_pct=70),
             node("rf", "rfs", tss_removal_pct=50), node("ds", "discharge")]
    nodes[2]["data"]["outlets"] = {"bottom": {"mode": "m3h", "value": 2}, "side": {"mode": "rest"}}
    def pipe(a, b, sh="out", eid=None):
        return {"id": eid or f"{a}-{sh}-{b}", "source": a, "sourceHandle": sh, "target": b, "targetHandle": "in"}
    edges = [pipe("mk", "t1"), pipe("t1", "rf", "bottom"), pipe("t1", "ds", "side"), pipe("rf", "ds"),
             pipe("rf", "ds", "sludge")]
    fish = {"feed_kgd": 24.0, "tss_per_feed": 0.5, "tan_per_feed": 0.04, "co2_per_feed": 0.5, "o2_per_feed": 0.1}
    r = solve({"nodes": nodes[:3] + nodes[4:], "edges": edges, "basis": {"fish": fish}})
    t1 = r["nodes"]["t1"]
    assert t1["feed_kgd"] == pytest.approx(24.0)                       # the only tank takes all the feed
    assert t1["tss_gh"] == pytest.approx(24 * 0.5 * 1000 / 24)         # 500 g/h
    bottom = r["edges"]["t1-bottom-rf"]
    assert bottom["q"] * bottom["c"]["tss"] == pytest.approx(0.7 * 500)  # 350 g/h down the bottom drain
    side = r["edges"]["t1-side-ds"]
    assert side["q"] * side["c"]["tss"] == pytest.approx(0.3 * 500)
    clear = r["edges"]["rf-out-ds"]
    assert clear["q"] * clear["c"]["tss"] == pytest.approx(0.5 * 350 * 0.98)  # settler keeps half; 2 % of the rest leaves with the sludge water
    assert side["c"]["o2"] == pytest.approx(10 - 0.1 * 24 * 1000 / 24 / 20)  # fish O₂ use per hour / flow


def test_fish_shared_over_tanks_and_feed_follows_them():
    """Three tanks: one gets 50 % of the fish, the other two share the rest by volume; feed follows the fish.
    An ozone generator left at 0 makes the plan's ozone per kg of the day's feed."""
    from ras_engine.process import solve
    def node(nid, kind, **spec):
        return {"id": nid, "type": kind, "position": {"x": 0, "y": 0}, "data": {"spec": spec}}
    nodes = [node("a", "tank_bottom", volume_m3=10, biomass_pct=50), node("b", "tank_bottom", volume_m3=30),
             node("c", "tank_bottom", volume_m3=10), node("g", "ozone_generator")]
    fish = {"feed_kgd": 40.0, "biomass_kg": 2000.0, "tss_per_feed": 0.5, "tan_per_feed": 0.04, "co2_per_feed": 0.5,
            "o2_per_feed": 0.3, "ozone_per_feed": 0.015}
    r = solve({"nodes": nodes, "edges": [], "basis": {"fish": fish}})
    n = r["nodes"]
    assert n["a"]["biomass_kg"] == pytest.approx(1000) and n["a"]["feed_kgd"] == pytest.approx(20)
    assert n["b"]["biomass_pct"] == pytest.approx(37.5) and n["c"]["feed_kgd"] == pytest.approx(5)
    assert n["b"]["density_kgm3"] == pytest.approx(750 / 30)
    assert n["a"]["tan_gh"] == pytest.approx(20 * 0.04 * 1000 / 24)
    assert n["g"]["output_g_h"] == pytest.approx(40 * 0.015 * 1000 / 24)


def test_hourly_loads_and_auto_oxygen_cone():
    """An hour of the day: the tanks take that hour's loads (kg/day at that rate) by their share of the fish.
    A cone left on auto lifts O₂ just enough for the tank it feeds to keep the minimum."""
    from ras_engine.process import solve
    def node(nid, kind, **spec):
        return {"id": nid, "type": kind, "position": {"x": 0, "y": 0}, "data": {"spec": spec}}
    nodes = [node("mk", "makeup", flow_m3h=10, o2=8), node("cn", "cone"), node("t", "tank_bottom", volume_m3=10),
             node("ds", "discharge")]
    pipe = lambda a, b: {"id": a + b, "source": a, "sourceHandle": "out" if a != "t" else "bottom", "target": b, "targetHandle": "in"}  # noqa: E731
    fish = {"feed_kgd": 24.0, "biomass_kg": 1000.0, "feed_now_kgh": 2.0,
            "loads": {"tss_kgd": 48 * 0.5, "tan_kgd": 1.2, "o2_kgd": 12.0, "co2_kgd": 12.0 * 1.375}}
    r = solve({"nodes": nodes, "edges": [pipe("mk", "cn"), pipe("cn", "t"), pipe("t", "ds")],
               "limits": {"o2": {"min": 7.0}}, "basis": {"fish": fish}})
    t = r["nodes"]["t"]
    assert t["feed_now_kgh"] == 2.0 and t["tss_gh"] == pytest.approx(1000) and t["co2_gh"] == pytest.approx(687.5)
    cone = r["nodes"]["cn"]
    assert cone["o2_auto"] and cone["o2_target_mgl"] == pytest.approx(7.0 + 500 / 10)   # 12 kg/day = 500 g/h over 10 m³/h
    assert r["edges"]["tds"]["c"]["o2"] == pytest.approx(7.0)


# ---- LHO course-sheet takeaways: TGP check, degasser G/L, jet CO₂ stripping ----

def test_tgp_helper_anchored_at_air_saturation():
    from ras_engine.jet import Water, tgp_pct
    for t in (8.0, 14.0, 20.0, 26.67):
        w = Water(t, 0.0)
        assert tgp_pct(w.O_sat, t) == pytest.approx(100.0, abs=0.2)
    # supersaturated oxygen pushes TGP past the 103% risk line
    w = Water(14.0, 0.0)
    assert tgp_pct(13.0, 14.0) > 103.0
    assert tgp_pct(None, 14.0) is None and tgp_pct(8.0, 50.0) is None


def test_solve_warns_on_tank_tgp_above_103():
    # tank loads off, a cone lifting O₂ far above saturation: the fish water is supersaturated
    d = loop(extra_nodes=[node("cone", "cone", {"o2_target_mgl": 20.0, "transfer_eff_pct": 90})],
             tank_spec={"tan_kgd": 0})
    r = solve(d)
    assert any("total gas pressure" in w and "103%" in w for w in r["warnings"])
    assert r["nodes"]["tank"]["tgp_pct"] > 103.0
    # and the calm case stays silent
    r2 = solve(loop())
    assert not any("gas pressure" in w for w in r2["warnings"])


def test_degasser_reports_stripping_gas_from_gl_ratio():
    d = loop(extra_nodes=[node("deg", "degasser", {"co2_removal_pct": 70, "gl_ratio_pct": 300})])
    r = solve(d)
    assert r["nodes"]["deg"]["gas_m3h"] == pytest.approx(100 * 300 / 100)  # pump sets 100 m³/h


def test_jet_strips_co2_when_told_to():
    # single pass: pump water carries 10 mg/L CO₂ (typed on the pump node), the jet strips 44%
    src = {"flow_m3h": 100, "tss": 0, "tan": 0, "co2": 10.0, "o2": 0, "no3": 0, "temp": 12}
    nodes = [node("mk", "makeup", src), node("jet", "jet_rect", {"co2_removal_pct": 44}),
             node("d", "discharge")]
    r = solve({"nodes": nodes, "edges": [pipe("mk", "out", "jet"), pipe("jet", "out", "d")]})
    assert r["edges"]["jet.out>d"]["c"]["co2"] == pytest.approx(10 * (1 - 0.44), abs=0.05)
    assert r["nodes"]["jet"]["co2_removed_kgd"] == pytest.approx(10 * 0.44 * 100 * 24 / 1000)
    # default off: nothing stripped
    r2 = solve({"nodes": [node("mk", "makeup", src), node("jet", "jet_rect", {}), node("d", "discharge")],
                "edges": [pipe("mk", "out", "jet"), pipe("jet", "out", "d")]})
    assert r2["edges"]["jet.out>d"]["c"]["co2"] == pytest.approx(10)
    assert not r2["nodes"]["jet"].get("co2_removed_kgd")
