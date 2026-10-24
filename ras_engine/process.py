"""Process diagram of one RAS unit: equipment nodes joined by pipes, solved as a steady-state
mass balance.

Flow: every node passes on what enters it (water in = water out). Each outlet takes a set m³/h,
a share (%) of the inflow, or "rest". Pumps set their own outflow; the difference with what reaches
their inlet is reported as make-up needed or overflow. Make-up nodes add water at a set flow.

Components (mg/L = g/m³): TSS, TAN (as N), CO2, O2, NO3 (as N), and the water temperature (°C),
which mixes like a concentration (flow-weighted). Equipment changes them according to its
specification. Loops are solved by repeating passes until concentrations settle.

Heat: 1 m³/h of water warmed by 1 °C takes 1.163 kW. Pumps put their power into the water, tanks
can gain or lose heat, chillers cool to a set point, heat exchangers pass heat between two circuits
that do not mix (process side in/out, utility side in2/out2).
"""
from collections import defaultdict

from .formulas import o2_saturation
from .jet import tgp_pct

COMPONENTS = ("tss", "tan", "co2", "o2", "no3", "temp")
COMPONENT_LABEL = {"tss": "TSS", "tan": "TAN", "co2": "CO₂", "o2": "O₂", "no3": "NO₃-N", "temp": "Temperature"}
COMPONENT_UNIT = {c: "mg/L" for c in COMPONENTS} | {"temp": "°C"}
CP_KW = 1.163  # kW to warm 1 m³/h of water by 1 °C
# one tolerance for "is a concentration at its limit": the auto-O₂ loop stops chasing gaps below it,
# so a tank 0.003 mg/L under its minimum is AT the limit, not outside it (kills the "O₂ 6.00 < 6" alarm)
LIMIT_TOL = 0.005
DEFAULT_LIMITS = {"tss": {"max": 10.5}, "tan": {"max": 1.0}, "co2": {"max": 15.0},
                  "o2": {"min": 6.0}, "no3": {"max": 100.0}}


def f(key, label, unit, default):
    return {"key": key, "label": label, "unit": unit, "default": default}


# type -> label, standard category (for CAPEX), inputs?, outlet ports, spec fields
# equipment without a drain of its own gets one; closed (0 m³/h) until the user opens it
DRAIN_CLOSED = {"out": {"mode": "rest"}, "drain": {"mode": "m3h", "value": 0}}

TANK_LOAD_SPEC = [f("volume_m3", "Water volume", "m³", 50.0),
                  f("biomass_pct", "Share of the unit's fish (blank = the rest, by tank volume)", "%", None),
                  f("feed_kgd", "Feed (blank = follows the fish: unit's feed × share)", "kg/day", None),
                  f("tan_kgd", "TAN production (0 = from the feed)", "kg/day", 0.0),
                  f("co2_kgd", "CO₂ production (0 = from the feed)", "kg/day", 0.0),
                  f("tss_kgd", "TSS production (0 = from the feed)", "kg/day", 0.0),
                  f("o2_kgd", "O₂ consumption (0 = from the feed)", "kg/day", 0.0),
                  f("heat_kw", "Heat gain (fish, building; negative = loss)", "kW", 0.0)]


# two circuits that do not mix: process water in -> out, utility (chilled/heating water) in2 -> out2.
# Effectiveness model: Q = ε · C_min · (T_utility,in − T_process,in), C = flow × 1.163 kW/K.
def _jet_type(label):
    return {
        "label": label, "category": "oxygen", "inputs": True, "jet": True,
        "outlets": [("out", "Out"), ("drain", "Drain")], "default_outlets": DRAIN_CLOSED,
        "spec": [f("o2_target_mgl", "O₂ at outlet (blank = auto: what the tanks it feeds need)", "mg/L", None),
                 f("head_m", "Head (water in the box above the tank water)", "m", 1.0),
                 f("compartments", "Gas compartments", "", 4.0),
                 f("plate_below_m", "Plate below the tank water level", "m", 0.10),
                 f("o2_purity_pct", "Feed gas O₂", "%", 95.0),
                 f("E", "Measured compartment efficiency (blank = calibrated)", "0–1", None),
                 f("co2_removal_pct", "CO₂ stripped in the same unit (LHO course: ~44% with a high gas flow)", "%", 0.0)],
    }


def _hx_type(label, eff):
    return {
        "label": label, "category": "heat_exchanger", "inputs": True, "sides": True,
        "outlets": [("out", "Process out"), ("out2", "Utility out")],
        "default_outlets": {"out": {"mode": "rest"}, "out2": {"mode": "rest"}},
        "spec": [f("effectiveness_pct", "Effectiveness ε", "%", eff),
                 f("utility_temp_c", "Utility inlet temperature (used when the utility side has no pipe)", "°C", 6.0),
                 f("utility_flow_m3h", "Utility flow (used when the utility side has no pipe)", "m³/h", 20.0),
                 f("area_m2", "Heat transfer area", "m²", 0.0)],
    }

NODE_TYPES = {
    # hatchery incubator cabinet: trays stacked, water in at the top, out at the bottom
    "incubator": {
        "label": "Hatchery incubator", "category": "incubator", "inputs": True,
        "outlets": [("out", "Out")], "default_outlets": {"out": {"mode": "rest"}},
        "spec": [f("trays", "Trays", "trays", 16.0),
                 f("o2_kgd", "O₂ consumption (eggs)", "kg/day", 0.0),
                 f("tan_kgd", "TAN production", "kg/day", 0.0),
                 f("co2_kgd", "CO₂ production", "kg/day", 0.0)],
    },
    # fish tanks: one or two inlets (data.inlets), and one of four outlet arrangements
    "tank": {
        "label": "Fish tank (bottom + side)", "category": "tank", "inputs": True, "tank": True,
        "outlets": [("bottom", "Bottom drain"), ("side", "Side outlet")],
        "default_outlets": {"bottom": {"mode": "pct", "value": 15}, "side": {"mode": "rest"}},
        "spec": TANK_LOAD_SPEC + [f("bottom_tss_pct", "Share of TSS leaving by the bottom drain", "%", 80.0)],
    },
    "tank_bottom": {
        "label": "Fish tank (bottom only)", "category": "tank", "inputs": True, "tank": True,
        "outlets": [("bottom", "Bottom drain")],
        "default_outlets": {"bottom": {"mode": "rest"}},
        "spec": TANK_LOAD_SPEC,
    },
    "tank_center": {
        "label": "Fish tank (bottom + centre pipe)", "category": "tank", "inputs": True, "tank": True,
        "outlets": [("bottom", "Bottom drain"), ("center", "Centre pipe")],
        "default_outlets": {"bottom": {"mode": "pct", "value": 15}, "center": {"mode": "rest"}},
        "spec": TANK_LOAD_SPEC + [f("bottom_tss_pct", "Share of TSS leaving by the bottom drain", "%", 80.0)],
    },
    "tank_3": {
        "label": "Fish tank (bottom + centre + side)", "category": "tank", "inputs": True, "tank": True,
        "outlets": [("bottom", "Bottom drain"), ("center", "Centre pipe"), ("side", "Side outlet")],
        "default_outlets": {"bottom": {"mode": "pct", "value": 10}, "center": {"mode": "pct", "value": 20},
                            "side": {"mode": "rest"}},
        "spec": TANK_LOAD_SPEC + [f("bottom_tss_pct", "Share of TSS leaving by the bottom drain", "%", 70.0),
                                  f("center_tss_pct", "Share of TSS leaving by the centre pipe", "%", 20.0)],
    },
    # raceway: a long rectangular channel, plug flow — water in at one end, out at the other. No bottom-drain
    # share: everything the fish make leaves with the outflow, so settle the solids downstream, not in the tank
    "raceway": {
        "label": "Raceway (in one end, out the other)", "category": "tank", "inputs": True, "tank": True,
        "outlets": [("out", "Out")], "default_outlets": {"out": {"mode": "rest"}},
        "spec": TANK_LOAD_SPEC,
    },
    "rfs": {
        "label": "Radial flow settler", "category": "rfs", "inputs": True,
        "outlets": [("out", "Clear water"), ("sludge", "Sludge")],
        "default_outlets": {"sludge": {"mode": "pct", "value": 2}, "out": {"mode": "rest"}},
        "spec": [f("tss_removal_pct", "TSS removal", "%", 50.0),
                 f("residence_min", "Residence time (sizes the volume from the flow through it)", "min", 5.0)],
    },
    "drum_filter": {
        "label": "Drum filter", "category": "drum_filter", "inputs": True,
        "outlets": [("out", "Filtered water"), ("sludge", "Backwash")],
        "default_outlets": {"sludge": {"mode": "pct", "value": 1}, "out": {"mode": "rest"}},
        "spec": [f("tss_removal_pct", "TSS removal", "%", 60.0), f("mesh_um", "Screen", "µm", 60.0)],
    },
    "biofilter": {
        "label": "Biofilter (MBBR)", "category": "biofilter", "inputs": True,
        "outlets": [("out", "Out"), ("drain", "Drain")], "default_outlets": DRAIN_CLOSED,
        "spec": [f("tan_removal_pct", "TAN removal", "%", 50.0),
                 f("o2_per_tan", "O₂ used per g TAN nitrified", "g/g", 4.57),
                 f("co2_per_tan", "CO₂ released per g TAN nitrified", "g/g", 5.85),
                 f("media_m3", "Media volume (0 = a share of the media required on step 04)", "m³", 0.0),
                 f("share_pct", "Share of the required media (blank = equal split)", "%", None),
                 f("fill_pct", "Media fill ratio (blank = step 04 assumption)", "%", None)],
    },
    "fbbr": {
        "label": "Fixed bed bioreactor (FBBR)", "category": "biofilter", "inputs": True,
        "outlets": [("out", "Out"), ("drain", "Drain")], "default_outlets": DRAIN_CLOSED,
        "spec": [f("tan_removal_pct", "TAN removal", "%", 60.0),
                 f("tss_removal_pct", "TSS captured by the bed (backwashed out)", "%", 30.0),
                 f("o2_per_tan", "O₂ used per g TAN nitrified", "g/g", 4.57),
                 f("co2_per_tan", "CO₂ released per g TAN nitrified", "g/g", 5.85),
                 f("media_m3", "Fixed media volume (0 = a share of the media required on step 04)", "m³", 0.0),
                 f("share_pct", "Share of the required media (blank = equal split)", "%", None),
                 f("fill_pct", "Media fill ratio (blank = step 04 assumption)", "%", None)],
    },
    "degasser": {
        "label": "CO₂ degasser", "category": "degasser", "inputs": True,
        "outlets": [("out", "Out"), ("drain", "Drain")], "default_outlets": DRAIN_CLOSED,
        "spec": [f("co2_removal_pct", "CO₂ removal", "%", 70.0),
                 f("gl_ratio_pct", "Stripping gas per water flow (G/L)", "%", 300.0),
                 f("o2_out_mgl", "O₂ after degasser (blank = unchanged)", "mg/L", None)],
    },
    "cone": {
        "label": "Oxygen cone", "category": "oxygen", "inputs": True,
        "outlets": [("out", "Out"), ("drain", "Drain")], "default_outlets": DRAIN_CLOSED,
        "spec": [f("o2_target_mgl", "O₂ at outlet (blank = auto: what the tanks it feeds need)", "mg/L", None),
                 f("transfer_eff_pct", "Oxygen transfer efficiency", "%", 90.0)],
    },
    # jet platform oxygenators (low head): water falls through a nozzle plate into a pure-O₂ cushion. The O₂ added
    # is set by the target; how much oxygen must be fed follows from the jet engine (ras_engine.jet) at the
    # solved flow, inlet O₂ and temperature
    "jet_rect": _jet_type("Jet oxygenator (rectangular)"),
    "jet_round": _jet_type("Jet oxygenator (round)"),
    # water passes through the contactor; the generator only supplies gas (a gas line, not a pipe)
    "ozone": {
        "label": "Ozone contactor", "category": "other", "inputs": True,
        "outlets": [("out", "Out"), ("drain", "Drain")], "default_outlets": DRAIN_CLOSED,
        "spec": [f("dose_g_h", "Ozone dose (blank = from connected generators)", "g O₃/h", None),
                 f("tss_removal_pct", "TSS removal", "%", 0.0)],
    },
    "ozone_generator": {
        "label": "Ozone generator", "category": "ozone", "inputs": False, "gas": True,
        "outlets": [], "default_outlets": {},
        "spec": [f("output_g_h", "Ozone output (0 = from the feed: plan's ozone per kg feed)", "g O₃/h", 0.0)],
    },
    # water in -> out, cooled to the set point as far as the cooling capacity allows
    "chiller": {
        "label": "Chiller", "category": "chiller", "inputs": True,
        "outlets": [("out", "Out")], "default_outlets": {"out": {"mode": "rest"}},
        "spec": [f("setpoint_c", "Outlet temperature set point", "°C", 12.0),
                 f("capacity_kw", "Cooling capacity (blank = unlimited)", "kW", None),
                 f("cop", "COP (cooling kW per electric kW)", "", 3.0)],
    },
    "heater": {
        "label": "Heater / heat pump", "category": "heater", "inputs": True,
        "outlets": [("out", "Out")], "default_outlets": {"out": {"mode": "rest"}},
        "spec": [f("setpoint_c", "Outlet temperature set point", "°C", 12.0),
                 f("capacity_kw", "Heating capacity (blank = unlimited)", "kW", None),
                 # heat pump 3-5; an electric immersion heater is COP = 1 (last resort)
                 f("cop", "COP (heat kW per electric kW)", "", 3.0)],
    },
    "hx_plate": _hx_type("Heat exchanger (plate)", 85.0),
    "hx_tube": _hx_type("Heat exchanger (shell & tube)", 60.0),
    "uv": {
        "label": "UV unit", "category": "uv", "inputs": True,
        "outlets": [("out", "Out"), ("drain", "Drain")], "default_outlets": DRAIN_CLOSED,
        "spec": [f("dose_mj_cm2", "UV dose", "mJ/cm²", 30.0)],
    },
    # a running pump sets its own flow; on a frequency drive Q = Q_nom·f/f_nom and P = P_nom·(f/f_nom)³
    "pump": {
        "label": "Pump", "category": "pump", "inputs": True,
        "outlets": [("out", "Out")], "default_outlets": {"out": {"mode": "rest"}},
        "spec": [f("active", "Running (no = standby)", "", True),
                 f("flow_m3h", "Rated flow (at nominal frequency)", "m³/h", 100.0),
                 f("vfd", "Frequency drive (VFD)", "", False),
                 f("nominal_hz", "Nominal frequency", "Hz", 50.0),
                 f("frequency_hz", "Operating frequency (VFD)", "Hz", 50.0),
                 f("power_kw", "Rated power", "kW", 0.0)],
    },
    # raw water storage (e.g. river water before it goes to the farm): inlets left, outlets right,
    # settled solids leave by the drain; only outlets with a pipe take water
    "reservoir": {
        "label": "Water reservoir", "category": "reservoir", "inputs": True, "connected_only": True,
        "outlets": [("out", "Outlet 1"), ("out2", "Outlet 2"), ("drain", "Drain / sludge")],
        "default_outlets": {"out": {"mode": "rest"}, "out2": {"mode": "rest"}, "drain": {"mode": "m3h", "value": 0}},
        "spec": [f("volume_m3", "Volume", "m³", 500.0),
                 f("tss_removal_pct", "TSS settling (to the drain)", "%", 0.0)],
    },
    # three inlets left, three outlets right, drain below; only outlets with a pipe take water
    "sump": {
        "label": "Sump", "category": "sump", "inputs": True, "connected_only": True,
        "outlets": [("out", "Outlet 1"), ("out2", "Outlet 2"), ("out3", "Outlet 3"), ("drain", "Drain")],
        "default_outlets": {"out": {"mode": "rest"}, "out2": {"mode": "rest"}, "out3": {"mode": "rest"},
                            "drain": {"mode": "m3h", "value": 0}},
        "spec": [f("volume_m3", "Volume", "m³", 10.0)],
    },
    "sand_filter": {
        "label": "Sand filter", "category": "sand_filter", "inputs": True,
        "outlets": [("out", "Filtered"), ("backwash", "Backwash")],
        "default_outlets": {"backwash": {"mode": "pct", "value": 2}, "out": {"mode": "rest"}},
        "spec": [f("tss_removal_pct", "TSS removal", "%", 70.0), f("area_m2", "Filter area", "m²", 0.0)],
    },
    # four ports (top/right/bottom/left); each is an inlet or an outlet (data.roles), so a header
    # can feed tank 1 from one port and carry the rest on from another
    "junction": {
        "label": "Connection point", "category": None, "inputs": True,
        "outlets": [], "default_outlets": {}, "spec": [],
    },
    # piping, not equipment: no CAPEX line
    "splitter": {
        "label": "Splitter", "category": None, "inputs": True,
        "outlets": [("a", "Outlet A"), ("b", "Outlet B"), ("c", "Outlet C")],
        "default_outlets": {"a": {"mode": "pct", "value": 50}, "b": {"mode": "rest"}, "c": {"mode": "pct", "value": 0}},
        "spec": [],
    },
    "makeup": {
        "label": "Make-up water", "category": None, "inputs": False,
        "outlets": [("out", "Out")], "default_outlets": {"out": {"mode": "rest"}},
        "spec": [f("flow_m3h", "Flow", "m³/h", 1.0), f("tss", "TSS", "mg/L", 0.0), f("tan", "TAN", "mg/L", 0.0),
                 f("co2", "CO₂", "mg/L", 2.0), f("o2", "O₂", "mg/L", 9.0), f("no3", "NO₃-N", "mg/L", 20.0),
                 f("temp", "Temperature", "°C", 12.0)],
    },
    "discharge": {
        "label": "Discharge", "category": None, "inputs": True, "outlets": [], "default_outlets": {}, "spec": [],
    },
    "other": {
        "label": "Other treatment", "category": "other", "inputs": True,
        "outlets": [("out", "Out"), ("drain", "Drain")], "default_outlets": DRAIN_CLOSED,
        "spec": [f("tss_removal_pct", "TSS removal", "%", 0.0), f("tan_removal_pct", "TAN removal", "%", 0.0),
                 f("co2_removal_pct", "CO₂ removal", "%", 0.0), f("no3_removal_pct", "NO₃ removal", "%", 0.0)],
    },
}


JUNCTION_PORTS = (("top", "Top"), ("right", "Right"), ("bottom", "Bottom"), ("left", "Left"))
JUNCTION_DEFAULT_ROLES = {"top": "in", "right": "out", "bottom": "out", "left": "in"}


def junction_roles(node):
    return {**JUNCTION_DEFAULT_ROLES, **(node.get("data", {}).get("roles") or {})}


def node_outlets(node):
    """[(port, label)] — fixed per type, except a connection point whose outlets are its 'out' ports."""
    if node["type"] == "junction":
        roles = junction_roles(node)
        return [(p, label) for p, label in JUNCTION_PORTS if roles[p] == "out"]
    return NODE_TYPES[node["type"]]["outlets"]


def spec_value(node, key):
    spec = node.get("data", {}).get("spec", {})
    default = next(s["default"] for s in NODE_TYPES[node["type"]]["spec"] if s["key"] == key)
    if key in spec and spec[key] not in ("", None):
        if isinstance(default, bool):
            v = spec[key]
            return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
        return float(spec[key])
    return default


def pump_state(node):
    """Flow, frequency and power of a pump from its specification (0 when on standby)."""
    if not spec_value(node, "active"):
        return {"running": False, "flow_m3h": 0.0, "frequency_hz": 0.0, "power_kw": 0.0}
    rated, fn = spec_value(node, "flow_m3h"), spec_value(node, "nominal_hz") or 50.0
    f_op = spec_value(node, "frequency_hz") if spec_value(node, "vfd") else fn
    ratio = max(f_op, 0.0) / fn
    return {"running": True, "flow_m3h": rated * ratio, "frequency_hz": f_op,
            "power_kw": spec_value(node, "power_kw") * ratio ** 3, "vfd": spec_value(node, "vfd")}


def outlet_specs(node):
    t = NODE_TYPES[node["type"]]
    saved = node.get("data", {}).get("outlets", {})
    # a connection point's outlets default to 'rest', i.e. an equal split
    fallback = {"mode": "rest"} if node["type"] == "junction" else {"mode": "pct", "value": 0}
    return {port: {**t["default_outlets"].get(port, fallback), **saved.get(port, {})}
            for port, _ in node_outlets(node)}


class DiagramError(Exception):
    pass


def _split_flow(node, q_in, warnings, connected=None):
    """Outlet port -> m³/h, from the outlet specs (m³/h, % of inflow, or rest).
    On a connection point only ports with a pipe take water (`connected`)."""
    specs = outlet_specs(node)
    if connected is not None and (node["type"] == "junction" or NODE_TYPES[node["type"]].get("connected_only")):
        specs = {p: s for p, s in specs.items() if p in connected}
    flows, rest = {}, []
    for port, s in specs.items():
        if s.get("mode") == "m3h":
            flows[port] = max(float(s.get("value") or 0), 0.0)
        elif s.get("mode") == "pct":
            flows[port] = q_in * max(float(s.get("value") or 0), 0.0) / 100
        else:
            rest.append(port)
    used = sum(flows.values())
    left = q_in - used
    if rest:
        for port in rest:
            flows[port] = max(left, 0.0) / len(rest)
    if left < -1e-6:
        warnings.append(f"outlets take {used:.1f} m³/h but only {q_in:.1f} m³/h comes in")
    elif not rest and abs(left) > 1e-6 and specs:
        warnings.append(f"outlets take {used:.1f} m³/h of {q_in:.1f} m³/h coming in — set one outlet to 'rest'")
    return flows


def solve(diagram: dict, max_iter: int = 2000, tol: float = 1e-7) -> dict:
    nodes = {n["id"]: dict(n) for n in diagram.get("nodes", []) if n.get("type") in NODE_TYPES}
    edges = [dict(e) for e in diagram.get("edges", []) if e.get("source") in nodes and e.get("target") in nodes]
    limits = {**DEFAULT_LIMITS, **(diagram.get("limits") or {})}
    node_warn = defaultdict(list)
    # gas lines (ozone generator -> contactor) carry no water: take them out of the water balance
    gas_res = {}
    gas_edges = [e for e in edges if NODE_TYPES[nodes[e["source"]]["type"]].get("gas")]
    edges = [e for e in edges if e not in gas_edges]
    for nid, n in nodes.items():
        if n["type"] == "ozone_generator":
            out = spec_value(n, "output_g_h")
            if not out:  # from the feed of the day, shared over the generators
                fish = (diagram.get("basis") or {}).get("fish") or {}
                gens = sum(1 for x in nodes.values() if x["type"] == "ozone_generator" and not spec_value(x, "output_g_h"))
                out = (fish.get("feed_kgd") or 0.0) * (fish.get("ozone_per_feed") or 0.0) * 1000 / 24 / max(gens, 1)
            feeds = [e for e in gas_edges if e["source"] == nid]
            gas_res[nid] = {"output_g_h": out, "output_kgd": out * 24 / 1000, "feeds": len(feeds)}
            if not feeds:
                node_warn[nid].append("not connected to an ozone contactor")
            for e in feeds:
                target = nodes[e["target"]]
                if target["type"] != "ozone":
                    node_warn[nid].append("a gas line goes to something other than an ozone contactor; it is ignored")
                    continue
                target["_gas_g_h"] = target.get("_gas_g_h", 0.0) + out / len(feeds)
    # connection points: older diagrams used 'in'/'out' handles; pipes must leave by an 'out' port
    for e in edges:
        src = nodes[e["source"]]
        if src["type"] == "junction" and (e.get("sourceHandle") or "out") == "out":
            e["sourceHandle"] = "right"
    kept = []
    for e in edges:
        src = nodes[e["source"]]
        if src["type"] == "junction" and junction_roles(src).get(e.get("sourceHandle")) != "out":
            node_warn[e["source"]].append(f"a pipe leaves by the '{e.get('sourceHandle')}' port, which is an inlet; it is ignored")
            continue
        kept.append(e)
    edges = kept
    out_edges, in_edges = defaultdict(list), defaultdict(list)
    for e in edges:
        out_edges[(e["source"], e.get("sourceHandle") or "out")].append(e)
        in_edges[e["target"]].append(e)
    # --- order: pumps and make-up are sources, so cut the edges that enter pumps.
    # A heat exchanger's two sides are separate circuits (in -> out, in2 -> out2): each side is ordered on its
    # own, so e.g. make-up -> exchanger -> tanks -> drain -> exchanger's other side is not a loop.
    sided = {nid for nid, n in nodes.items() if NODE_TYPES[n["type"]].get("sides")}

    def unit_in(e):
        nid = e["target"]
        return (nid, 2 if nid in sided and e.get("targetHandle") == "in2" else 1)

    def unit_out(e):
        nid = e["source"]
        return (nid, 2 if nid in sided and e.get("sourceHandle") == "out2" else 1)

    units = [(nid, 1) for nid in nodes] + [(nid, 2) for nid in sided]
    indeg = dict.fromkeys(units, 0)
    for e in edges:
        if nodes[e["target"]]["type"] != "pump":
            indeg[unit_in(e)] += 1
    uorder, ready = [], [u for u, d in indeg.items() if d == 0]
    while ready:
        u = ready.pop(0)
        uorder.append(u)
        for e in edges:
            if unit_out(e) == u and nodes[e["target"]]["type"] != "pump":
                indeg[unit_in(e)] -= 1
                if indeg[unit_in(e)] == 0:
                    ready.append(unit_in(e))
    if len(uorder) < len(units):
        stuck = list(dict.fromkeys(nodes[nid].get("data", {}).get("name") or nodes[nid]["type"]
                                   for nid, _side in units if (nid, _side) not in uorder))
        raise DiagramError("Water circulates without passing a pump: " + ", ".join(stuck)
                           + ". Every loop needs a pump to set its flow.")
    # balance passes visit an exchanger at each of its sides, so both circuits see each other's latest water
    order = [nid for nid, _side in uorder]

    for nid, ld in _tank_loads(nodes, diagram.get("basis")).items():
        nodes[nid]["_loads"] = ld

    # --- flows (one pass in order)
    pumps = {nid: pump_state(n) for nid, n in nodes.items() if n["type"] == "pump"}
    standby = {nid for nid, s in pumps.items() if not s["running"]}
    q_edge, q_in, q_out_port = {}, defaultdict(float), {}
    for nid, side in uorder:
        n = nodes[nid]
        t = n["type"]
        if t != "pump":
            q_in[nid] = sum(q_edge.get(e["id"], 0.0) for e in in_edges[nid])
        if t == "pump":
            flows = {"out": pumps[nid]["flow_m3h"]}
        elif nid in sided:  # each circuit leaves by its own outlet, set when that side is reached
            port, handle = ("out2", "in2") if side == 2 else ("out", None)
            flows = {port: sum(q_edge.get(e["id"], 0.0) for e in in_edges[nid]
                               if (e.get("targetHandle") == "in2") == (handle == "in2"))}
        elif t == "makeup":
            flows = {"out": spec_value(n, "flow_m3h")}
        else:
            # an outlet that only feeds standby pumps is closed: it takes no water
            connected = {port for (src, port), es in out_edges.items()
                         if src == nid and not all(e["target"] in standby for e in es)}
            flows = _split_flow(n, q_in[nid], node_warn[nid], connected)
            if t == "junction" and q_in[nid] > 1e-9 and not connected:
                node_warn[nid].append("water comes in but no outlet port has a pipe")
        for port, q in flows.items():
            q_out_port[(nid, port)] = q
            targets = out_edges.get((nid, port), [])
            if len(targets) > 1 and t != "splitter":  # a splitter outlet is a manifold by design
                node_warn[nid].append(f"outlet '{port}' feeds {len(targets)} pipes; flow is split equally")
            for e in targets:
                q_edge[e["id"]] = q / len(targets)
            if q > 1e-9 and not targets and t not in ("discharge",):
                node_warn[nid].append(f"outlet '{port}' carries {q:.1f} m³/h but is not connected")
    pump_balance = {}
    for nid, n in nodes.items():
        if n["type"] == "pump":
            q_in[nid] = sum(q_edge.get(e["id"], 0.0) for e in in_edges[nid])
            diff = q_in[nid] - q_out_port[(nid, "out")]
            pump_balance[nid] = diff
            tol_q = max(0.01, 1e-5 * q_out_port[(nid, "out")])  # ignore rounding crumbs
            if nid in standby:
                if q_in[nid] > tol_q:
                    node_warn[nid].append(f"standby pump, but {q_in[nid]:.1f} m³/h still reaches it")
                continue
            if diff < -tol_q:
                node_warn[nid].append(f"{-diff:.1f} m³/h more is pumped than reaches the pump: add make-up water")
            elif diff > tol_q:
                node_warn[nid].append(f"{diff:.1f} m³/h more reaches the pump than it pumps: overflow")

    # --- concentrations: the pump outlets are the "tear streams" of the loops. One pass through the
    # network maps pump-outlet concentrations to new ones; Wegstein acceleration finds the fixed point
    # (plain repetition would need thousands of passes when little water is exchanged).
    c_edge = {e["id"]: dict.fromkeys(COMPONENTS, 0.0) for e in edges}
    node_res = {}

    def one_pass(pump_c):
        for nid in order:
            n, t = nodes[nid], nodes[nid]["type"]
            res = {"q_in": q_in[nid]}
            if t == "pump":
                m = {c: pump_c[nid][c] * q_out_port[(nid, "out")] for c in COMPONENTS}
                q = q_out_port[(nid, "out")]
                if q > 1e-12:  # the pump's power ends up as heat in the water
                    m["temp"] += pumps[nid]["power_kw"] / CP_KW
            elif t == "makeup":
                q = spec_value(n, "flow_m3h")
                m = {c: spec_value(n, c) * q for c in COMPONENTS}
            else:
                q = q_in[nid]
                m = {c: sum(q_edge.get(e["id"], 0.0) * c_edge[e["id"]][c] for e in in_edges[nid]) for c in COMPONENTS}
            if NODE_TYPES[t].get("sides"):
                side = {"out": [e for e in in_edges[nid] if e.get("targetHandle") != "in2"],
                        "out2": [e for e in in_edges[nid] if e.get("targetHandle") == "in2"]}
                qs = {p: sum(q_edge.get(e["id"], 0.0) for e in es) for p, es in side.items()}
                ms = {p: {c: sum(q_edge.get(e["id"], 0.0) * c_edge[e["id"]][c] for e in es) for c in COMPONENTS}
                      for p, es in side.items()}
                res["c_in"] = {c: (ms["out"][c] / qs["out"] if qs["out"] > 1e-12 else 0.0) for c in COMPONENTS}
                res["q_process"] = qs["out"]
                port_m = _exchange(n, qs, ms, res)
            else:
                res["c_in"] = {c: (m[c] / q if q > 1e-12 else 0.0) for c in COMPONENTS}
                n["_flows"] = {port: q_out_port.get((nid, port), 0.0) for port, _ in node_outlets(n)}
                port_m = _treat(n, t, q, m, res)
            flows = {port: q_out_port.get((nid, port), 0.0) for port, _ in node_outlets(n)}
            for port, qp in flows.items():
                mp = port_m.get(port) if port_m else None
                for e in out_edges.get((nid, port), []):
                    share = 1 / len(out_edges[(nid, port)])
                    qe = qp * share
                    c_edge[e["id"]] = {c: ((mp[c] if mp else m[c] * (qp / q if q > 1e-12 else 0)) * share / qe
                                           if qe > 1e-12 else 0.0) for c in COMPONENTS}
            node_res[nid] = res
        new_c = {}
        for nid in pump_c:  # pump outlet = what reaches its inlet (a shortfall counts as clean water)
            qi, qo = q_in[nid], q_out_port[(nid, "out")]
            new_c[nid] = {}
            for c in COMPONENTS:
                mass = sum(q_edge.get(e["id"], 0.0) * c_edge[e["id"]][c] for e in in_edges[nid])
                # overflow: the pump takes the mixed water; a shortfall dilutes, but does not cool
                denom = qi if qi > qo or (c == "temp" and qi > 1e-12) else qo
                new_c[nid][c] = mass / denom if denom > 1e-12 else 0.0
        return new_c

    keys = [(nid, c) for nid, n in nodes.items() if n["type"] == "pump" for c in COMPONENTS]
    as_nested = lambda v: {nid: {c: v[(nid, c)] for c in COMPONENTS}  # noqa: E731
                           for nid in {k[0] for k in keys}}
    flat = lambda nested: {(nid, c): nested[nid][c] for nid in nested for c in COMPONENTS}  # noqa: E731

    def settle(x=None):
        """Wegstein on the pump outlets; returns (converged, iterations, state)."""
        if x is None:
            # first guess: clean water at the make-up temperature (a loop without make-up keeps this temperature)
            makeups = [spec_value(n, "temp") for n in nodes.values() if n["type"] == "makeup"]
            t0 = sum(makeups) / len(makeups) if makeups else 12.0
            x = {k: (t0 if k[1] == "temp" else 0.0) for k in keys}
        gx = flat(one_pass(as_nested(x)))
        x_prev, g_prev = x, gx
        x = gx
        converged = not keys
        it = 0
        for it in range(1, max_iter):
            gx = flat(one_pass(as_nested(x)))
            err = max((abs(gx[k] - x[k]) / max(1.0, abs(gx[k])) for k in keys), default=0.0)
            if err < tol:
                converged = True
                break
            x_next = {}
            for k in keys:
                dx = x[k] - x_prev[k]
                s = (gx[k] - g_prev[k]) / dx if abs(dx) > 1e-15 else 0.0
                qw = s / (s - 1) if abs(s - 1) > 1e-12 else 0.0
                qw = min(max(qw, -1e6), 0.0)
                v = qw * x[k] + (1 - qw) * gx[k]
                x_next[k] = v if k[1] == "temp" else max(v, 0.0)
            x_prev, g_prev, x = x, gx, x_next
            if any(v > 1e9 for v in x.values()):
                break
        return converged, it, x

    # oxygenators left on "auto" (no outlet O₂ typed) add what the tanks they feed need: the fish's O₂ use at
    # the tank's flow on top of the tank water's minimum. Water from elsewhere can dilute it on the way, so the
    # set point is raised until every one of those tanks keeps its minimum.
    o2_min = (limits.get("o2") or {}).get("min") or 0.0
    auto = [nid for nid, n in nodes.items() if (n["type"] == "cone" or NODE_TYPES[n["type"]].get("jet"))
            and spec_value(n, "o2_target_mgl") is None]
    fed = {nid: _tanks_fed(nid, nodes, edges) for nid in auto}

    def tank_o2(tid):
        outs = [e for e in edges if e["source"] == tid and q_edge.get(e["id"], 0.0) > 1e-9]
        water = [e for e in outs if (e.get("sourceHandle") or "out") != "bottom"] or outs
        q = sum(q_edge[e["id"]] for e in water)
        return sum(q_edge[e["id"]] * c_edge[e["id"]]["o2"] for e in water) / q if q > 1e-9 else None

    for nid in auto:
        need = [o2_min + (nodes[t].get("_loads") or {}).get("o2_kgd", 0.0) * 1000 / 24 / q_in[t]
                for t in fed[nid] if q_in.get(t, 0.0) > 1e-9]
        set_pt = max(need) if need else o2_min
        if set_pt > 60.0:  # supersaturated inlet water is a gas-bubble trauma risk, not a design tool
            node_warn[nid].append("auto O₂ set point capped at 60 mg/L (supersaturation/GBT risk) — "
                                  "increase flow or split the load")
            set_pt = 60.0
        nodes[nid]["_o2_set"] = set_pt
    converged, it, x = settle()
    for _ in range(30 if auto else 0):
        moved = False
        for nid in auto:
            short = [o2_min - v for v in (tank_o2(t) for t in fed[nid]) if v is not None]
            gap = max(short, default=0.0)
            if gap > LIMIT_TOL and nodes[nid]["_o2_set"] < 60:
                nodes[nid]["_o2_set"] = min(nodes[nid]["_o2_set"] + gap, 60.0)
                moved = True
        if not moved:
            break
        converged, it, x = settle(x)
    for nid in auto:
        nodes[nid]["_tanks_fed"] = len(fed[nid])
        if not fed[nid]:
            node_warn[nid].append("set to auto but feeds no fish tank: it only lifts O₂ to the tank minimum")
    one_pass(as_nested(x))  # leave edges and node results at the final state
    for nid, plan in _media_plan(nodes, diagram.get("basis")).items():
        node_res.setdefault(nid, {}).update(plan)
    for nid, n in nodes.items():  # jet oxygenators: efficiency and oxygen fed at the solved state
        if NODE_TYPES[n["type"]].get("jet") and nid in node_res:
            _jet_result(n, node_res[nid], node_warn[nid])

    # --- checks
    # tank water (every tank type): the water leaving by the side/centre outlets is the tank water; the
    # bottom drain carries the settled solids, so it only counts when the tank has no other outlet
    exceed, tank_water = {}, {}
    for nid, n in nodes.items():
        if not NODE_TYPES[n["type"]].get("tank"):
            continue
        outs = [e for e in edges if e["source"] == nid and q_edge.get(e["id"], 0.0) > 1e-9]
        water = [e for e in outs if (e.get("sourceHandle") or "out") != "bottom"] or outs
        q = sum(q_edge[e["id"]] for e in water)
        if q <= 1e-9:
            continue
        mixed = {c: sum(q_edge[e["id"]] * c_edge[e["id"]][c] for e in water) / q for c in COMPONENTS}
        tank_water[nid] = {"name": n.get("data", {}).get("name") or nid, "q_in": q_in.get(nid, 0.0), "c": mixed}
        tgp = tgp_pct(mixed["o2"], mixed["temp"])
        node_res.setdefault(nid, {})["c_tank"] = mixed
        if tgp is not None:
            node_res[nid]["tgp_pct"] = tgp
        bad = []
        for c, lim in limits.items():
            if c not in COMPONENTS:
                continue
            v = mixed[c]
            if "max" in lim and lim["max"] is not None and v > lim["max"] + LIMIT_TOL:
                bad.append(f"{COMPONENT_LABEL[c]} {v:.2f} > {lim['max']:g}")
            if "min" in lim and lim["min"] is not None and v < lim["min"] - LIMIT_TOL:
                bad.append(f"{COMPONENT_LABEL[c]} {v:.2f} < {lim['min']:g}")
        if bad:
            for e in water:
                exceed[e["id"]] = bad
            node_warn[nid].append("tank water outside limits: " + "; ".join(bad))
    for nid, res in node_res.items():
        shares_warn = res.pop("solids_shares", None)
        if shares_warn:
            node_warn[nid].append(shares_warn)
        if res.get("o2_short"):
            node_warn[nid].append(f"O₂ runs out: {res['o2_short']:.1f} kg/day more is consumed than arrives")
        if res.get("cooling_short_kw"):
            node_warn[nid].append(f"cooling capacity is {res['cooling_short_kw']:.1f} kW short of the set point "
                                  f"(water leaves at {res['t_out']:.1f} °C)")

    global_warn = []
    if not converged:
        global_warn.append("Concentrations did not settle: some component builds up in the loop "
                           "(e.g. NO₃ with no water exchange, or TAN with no biofilter). "
                           "Add removal or make-up/discharge water.")
    # plausibility: water no fish should ever sit in — almost always a unit or load mistake
    for tw in tank_water.values():
        temp, o2 = tw["c"]["temp"], tw["c"]["o2"]
        if not 0.0 <= temp <= 40.0:
            global_warn.append(f"{tw['name']}: water temperature {temp:.1f} °C outside 0–40 °C")
        if o2 > 1.5 * o2_saturation(temp, 0.0):
            global_warn.append(f"{tw['name']}: oxygen {o2:.0f} mg/L far above saturation — check units/load")
        tgp = tgp_pct(o2, temp)
        if tgp is not None and tgp > 103.0:
            global_warn.append(f"{tw['name']}: total gas pressure {tgp:.0f}% above 103% — gas-bubble trauma risk; "
                               "strip N₂/CO₂ before oxygenation or lower the O₂ target")
    return {
        "converged": converged, "iterations": it + 1,
        "edges": {**{e["id"]: {"q": q_edge.get(e["id"], 0.0), "c": c_edge[e["id"]], "exceed": exceed.get(e["id"], [])}
                     for e in edges},
                  **{e["id"]: {"q": 0.0, "c": dict.fromkeys(COMPONENTS, 0.0), "exceed": [], "gas": True}
                     for e in gas_edges}},
        "nodes": {nid: {**node_res.get(nid, {}), **gas_res.get(nid, {}),
                        **({"pump": pumps[nid]} if nid in pumps else {}), "warnings": node_warn.get(nid, []),
                        "pump_balance": pump_balance.get(nid)} for nid in nodes},
        "warnings": global_warn,
        "makeup_m3h": sum(spec_value(n, "flow_m3h") for n in nodes.values() if n["type"] == "makeup"),
        "pump_power_kw": sum(s["power_kw"] for s in pumps.values()),
        "cooling_kw": sum(r.get("cooling_kw", 0.0) for r in node_res.values()),
        "chiller_power_kw": sum(r.get("chiller_power_kw", 0.0) for r in node_res.values()),
        "heater_power_kw": sum(r.get("heater_power_kw", 0.0) for r in node_res.values()),
        "discharge_m3h": sum(q_in[nid] for nid, n in nodes.items() if n["type"] == "discharge"),
        "totals": _totals(nodes, node_res, edges, q_edge, c_edge, pumps, tank_water),
        "limits": limits,
    }


SOLIDS_PORTS = ("sludge", "backwash", "drain")
BIOFILTERS = ("biofilter", "fbbr")
# water passes these without its O₂ changing on the way to the tanks
O2_NEUTRAL = ("junction", "splitter", "uv", "hx_plate", "hx_tube", "chiller", "heater")


def _o2_target(n):
    typed = spec_value(n, "o2_target_mgl")
    return typed if typed is not None else n.get("_o2_set", 0.0)


def _tanks_fed(start, nodes, edges):
    """Fish tanks that water from `start` reaches through connection points, splitters, UV, heat exchangers
    and chillers (anything that does not change its O₂)."""
    seen, todo, tanks = {start}, [start], []
    while todo:
        cur = todo.pop()
        for e in edges:
            if e["source"] != cur or (e.get("sourceHandle") or "out") in SOLIDS_PORTS:
                continue
            nxt = e["target"]
            if nxt in seen:
                continue
            seen.add(nxt)
            t = nodes[nxt]["type"]
            if NODE_TYPES[t].get("tank"):
                tanks.append(nxt)
            elif t in O2_NEUTRAL:
                todo.append(nxt)
    return tanks
LOAD_KEYS = ("tan_kgd", "co2_kgd", "tss_kgd", "o2_kgd")


def _tank_loads(nodes, basis):
    """Fish loads per tank. A load typed on the tank is used as it is; a 0 is worked out from the tank's feed
    (its own, or the unit's peak feed shared over the tanks by volume) × the plan's production per kg feed."""
    fish = (basis or {}).get("fish") or {}
    tanks = [nid for nid, n in nodes.items() if NODE_TYPES[n["type"]].get("tank")]
    vol = {nid: max(spec_value(nodes[nid], "volume_m3"), 0.0) for nid in tanks}
    # the unit's fish shared over its tanks: a typed share is kept, the rest goes to the others by volume
    typed = {nid: spec_value(nodes[nid], "biomass_pct") for nid in tanks if spec_value(nodes[nid], "biomass_pct") is not None}
    rest = max(100.0 - sum(typed.values()), 0.0)
    v_auto = sum(vol[nid] for nid in tanks if nid not in typed)
    share = {nid: typed[nid] if nid in typed else (rest * vol[nid] / v_auto if v_auto > 0 else 0.0) for nid in tanks}
    out = {}
    for nid in tanks:
        own = spec_value(nodes[nid], "feed_kgd")
        feed = own if own is not None else (fish.get("feed_kgd") or 0.0) * share[nid] / 100
        biomass = (fish.get("biomass_kg") or 0.0) * share[nid] / 100
        ld = {"feed_kgd": feed, "biomass_pct": share[nid], "biomass_kg": biomass,
              "density_kgm3": biomass / vol[nid] if vol[nid] > 0 else None}
        if fish.get("feed_now_kgh") is not None:  # one hour of the day: the feed given in that hour
            ld["feed_now_kgh"] = fish["feed_now_kgh"] * share[nid] / 100
        rates = fish.get("loads")  # one hour of the day, as kg/day at that hour's rate
        for k in LOAD_KEYS:
            typed = spec_value(nodes[nid], k)
            if typed:
                ld[k] = typed
            elif rates is not None:
                ld[k] = (rates.get(k) or 0.0) * share[nid] / 100 if own is None else \
                    (rates.get(k) or 0.0) * feed / max(fish.get("feed_kgd") or 0.0, 1e-9)
            else:
                ld[k] = feed * (fish.get(k.replace("_kgd", "_per_feed")) or 0.0)
        out[nid] = ld
    return out


def _media_plan(nodes, basis):
    """Media per biofilter. A biofilter with its own media volume keeps it; the others share what is still
    required (step 04) by their share % (blank = equal split). Vessel volume = media / fill ratio."""
    basis = basis or {}
    bio = [nid for nid, n in nodes.items() if n["type"] in BIOFILTERS]
    required = basis.get("media_m3") or 0.0
    fill_default = basis.get("fill_pct") or 40.0
    fixed = {nid: spec_value(nodes[nid], "media_m3") for nid in bio if (spec_value(nodes[nid], "media_m3") or 0) > 0}
    auto = [nid for nid in bio if nid not in fixed]
    given = {nid: spec_value(nodes[nid], "share_pct") for nid in auto if spec_value(nodes[nid], "share_pct") is not None}
    blank = [nid for nid in auto if nid not in given]
    rest = max(100.0 - sum(given.values()), 0.0)
    w = {**given, **{nid: rest / len(blank) for nid in blank}}
    total_w = sum(w.values())
    remaining = max(required - sum(fixed.values()), 0.0)
    plan = {}
    for nid in bio:
        fill = spec_value(nodes[nid], "fill_pct") or fill_default
        if nid in fixed:
            media, share, auto_ = fixed[nid], (fixed[nid] / required * 100 if required else None), False
        else:
            share = w[nid] / total_w * 100 if total_w > 0 else 100.0 / len(auto)
            media, auto_ = remaining * share / 100, True
        plan[nid] = {"media_m3": media, "media_share_pct": share, "fill_pct": fill,
                     "vessel_m3": media / (fill / 100) if fill > 0 else None, "media_auto": auto_,
                     "media_required_m3": required or None}
    return plan


def _totals(nodes, node_res, edges, q_edge, c_edge, pumps, tank_water):
    """What the drawn system does, summed over the equipment (per day unless noted)."""
    def total(key, types=None):
        return sum(r.get(key) or 0.0 for nid, r in node_res.items() if types is None or nodes[nid]["type"] in types)
    load = {k: 0.0 for k in ("tan_kgd", "co2_kgd", "tss_kgd", "o2_kgd")}
    heat = 0.0
    for n in nodes.values():
        if n.get("_loads"):
            for k in load:
                load[k] += n["_loads"][k]
        elif n["type"] == "incubator":
            for k in load:
                if any(f["key"] == k for f in NODE_TYPES[n["type"]]["spec"]):
                    load[k] += spec_value(n, k)
        if NODE_TYPES[n["type"]].get("tank"):
            heat += spec_value(n, "heat_kw")
    sludge = [e for e in edges if (e.get("sourceHandle") or "") in SOLIDS_PORTS and q_edge.get(e["id"], 0.0) > 1e-12]
    media = sum(r.get("media_m3") or 0.0 for nid, r in node_res.items() if nodes[nid]["type"] in BIOFILTERS)
    return {
        "fish_load": load, "tank_heat_kw": heat,
        "tss_removed_kgd": total("tss_removed_kgd") + total("tss_captured_kgd"),
        "tan_nitrified_kgd": total("tan_removed_kgd"),
        "o2_nitrification_kgd": total("o2_used_kgd"),
        "co2_stripped_kgd": total("co2_removed_kgd"),
        "pure_o2_kgd": total("pure_o2_kgd", ("cone", "jet_rect", "jet_round")),
        "ozone_kgd": total("ozone_kgd", ("ozone",)),
        "recirculated_m3h": sum(s["flow_m3h"] for s in pumps.values() if s["running"]),
        "sludge_m3h": sum(q_edge[e["id"]] for e in sludge),
        "sludge_tss_kgd": sum(q_edge[e["id"]] * c_edge[e["id"]]["tss"] for e in sludge) * 24 / 1000,
        "hx_duty_kw": total("duty_kw"),
        "media_m3": media,
        # water treated per kind of equipment (m³/h through all of them) — what the equipment must handle
        "flow_by_type": {t: sum(node_res.get(nid, {}).get("q_in") or 0.0 for nid, n in nodes.items() if n["type"] == t)
                         for t in {n["type"] for n in nodes.values()}},
        "tanks": tank_water,
    }


def _treat(n, t, q, m, res):
    """Apply the node's specification to mass flows m (g/h). Returns per-port masses when a port
    gets a different mix than the others (e.g. solids to the sludge outlet), else None."""
    g_h = lambda kg_day: kg_day * 1000 / 24  # noqa: E731
    if NODE_TYPES[t].get("tank"):
        ld = n.get("_loads") or {k: spec_value(n, k) for k in LOAD_KEYS}
        res.update({**{k: ld.get(k) for k in ("feed_kgd", "feed_now_kgh", "biomass_pct", "biomass_kg", "density_kgm3")},
                    **{f"{k[:-4]}_gh": g_h(ld[k]) for k in LOAD_KEYS}})
        # one hour in the tank: feed given, what it makes, and what that does to the water — added on top of
        # the inflow (g/h ÷ m³/h = mg/L) and, without exchange, built up in the tank volume per hour
        vol = spec_value(n, "volume_m3")
        res["feed_kgh"] = ld["feed_now_kgh"] if ld.get("feed_now_kgh") is not None else (ld.get("feed_kgd") or 0.0) / 24
        res["hour"] = {c: {"gh": g_h(ld[f"{c}_kgd"]) * (-1 if c == "o2" else 1),
                           "rise_mgl": g_h(ld[f"{c}_kgd"]) * (-1 if c == "o2" else 1) / q if q > 1e-12 else None,
                           "per_volume_mgl_h": g_h(ld[f"{c}_kgd"]) * (-1 if c == "o2" else 1) / vol if vol > 0 else None}
                       for c in ("tss", "tan", "co2", "o2")}
        m["tan"] += g_h(ld["tan_kgd"])
        m["co2"] += g_h(ld["co2_kgd"])
        m["tss"] += g_h(ld["tss_kgd"])
        need = g_h(ld["o2_kgd"])
        if need > m["o2"]:
            res["o2_short"] = (need - m["o2"]) * 24 / 1000
        m["o2"] = max(m["o2"] - need, 0.0)
        m["temp"] += spec_value(n, "heat_kw") / CP_KW
        shares = {p: spec_value(n, f"{p}_tss_pct") / 100 for p in ("bottom", "center")
                  if any(s["key"] == f"{p}_tss_pct" for s in NODE_TYPES[t]["spec"])}
        warn = []
        out = _solids_split(n, q, m, shares, warn)
        if warn:
            res["solids_shares"] = warn[0]  # surfaced as a node warning after the solve
        return out
    if t in ("rfs", "drum_filter", "sand_filter", "reservoir"):
        removed = m["tss"] * spec_value(n, "tss_removal_pct") / 100
        if t == "reservoir":
            res["retention_h"] = spec_value(n, "volume_m3") / q if q > 1e-12 else None
            if removed <= 0:
                return None
        res["tss_removed_kgd"] = removed * 24 / 1000
        if t == "rfs":  # volume = flow × residence time
            res["residence_min"] = spec_value(n, "residence_min")
            res["volume_m3"] = q * res["residence_min"] / 60
        port = {"sand_filter": "backwash", "reservoir": "drain"}.get(t, "sludge")
        return _solids_to(n, q, m, port, removed)
    if t == "incubator":  # eggs: small loads, no solids handling
        m["tan"] += g_h(spec_value(n, "tan_kgd"))
        m["co2"] += g_h(spec_value(n, "co2_kgd"))
        need = g_h(spec_value(n, "o2_kgd"))
        if need > m["o2"]:
            res["o2_short"] = (need - m["o2"]) * 24 / 1000
        m["o2"] = max(m["o2"] - need, 0.0)
        return None
    if t == "fbbr":  # the fixed bed also traps solids, removed later by backwashing
        captured = m["tss"] * spec_value(n, "tss_removal_pct") / 100
        m["tss"] -= captured
        res["tss_captured_kgd"] = captured * 24 / 1000
    if t in ("biofilter", "fbbr"):
        nitrified = m["tan"] * spec_value(n, "tan_removal_pct") / 100
        res["tan_in_mgl"] = m["tan"] / q if q > 1e-12 else 0.0
        res["tan_out_mgl"] = (m["tan"] - nitrified) / q if q > 1e-12 else 0.0
        res["tan_eff_pct"] = spec_value(n, "tan_removal_pct")
        m["tan"] -= nitrified
        m["no3"] += nitrified
        m["o2"] = max(m["o2"] - nitrified * spec_value(n, "o2_per_tan"), 0.0)
        m["co2"] += nitrified * spec_value(n, "co2_per_tan")
        res["tan_removed_kgd"] = nitrified * 24 / 1000
        res["o2_used_kgd"] = nitrified * spec_value(n, "o2_per_tan") * 24 / 1000
        res["co2_added_kgd"] = nitrified * spec_value(n, "co2_per_tan") * 24 / 1000
    elif t == "degasser":
        removed = m["co2"] * spec_value(n, "co2_removal_pct") / 100
        m["co2"] -= removed
        res["co2_removed_kgd"] = removed * 24 / 1000
        # blower sizing: the stripping gas the G/L ratio asks for (LHO course sheet sizes
        # strippers by this ratio — it ran 2000 % in air mode; 100–500 % is usual for CO₂ columns)
        gl = spec_value(n, "gl_ratio_pct")
        if gl:
            res["gas_m3h"] = q * gl / 100
        o2_out = spec_value(n, "o2_out_mgl")
        if o2_out is not None:
            m["o2"] = o2_out * q
    elif NODE_TYPES[t].get("jet"):
        target = _o2_target(n) * q
        added = max(target - m["o2"], 0.0)
        m["o2"] = max(m["o2"], target)
        res["o2_added_kgd"] = added * 24 / 1000   # fed oxygen: after the solve, from the jet engine
        co2_rm = m["co2"] * spec_value(n, "co2_removal_pct") / 100
        m["co2"] -= co2_rm
        if co2_rm > 0:
            res["co2_removed_kgd"] = co2_rm * 24 / 1000  # a jet with a gas flow strips CO₂ too (LHO: 44%)
    elif t == "cone":
        res["o2_target_mgl"] = _o2_target(n)
        if spec_value(n, "o2_target_mgl") is None:
            res.update(o2_auto=True, tanks_fed=n.get("_tanks_fed", 0))
        target = _o2_target(n) * q
        added = max(target - m["o2"], 0.0)
        m["o2"] = max(m["o2"], target)
        res["pure_o2_kgd"] = added / (spec_value(n, "transfer_eff_pct") / 100) * 24 / 1000
    elif t == "ozone":
        res["tss_removed_kgd"] = m["tss"] * spec_value(n, "tss_removal_pct") / 100 * 24 / 1000
        m["tss"] *= 1 - spec_value(n, "tss_removal_pct") / 100
        dose = spec_value(n, "dose_g_h")
        if dose is None:  # blank: whatever the connected generators supply
            dose = n.get("_gas_g_h", 0.0)
        res["ozone_g_h"] = dose
        res["ozone_kgd"] = dose * 24 / 1000
        res["ozone_mg_per_l"] = dose / q if q > 1e-12 else 0.0
    elif t == "chiller":
        t_in = m["temp"] / q if q > 1e-12 else 0.0
        need = max(t_in - spec_value(n, "setpoint_c"), 0.0) * q * CP_KW
        cap = spec_value(n, "capacity_kw")
        duty = min(need, cap) if cap is not None else need
        m["temp"] -= duty / CP_KW
        res["cooling_kw"] = duty
        res["chiller_power_kw"] = duty / (spec_value(n, "cop") or 1.0)
        res["t_out"] = m["temp"] / q if q > 1e-12 else 0.0
        if need - duty > 1e-6:
            res["cooling_short_kw"] = need - duty
    elif t == "heater":
        t_in = m["temp"] / q if q > 1e-12 else 0.0
        need = max(spec_value(n, "setpoint_c") - t_in, 0.0) * q * CP_KW
        cap = spec_value(n, "capacity_kw")
        duty = min(need, cap) if cap is not None else need
        m["temp"] += duty / CP_KW
        res["heating_kw"] = duty
        res["heater_power_kw"] = duty / (spec_value(n, "cop") or 1.0)
        res["t_out"] = m["temp"] / q if q > 1e-12 else 0.0
        if need - duty > 1e-6:
            res["heating_short_kw"] = need - duty
    elif t == "other":
        for c in ("tss", "tan", "co2", "no3"):
            m[c] *= 1 - spec_value(n, f"{c}_removal_pct") / 100
    return None


def _jet_result(n, res, warn):
    """Run the jet platform engine for one oxygenator at the flow and water it receives."""
    from . import jet
    q, c = res.get("q_in", 0.0), res.get("c_in", {})
    target = _o2_target(n)
    res["o2_target_mgl"] = target
    if spec_value(n, "o2_target_mgl") is None:
        res.update(o2_auto=True, tanks_fed=n.get("_tanks_fed", 0))
    if q <= 1e-6 or res.get("o2_added_kgd", 0.0) <= 1e-9:
        res["pure_o2_kgd"] = 0.0
        return
    try:
        out = jet.design(flow_m3h=q, head_m=spec_value(n, "head_m"), compartments=int(round(spec_value(n, "compartments"))),
                         plate_below_m=spec_value(n, "plate_below_m"), o2_purity_pct=spec_value(n, "o2_purity_pct"),
                         E=spec_value(n, "E"), temp_c=min(max(c.get("temp", 14.0), 0.0), 40.0),
                         o2_in=max(c.get("o2", 0.0), 0.0), targets=[target],
                         shape="round" if n["type"] == "jet_round" else "rect")
    except jet.JetInputError as exc:
        warn.append(f"jet oxygenator: {exc}")
        res["pure_o2_kgd"] = None
        return
    row = out["rows"][0] if out["rows"] else None
    if not row or not row["reachable"]:
        warn.append(f"jet oxygenator cannot reach {target:g} mg/L (ceiling {out['transfer']['O_max']:.1f} mg/L): "
                    "more head or submergence, or a lower target")
        res["pure_o2_kgd"] = None
        return
    ae = row["ae_pct"] / 100
    res.update(jet_ae_pct=row["ae_pct"], pure_o2_kgd=res["o2_added_kgd"] / ae, jet_vent_nlmin=row["vent_nlmin"],
               jet_tgp_pct=row["tgp_pct"], jet_holes=out["nozzle"]["holes"], jet_d_mm=out["nozzle"]["d_mm"],
               jet_size=(f"Ø{out['drawing']['D']:.0f} mm" if out["drawing"]["shape"] == "round"
                         else f"{out['drawing']['W']:.0f} × {out['drawing']['L']:.0f} mm"),
               jet_depth_m=out["levels"]["depth_needed"])
    for m in out["msgs"]:
        if "Head below 0.8" in m or "cannot" in m or "too shallow" in m or "velocity" in m:
            warn.append("jet oxygenator: " + m.split(":")[0])


def _exchange(n, qs, ms, res):
    """Heat exchanger: heat passes from the warmer circuit to the colder one; nothing else crosses.
    A utility side without a pipe is taken from the specification (temperature and flow)."""
    qp, qu = qs["out"], qs["out2"]
    tp = ms["out"]["temp"] / qp if qp > 1e-12 else 0.0
    if qu > 1e-12:
        tu, qu_eff = ms["out2"]["temp"] / qu, qu
    else:
        tu, qu_eff = spec_value(n, "utility_temp_c"), spec_value(n, "utility_flow_m3h")
    heat = 0.0  # kW into the process water (negative = the process water is cooled)
    if qp > 1e-12 and qu_eff > 1e-12:
        heat = spec_value(n, "effectiveness_pct") / 100 * min(qp, qu_eff) * CP_KW * (tu - tp)
    out = {p: dict(ms[p]) for p in ("out", "out2")}
    out["out"]["temp"] += heat / CP_KW
    if qu > 1e-12:
        out["out2"]["temp"] -= heat / CP_KW
    res.update(q_utility=qu_eff, duty_kw=abs(heat), heat_kw=heat,
               mode="cooling" if heat < -1e-9 else "heating" if heat > 1e-9 else "idle",
               t_process_in=tp, t_process_out=tp + heat / (qp * CP_KW) if qp > 1e-12 else tp,
               t_utility_in=tu, t_utility_out=tu - heat / (qu_eff * CP_KW) if qu_eff > 1e-12 else tu)
    return out


def _solids_to(n, q, m, port, removed):
    """Send `removed` g/h of TSS to `port`; the other outlets share the rest by flow."""
    flows = _port_flows_static(n, q)
    q_port = flows.get(port, 0.0)
    q_rest = q - q_port
    out = {}
    for p, qp in flows.items():
        mp = {}
        for c in COMPONENTS:
            if c == "tss":
                mp[c] = removed + (m["tss"] - removed) * (qp / q if q > 1e-12 else 0) if p == port \
                    else (m["tss"] - removed) * (qp / q if q > 1e-12 else 0)
            else:
                mp[c] = m[c] * (qp / q if q > 1e-12 else 0)
        out[p] = mp
    if q_port <= 1e-12 and removed > 0:  # no sludge flow: removed solids cannot leave
        out[port] = {c: 0.0 for c in COMPONENTS}
        for p in out:
            if p != port and q_rest > 1e-12:
                out[p]["tss"] = m["tss"] * flows[p] / q_rest
    return out


def _solids_split(n, q, m, shares, warns=None):
    """Tank: each outlet in `shares` (e.g. bottom 0.8) takes that fraction of the TSS; the outlets
    without a set share take the rest in proportion to their flow. Dissolved components follow flow.
    An outlet with no flow cannot carry solids, so its share goes to the others."""
    flows = _port_flows_static(n, q)
    out = {p: {c: m[c] * (qp / q if q > 1e-12 else 0) for c in COMPONENTS} for p, qp in flows.items()}
    if q <= 1e-12:
        return out
    fixed = {p: s for p, s in shares.items() if flows.get(p, 0.0) > 1e-12}
    free = [p for p, qp in flows.items() if p not in shares and qp > 1e-12]
    total_fixed = sum(fixed.values())
    if free and total_fixed > 1:  # shares over 100% would create solids out of nothing: close the balance
        if warns is not None:
            warns.append(f"solids shares sum to {total_fixed * 100:.0f}% — normalized to 100%")
        fixed = {p: s / total_fixed for p, s in fixed.items()}
        total_fixed = 1.0
    if not free:  # every flowing outlet has a share: scale them to carry all solids
        scale = 1 / total_fixed if total_fixed > 0 else 0
        fixed = {p: s * scale for p, s in fixed.items()} if scale else {p: flows[p] / q for p in flows if flows[p] > 1e-12}
        total_fixed = 1.0
    rest = max(1 - total_fixed, 0.0)
    q_free = sum(flows[p] for p in free)
    for p in flows:
        if p in fixed:
            out[p]["tss"] = m["tss"] * fixed[p]
        elif p in free:
            out[p]["tss"] = m["tss"] * rest * flows[p] / q_free
        else:
            out[p]["tss"] = 0.0
    return out


def _port_flows_static(n, q):
    """The outlet flows found by the flow pass (they know which outlets have a pipe)."""
    return n.get("_flows") or _split_flow(n, q, [])


def default_diagram_flow_through(stage_name: str, makeup_flow_m3h: float, loads: dict | None = None,
                                  n_tanks: int = 4, basis: dict | None = None) -> dict:
    """A flow-through (single-pass) diagram: fresh water → jet O₂ → tanks → discharge.
    No recirculation, no biofilter, no degasser — dilution does everything."""
    loads = loads or {}
    basis = basis or {}
    flow = round(max(makeup_flow_m3h, 1.0), 1)
    per_tank = {k: round(v / n_tanks, 3) for k, v in loads.items()}
    nodes, edges = [], []

    def node(nid, typ, x, y, name, spec=None, outlets=None):
        nodes.append({"id": nid, "type": typ, "position": {"x": x, "y": y},
                      "data": {"name": name, "spec": spec or {}, "outlets": outlets or {}, "qty": 1}})

    def pipe(src, port, dst):
        edges.append({"id": f"e-{src}-{port}-{dst}", "source": src, "sourceHandle": port,
                      "target": dst, "targetHandle": "in"})

    gap = 220
    for i in range(n_tanks):
        node(f"tank{i + 1}", "tank", 400, 40 + i * gap, f"Tank {i + 1}",
             {"tan_kgd": per_tank.get("tan_kgd", 0.0), "co2_kgd": per_tank.get("co2_kgd", 0.0),
              "tss_kgd": per_tank.get("tss_kgd", 0.0), "o2_kgd": per_tank.get("o2_kgd", 0.0)})
    last = 40 + (n_tanks - 1) * gap
    mid = (40 + last) / 2

    pick = lambda **kw: {k: round(basis[v], 4) for k, v in kw.items() if basis.get(v) is not None}  # noqa: E731
    node("makeup", "makeup", 100, mid, "Fresh water (all flow)", {"flow_m3h": flow})
    node("jet", "jet_rect", 250, mid, "Jet oxygenator",
         pick(o2_target_mgl="cone_o2_mgl"))
    node("outlet", "discharge", 700, mid, "Outlet water (single pass)")

    pipe("makeup", "out", "jet")
    half = max(n_tanks // 2, 1)
    for i in range(n_tanks):
        pipe("jet", "out", f"tank{i + 1}")
        pipe(f"tank{i + 1}", "side", "outlet")
    if basis.get("temp_c") is not None:
        nodes[[n["id"] for n in nodes].index("makeup")]["data"]["spec"]["temp"] = round(basis["temp_c"], 2)
    if basis.get("tss_removal_pct"):
        node("rfs", "rfs", 550, mid - 200, "Solids settler (optional)",
             outlets={"sludge": {"mode": "pct", "value": 5}, "out": {"mode": "rest"}})
        for i in range(n_tanks):
            pipe(f"tank{i + 1}", "bottom", "rfs")
        pipe("rfs", "out", "outlet")
        node("sludge", "discharge", 750, mid - 200, "Sludge")
        pipe("rfs", "sludge", "sludge")
    return {"nodes": nodes, "edges": edges, "limits": basis.get("limits") or DEFAULT_LIMITS}


def default_diagram(stage_name: str, design_flow_m3h: float, loads: dict | None = None,
                    n_tanks: int = 4, basis: dict | None = None) -> dict:
    """A typical RAS loop to start from: tanks → drum filter → sump → pump → biofilter →
    degasser → oxygen cone → tanks, with a radial flow settler on the bottom drains.
    basis: the scenario's own assumptions (removal efficiencies, cone outlet O₂, O₂/CO₂ per g TAN,
    tank limits, water temperature), so the drawn loop agrees with the production plan."""
    loads = loads or {}
    basis = basis or {}
    flow = round(max(design_flow_m3h, 1.0), 1)
    per_tank = {k: round(v / n_tanks, 3) for k, v in loads.items()}
    nodes, edges = [], []

    def node(nid, typ, x, y, name, spec=None, outlets=None):
        nodes.append({"id": nid, "type": typ, "position": {"x": x, "y": y},
                      "data": {"name": name, "spec": spec or {}, "outlets": outlets or {}, "qty": 1}})

    def pipe(src, port, dst):
        edges.append({"id": f"e-{src}-{port}-{dst}", "source": src, "sourceHandle": port,
                      "target": dst, "targetHandle": "in"})

    # make-up = water lost with sludge: bottom drains 15 %, settler sludge 2 % of that, drum backwash 1 %
    bottom, rfs_sludge, drum_sludge = 0.15, 0.02, 0.01
    makeup = flow * (bottom * rfs_sludge + (1 - bottom * rfs_sludge) * drum_sludge)
    # Layout chosen for orthogonal pipe routing: tanks in one column; the settler sits top-right,
    # above every bottom drain, so drains leave through the gaps between tanks; the drum filter is
    # level with the middle of the column; the oxygen cone and the inlet manifold share the bottom
    # row so the long return pipe runs under the tanks instead of through them.
    gap = 220  # tanks are drawn large (×1.7)
    for i in range(n_tanks):
        node(f"tank{i + 1}", "tank", 280, 40 + i * gap, f"Tank {i + 1}",
             {"tan_kgd": per_tank.get("tan_kgd", 0.0), "co2_kgd": per_tank.get("co2_kgd", 0.0),
              "tss_kgd": per_tank.get("tss_kgd", 0.0), "o2_kgd": per_tank.get("o2_kgd", 0.0)})
    last = 40 + (n_tanks - 1) * gap
    mid, below = (40 + last) / 2, last + 200
    node("rfs", "rfs", 620, 0, "Radial flow settler",
         outlets={"sludge": {"mode": "pct", "value": rfs_sludge * 100}, "out": {"mode": "rest"}})
    node("drum", "drum_filter", 880, mid, "Drum filter",
         outlets={"sludge": {"mode": "pct", "value": drum_sludge * 100}, "out": {"mode": "rest"}})
    node("waste", "discharge", 1140, 0, "Sludge discharge")
    node("makeup", "makeup", 1140, mid - 190, "Make-up water", {"flow_m3h": round(makeup, 3)})
    node("sump", "sump", 1140, mid + 40, "Sump")
    node("pump", "pump", 1380, mid + 40, "Main pump", {"flow_m3h": flow})
    pick = lambda **kw: {k: round(basis[v], 4) for k, v in kw.items() if basis.get(v) is not None}  # noqa: E731
    node("bio", "biofilter", 1620, mid - 200, "MBBR biofilter",
         pick(tan_removal_pct="tan_removal_pct", o2_per_tan="o2_per_tan", co2_per_tan="co2_per_tan", media_m3="media_m3"))
    node("degas", "degasser", 1660, mid + 40, "CO₂ degasser", pick(co2_removal_pct="co2_removal_pct"))
    node("cone", "cone", 1660, max(below, mid + 240), "Oxygen cone", pick(o2_target_mgl="cone_o2_mgl"))
    if basis.get("tss_removal_pct") is not None:
        nodes[[n["id"] for n in nodes].index("drum")]["data"]["spec"]["tss_removal_pct"] = round(basis["tss_removal_pct"], 2)
    if basis.get("temp_c") is not None:
        nodes[[n["id"] for n in nodes].index("makeup")]["data"]["spec"]["temp"] = round(basis["temp_c"], 2)
    half = max(n_tanks // 2, 1)
    node("split", "splitter", 40, max(below, mid + 240), "Tank inlet manifold",
         outlets={"a": {"mode": "pct", "value": half / n_tanks * 100},
                  "b": {"mode": "rest"}, "c": {"mode": "pct", "value": 0}})
    for i in range(n_tanks):
        nodes[i]["data"]["outlets"] = {"bottom": {"mode": "pct", "value": bottom * 100}, "side": {"mode": "rest"}}
        pipe(f"tank{i + 1}", "bottom", "rfs")
        pipe(f"tank{i + 1}", "side", "drum")
    pipe("rfs", "out", "drum")
    pipe("rfs", "sludge", "waste")
    pipe("drum", "sludge", "waste")
    pipe("drum", "out", "sump")
    pipe("makeup", "out", "sump")
    pipe("sump", "out", "pump")
    pipe("pump", "out", "bio")
    pipe("bio", "out", "degas")
    pipe("degas", "out", "cone")
    pipe("cone", "out", "split")
    # manifold: outlet a feeds the first half of the tanks, b the rest (each outlet splits equally)
    for i in range(n_tanks):
        pipe("split", "a" if i < half else "b", f"tank{i + 1}")
    return {"nodes": nodes, "edges": edges, "limits": basis.get("limits") or DEFAULT_LIMITS}
