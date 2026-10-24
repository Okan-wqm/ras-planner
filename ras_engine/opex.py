"""Operating cost (OPEX) of a production plan, for one year of operation.

A year is a number of batches: what one batch uses as it goes through the farm (feed, oxygen, ozone, eggs,
make-up water, sludge, days in each RAS unit) × batches per year (the plan's 365 / interval unless typed).
Pumps run whenever a unit holds fish: days per batch × batches, at most the whole year.

Every line has a calculated quantity that the user can overwrite, and a unit price:
- feed per feed type (each feed in the plan, with its own price per kg),
- pure oxygen and eggs/fry from the plan, make-up water from the process diagram (or the plan),
- pumping per RAS unit: the pumps chosen in the process diagram when their power is set, otherwise
  lifting the flow by the head loss: 2.725 W per m³/h per metre (ρ·g/3600), divided by the efficiency,
- chillers from the diagram, ozone generation (kWh per kg O₃ entered), base load (kW entered),
- alkalinity (bicarbonate per kg feed), insurance and maintenance (% of CAPEX), farm software,
  sludge handling, the user's own lines and other variable costs (% of everything else),
- payroll: managers, technicians and labour × annual salary, plus pension, social and medical insurance
  funds and other payroll costs as % of salaries.
"""
import math

W_PER_M3H_M = 1000 * 9.81 / 3600  # 2.725 W lifts 1 m³/h by 1 m

DEFAULT_PRICES = {
    # variable costs
    "electricity_price": 0.15,   # per kWh
    "pump_head_m": 3.0,          # head loss the recirculation pumps work against (default per unit)
    "pump_eff_pct": 60.0,        # wire-to-water efficiency (default per unit)
    "ozone_kwh_per_kg": 12.0,    # electricity per kg ozone produced
    "base_load_kw": 10.0,        # lighting, HVAC, blowers, feeders … running all year
    "heat_cop": 3.0,             # heat pump COP for the make-up heating line (immersion = 1.0)
    "feed_price": 1.6,           # per kg feed, for every feed without its own price
    "oxygen_price": 0.25,        # per kg pure O₂
    "ozone_o2_per_kg": 10.0,     # kg O₂ feed gas per kg ozone (0 for air-fed generators)
    "juvenile_price": 0.10,      # per egg / fry stocked
    "water_price": 0.0,          # per m³ make-up water (0 = not priced; the quantity is always shown)
    # stoichiometric: 46 g TAN/kg feed (0.092 kg N/kg protein × 0.5 protein) × 7.14 g CaCO₃/g N
    # × 84/50 g NaHCO₃ per g CaCO₃-equivalent alkalinity = 0.552 kg NaHCO₃/kg feed
    "alkalinity_per_feed": 0.552, # kg sodium bicarbonate per kg feed (nitrification uses alkalinity)
    "alkalinity_price": 0.35,    # per kg bicarbonate
    "insurance_pct": 1.0,        # of CAPEX per year (stock and assets)
    "maintenance_pct": 2.0,      # of CAPEX per year
    "software_cost": 0.0,        # farm software, per year
    "sludge_price": 0.0,         # sludge handling, per tonne dry matter
    "other_var_pct": 5.0,        # other variable costs, % of all other OPEX
    # payroll
    "managers": 1.0, "manager_salary": 60000.0,
    "technicians": 2.0, "technician_salary": 45000.0,
    "labour": 3.0, "labour_salary": 35000.0,
    "pension_pct": 5.0, "social_pct": 15.0, "medical_pct": 3.0, "other_payroll_pct": 5.0,  # % of salaries
    # revenue
    "sale_price": 0.0,           # per kg harvested
    "sale_price_piece": 0.0,     # per fish harvested (fry and fingerlings are sold by the piece)
    "discount_pct": 8.0,         # discount rate for the first-years NPV
    "batches_per_year": 0.0,     # 0 = from the plan: 365 / batch interval
    "depreciation_years": 15.0,  # CAPEX written off over this many years
}
VARIABLE_GROUPS = ["Electricity", "Feed", "Oxygen", "Eggs", "Alkalinity", "Water", "Insurance", "Maintenance",
                   "Farm software", "Sludge handling", "Other", "Other variable"]
PAYROLL_GROUPS = ["Payroll"]


def prices_with_defaults(saved: dict | None) -> dict:
    out = dict(DEFAULT_PRICES)
    for k, v in (saved or {}).items():
        if k in DEFAULT_PRICES and v not in (None, ""):
            out[k] = float(v)
    return out


def pump_kw(q_m3h: float, head_m: float, eff_pct: float) -> float:
    """Electric power to lift q m³/h by head_m: 2.725 W per m³/h per m, divided by the efficiency."""
    if not q_m3h or q_m3h == float("inf") or eff_pct <= 0:
        return 0.0
    return q_m3h * head_m * W_PER_M3H_M / (eff_pct / 100) / 1000


def _num(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def annual_opex(res: dict, saved: dict | None = None, diagrams: dict | None = None,
                capex_total: float = 0.0, design_o2_per_feed: float = 0.0,
                diffuser_efficiency: float = 0.95, design_params=None, water_temp: float = 12.0) -> dict:
    """res: run_plan result. saved: the user's inputs {prices, feed_prices, line_prices, qty, units, extras}.
    diagrams: RAS index -> {pump_kw, chiller_kw, makeup_m3h, pumped_m3h, pure_o2_kgd, ozone_kgd,
    sludge_tss_kgd} from its process diagram, solved on the unit's busiest day."""
    saved = saved or {}
    p = prices_with_defaults(saved.get("prices"))
    sp = saved.get("prices") or {}
    feed_prices = {k: float(v) for k, v in (saved.get("feed_prices") or {}).items() if _num(v) is not None}
    line_prices = {k: float(v) for k, v in (saved.get("line_prices") or {}).items() if _num(v) is not None}
    qty_override = {k: float(v) for k, v in (saved.get("qty") or {}).items() if _num(v) is not None}
    unit_inputs = saved.get("units") or {}
    diagrams = diagrams or {}
    curve = res["curve"]
    stage_of_age = res["stage_of_age"]

    # --- batches per year: typed, or one every interval
    interval = res.get("interval_days") or 0
    plan_batches = 365 / interval if interval else float(len(res["batches"]))
    per_year = p["batches_per_year"] if p["batches_per_year"] > 0 else plan_batches

    # --- one batch through the farm
    feed_by_name = {}
    for r in curve:
        name = r.get("feed_name") or "Feed"
        feed_by_name[name] = feed_by_name.get(name, 0.0) + r["feed_kg"]
    stocked_batch = curve[0]["count"] if curve else 0.0
    harvest = res["harvests"][0] if res["harvests"] else None
    harvest_kg_batch = harvest["biomass_t"] * 1000 if harvest else (curve[-1]["biomass_kg"] if curve else 0.0)
    harvest_pcs_batch = harvest["count"] if harvest else (curve[-1]["count"] if curve else 0.0)

    o2_kg = ozone_kg = plan_water = tss_kg = 0.0
    units = []
    for k, stage in enumerate(res["stages"]):
        d_in = diagrams.get(k) or {}
        design = res["designs"][k] if k < len(res.get("designs") or []) else {}
        key = stage.uid or str(k)
        ui = unit_inputs.get(key) or {}
        head = _num(ui.get("head_m"))
        eff = _num(ui.get("eff_pct"))
        head = p["pump_head_m"] if head is None else head
        eff = p["pump_eff_pct"] if eff is None else eff
        ages = [a for a, st in enumerate(stage_of_age) if st == k and a < len(curve)]
        rows = [curve[a] for a in ages]
        days_batch = len(rows)
        days_year = min(days_batch * per_year, 365.0)   # batches overlap: a unit cannot run more than all year
        hours = days_year * 24
        fin = lambda v: v if v != float("inf") else 0.0  # noqa: E731
        batch = {"o2": sum(r["o2_kgd"] for r in rows), "pure_o2": sum(r["pure_o2_kgd"] for r in rows),
                 "ozone": sum(r["ozone_kgd"] for r in rows), "tss": sum(r["tss_kgd"] for r in rows),
                 "feed": sum(r["feed_kg"] for r in rows), "water": sum(fin(r["q_no3_m3d"]) for r in rows)}
        peak = design.get("peak") or {}
        u = {"index": k, "key": key, "name": stage.name, "hours": hours, "days_batch": days_batch,
             "head_m": head, "eff_pct": eff,
             "head_set": _num(ui.get("head_m")) is not None, "eff_set": _num(ui.get("eff_pct")) is not None}
        if d_in.get("pump_kw"):  # pumps chosen in the diagram, with their power
            u.update(source="diagram", kw=d_in["pump_kw"], q_m3h=d_in.get("pumped_m3h"))
        else:  # the unit's design flow lifted by the head loss; the diagram's pumped flow when drawn
            q_plan = max(design.get("q_unit_m3h") or 0.0, fin(peak.get("q_design_m3h", 0.0)))
            q = d_in.get("pumped_m3h") or q_plan
            u.update(source="head loss", q_m3h=q, kw=pump_kw(q, head, eff))
        u["kwh"] = u["kw"] * hours
        u["chiller_kw"] = d_in.get("chiller_kw") or 0.0
        if d_in.get("makeup_m3h"):  # make-up water as drawn, running whenever the unit holds fish
            u["water_m3"], u["water_source"] = d_in["makeup_m3h"] * hours, "diagram"
        else:
            u["water_m3"], u["water_source"] = batch["water"] * per_year, "plan"
        plan_water += u["water_m3"]

        # oxygen, ozone and sludge: the diagram is solved on the busiest day, so it is scaled down to the
        # batch's own days by the plan's day-by-day load (pure O₂ as dosed, feed, solids)
        def scaled(drawn_peak, profile_sum, profile_peak):
            return drawn_peak * profile_sum / profile_peak if profile_peak else 0.0
        drawn = "pure_o2_kgd" in d_in
        if drawn:
            # scale by the plan's pure-O₂ profile (fish need ÷ diffuser efficiency), not the fish-only O₂
            # profile: the diagram's peak is gas dosed for all consumers, so like must scale like
            o2_kg += scaled(d_in["pure_o2_kgd"], batch["pure_o2"], peak.get("pure_o2_kgd", 0.0)) * per_year
            ozone_kg += scaled(d_in.get("ozone_kgd", 0.0), batch["feed"], peak.get("feed_kg", 0.0)) * per_year
            tss_kg += scaled(d_in.get("sludge_tss_kgd", 0.0), batch["tss"], peak.get("tss_kgd", 0.0)) * per_year
        else:
            o2_kg += batch["pure_o2"] * per_year
            ozone_kg += batch["ozone"] * per_year
            tss_kg += batch["tss"] * per_year
        u["consumables_source"] = "diagram" if drawn else "plan"
        units.append(u)
    stocked = stocked_batch * per_year
    harvest_kg = harvest_kg_batch * per_year
    harvest_pcs = harvest_pcs_batch * per_year

    lines = []

    def line(key, group, item, qty_calc, unit, price, price_name=None, price_default=None, note="", inputs=(),
             qty_name=None, qty_value=None, qty_default=None, price_suffix=""):
        """Every line: quantity × unit price, both typed or calculated.
        qty_name/price_name: the form field; by default q:<key> and pr:<key> (overrides of this line only)."""
        if qty_name is None:  # a calculated quantity the user can overwrite
            qty = qty_override.get(key, qty_calc)
            qty_in = {"name": f"q:{key}", "value": qty_override.get(key, ""), "placeholder": qty_calc}
        else:  # the quantity is an assumption of its own (persons, …)
            qty = qty_calc
            qty_in = {"name": qty_name, "value": qty_value if qty_value is not None else "", "placeholder": qty_default}
        if price_name is None:
            price_name = f"pr:{key}"
            price = line_prices.get(key, price)
            price_value = line_prices.get(key, "")
            price_default = price_default if price_default is not None else price
        else:
            price_value = sp.get(price_name, "") if not price_name.startswith("fp:") else \
                saved.get("feed_prices", {}).get(price_name[3:], "")
            if price_default is None:
                price_default = DEFAULT_PRICES.get(price_name, price)
        lines.append({"key": key, "group": group, "item": item, "qty_calc": qty_calc, "qty": qty,
                      "overridden": key in qty_override, "unit": unit, "unit_price": price,
                      "qty_in": qty_in, "price_in": {"name": price_name, "value": price_value,
                                                     "placeholder": price_default, "suffix": price_suffix},
                      "note": note, "inputs": list(inputs), "cost": (qty or 0.0) * (price or 0.0)})

    def field(name, unit):  # an assumption typed into the "how it is calculated" column
        return (name, sp.get(name, ""), DEFAULT_PRICES[name], unit)

    def pct_line(key, group, item, base, base_unit, pct_name, note):
        line(key, group, item, base, base_unit, p[pct_name] / 100, price_name=pct_name,
             price_default=DEFAULT_PRICES[pct_name], note=note, price_suffix="%")
        lines[-1]["pct"] = True
        lines[-1]["cost"] = (lines[-1]["qty"] or 0.0) * p[pct_name] / 100

    batches_note = f"× {per_year:.2f} batches/yr"

    # --- electricity
    elec = p["electricity_price"]
    for u in units:
        days = f"{u['days_batch']} d/batch {batches_note} = {u['hours'] / 24:,.0f} d"
        if u["source"] == "diagram":
            note, inputs = f"pumps in the process diagram {u['kw']:.1f} kW × {days}", ()
        else:
            note = f"{u['q_m3h']:.0f} m³/h × head × 2.725 W ÷ η = {u['kw']:.1f} kW × {days}"
            inputs = ((f"head:{u['key']}", u["head_m"] if u["head_set"] else "", p["pump_head_m"], "m head"),
                      (f"eff:{u['key']}", u["eff_pct"] if u["eff_set"] else "", p["pump_eff_pct"], "% η"))
        line(f"pump:{u['key']}", "Electricity", f"Pumping — {u['name']}", u["kwh"], "kWh", elec,
             note=note, inputs=inputs)
        if u["chiller_kw"]:
            line(f"chiller:{u['key']}", "Electricity", f"Chillers — {u['name']}", u["chiller_kw"] * u["hours"], "kWh",
                 elec, note=f"{u['chiller_kw']:.1f} kW electric, from the diagram, × {u['hours'] / 24:,.0f} d")
    line("ozone", "Electricity", "Ozone generators", ozone_kg * p["ozone_kwh_per_kg"], "kWh", elec,
         note=f"{ozone_kg:,.0f} kg O₃ ×", inputs=(field("ozone_kwh_per_kg", "kWh/kg"),))
    line("base", "Electricity", "Base load (lighting, HVAC, blowers, feeders)", p["base_load_kw"] * 24 * 365, "kWh",
         elec, note="all year:", inputs=(field("base_load_kw", "kW"),))
    # --- heating the make-up water + tank losses (Lekang ch.11: 1.163 kWh per m³ per K),
    # quoted BEFORE heat recovery — the honest price of not putting a heat exchanger in
    if design_params is not None:
        dt = max(water_temp - design_params.inlet_water_temp, 0.0)
        heat_kwh = plan_water * 1.163 * dt
        n_units = max(len(res.get("stages") or []), 1)
        loss_kw = design_params.tank_ua_w_per_k / 1000 * max(water_temp - design_params.room_temp, 0.0) * n_units
        heat_kwh += loss_kw * 8760
        if heat_kwh > 0:
            bits = [f"{plan_water:,.0f} m³ water × {dt:.0f} K"]
            if loss_kw > 0:
                bits.append(f"{loss_kw:.1f} kW tank losses")
            line("heating", "Electricity", "Heating before recovery (make-up + losses)",
                 heat_kwh / p["heat_cop"], "kWh", elec,
                 note=" + ".join(bits) + f", ÷ COP", inputs=(field("heat_cop", "COP"),))
    # --- feed, oxygen, eggs, alkalinity
    for name, kg in sorted(feed_by_name.items()):
        line(f"feed:{name}", "Feed", name, kg * per_year, "kg", feed_prices.get(name, p["feed_price"]),
             price_name=f"fp:{name}", price_default=p["feed_price"], note=f"{kg:,.0f} kg per batch {batches_note}")
    feed_total = sum(ln["qty"] or 0.0 for ln in lines if ln["group"] == "Feed")
    src = "the process diagrams (busiest day, scaled to each day of the batch)" \
        if any(u["consumables_source"] == "diagram" for u in units) else "the plan, day by day"
    # safety envelope: never quote less oxygen than the design rule over the annual feed
    if design_o2_per_feed > 0 and feed_total > 0 and o2_kg < design_o2_per_feed * feed_total / diffuser_efficiency:
        o2_kg = design_o2_per_feed * feed_total / diffuser_efficiency
        src += ", floored to the design rule (design.o2_per_feed x annual feed)"
    line("oxygen", "Oxygen", "Pure oxygen", o2_kg, "kg", p["oxygen_price"], "oxygen_price",
         note=f"what the oxygenators dissolve, from {src}")
    line("ozone_o2", "Oxygen", "Feed gas for the ozone generators", ozone_kg * p["ozone_o2_per_kg"], "kg",
         p["oxygen_price"], note=f"{ozone_kg:,.0f} kg O₃ ×", inputs=(field("ozone_o2_per_kg", "kg O₂/kg O₃"),))
    line("eggs", "Eggs", "Eggs / fry stocked", stocked, "pcs", p["juvenile_price"], "juvenile_price",
         note=f"{stocked_batch:,.0f} per batch {batches_note}")
    line("alkalinity", "Alkalinity", "Sodium bicarbonate", feed_total * p["alkalinity_per_feed"], "kg",
         p["alkalinity_price"], "alkalinity_price", note=f"{feed_total:,.0f} kg feed ×",
         inputs=(field("alkalinity_per_feed", "kg/kg feed"),))
    # make-up water: priced only when a price is entered, but the quantity is always visible
    water_qty = max(plan_water, 0.0)  # a negative calculated need cannot be sold back: shown as 0
    w_src = []
    for u in units:  # each unit: as drawn in its diagram, or the plan's NO₃-exchange need
        s = "diagram" if u["water_source"] == "diagram" else "plan (NO₃ exchange)"
        if s not in w_src:
            w_src.append(s)
    line("water", "Water", "Make-up water", water_qty, "m³", p["water_price"], "water_price",
         note="from " + " + ".join(w_src) + ("; negative calculated need shown as 0" if plan_water < 0 else ""))
    # --- shares of CAPEX, software, sludge
    pct_line("insurance", "Insurance", "Insurance", capex_total, "CAPEX", "insurance_pct", "share of CAPEX")
    pct_line("maintenance", "Maintenance", "Maintenance", capex_total, "CAPEX", "maintenance_pct", "share of CAPEX")
    line("software", "Farm software", "Farm software (licences, monitoring)", 1.0, "year", p["software_cost"], "software_cost")
    line("sludge", "Sludge handling", "Sludge handling", tss_kg / 1000, "t dry matter", p["sludge_price"], "sludge_price",
         note=f"solids to sludge, from {src}")
    for i, x in enumerate(saved.get("extras") or []):
        line(f"x:{i}", x.get("group") or "Other", x.get("name") or "Other", _num(x.get("qty")) or 0.0,
             x.get("unit") or "", _num(x.get("unit_price")) or 0.0)
    # --- payroll
    for key, item, n, sal in (("managers", "Management", "managers", "manager_salary"),
                              ("technicians", "Technicians", "technicians", "technician_salary"),
                              ("labour", "Labour", "labour", "labour_salary")):
        line(key, "Payroll", item, p[n], "persons", p[sal], sal, qty_name=n, qty_value=sp.get(n),
             qty_default=DEFAULT_PRICES[n])
    salaries = sum(ln["cost"] for ln in lines if ln["group"] == "Payroll")
    pct_line("pension", "Payroll", "Pension fund", salaries, "salaries", "pension_pct", "share of salaries")
    pct_line("social", "Payroll", "Social insurance fund", salaries, "salaries", "social_pct", "share of salaries")
    pct_line("medical", "Payroll", "Medical insurance fund", salaries, "salaries", "medical_pct", "share of salaries")
    pct_line("other_payroll", "Payroll", "Other payroll costs", salaries, "salaries", "other_payroll_pct",
             "share of salaries")
    # --- other variable costs: a share of everything else
    rest = sum(ln["cost"] for ln in lines)
    pct_line("other_var", "Other variable", "Other variable costs", rest, "other OPEX", "other_var_pct",
             "share of all other OPEX")

    total = sum(ln["cost"] for ln in lines)
    by_group = {}
    for ln in lines:
        by_group[ln["group"]] = by_group.get(ln["group"], 0.0) + ln["cost"]
    payroll = by_group.get("Payroll", 0.0)
    kwh = sum(ln["qty"] or 0 for ln in lines if ln["unit"] == "kWh")
    feed_kg = sum(ln["qty"] or 0 for ln in lines if ln["group"] == "Feed")
    depreciation = capex_total / p["depreciation_years"] if p["depreciation_years"] > 0 else 0.0
    selling = p["sale_price"] > 0 or p["sale_price_piece"] > 0
    revenue = harvest_kg * p["sale_price"] + harvest_pcs * p["sale_price_piece"]
    # one harvested fish: what it costs, by group, and what it sells for
    weight_g = harvest_kg / harvest_pcs * 1000 if harvest_pcs else None
    per_fish = None
    if harvest_pcs:
        price_fish = p["sale_price_piece"] + p["sale_price"] * (weight_g or 0) / 1000
        cost_fish = total / harvest_pcs
        dep_fish = depreciation / harvest_pcs
        per_fish = {"weight_g": weight_g, "groups": [(g, v / harvest_pcs) for g, v in by_group.items() if v],
                    "opex": cost_fish, "depreciation": dep_fish, "full": cost_fish + dep_fish,
                    "price": price_fish if selling else None,
                    "margin": price_fish - cost_fish if selling else None,
                    "margin_full": price_fish - cost_fish - dep_fish if selling else None}
    return {
        "per_fish": per_fish, "harvest_weight_g": weight_g,
        "cost_per_batch": total / per_year if per_year else None,
        "harvest_pcs_batch": harvest_pcs_batch, "harvest_kg_batch": harvest_kg_batch,
        "prices": p, "lines": lines, "total": total, "by_group": by_group,
        "payroll": payroll, "variable": total - payroll, "salaries": salaries,
        "batches_per_year": per_year, "plan_batches_per_year": plan_batches,
        "units": units, "kwh": kwh, "feed_kg": feed_kg, "harvest_kg": harvest_kg, "harvest_pcs": harvest_pcs,
        "cost_per_kg": total / harvest_kg if harvest_kg else None,
        "cost_per_piece": total / harvest_pcs if harvest_pcs else None,
        "depreciation": depreciation,
        "full_cost_per_kg": (total + depreciation) / harvest_kg if harvest_kg else None,
        "full_cost_per_piece": (total + depreciation) / harvest_pcs if harvest_pcs else None,
        "revenue": revenue if selling else None,
        "ebitda": revenue - total if selling else None,
        "profit": revenue - total - depreciation if selling else None,
        "payback_years": (capex_total / (revenue - total)) if selling and revenue > total and capex_total else None,
    }


def ramped_cashflow(res: dict, opex_total: float, capex_total: float, price_per_kg: float,
                    discount_pct: float = 8.0) -> dict:
    """The farm's first years as a cash flow, next to the steady-state table.

    Formula (the simplest defensible reading of a plan, documented on purpose):
    - year k covers plan days 365·(k−1) … 365·k−1, starting at the first stocking;
    - revenue_k = the biomass actually harvested in year k (the plan's real harvest days) × price;
    - costs_k = the full OPEX, every year ("fixed costs spread evenly"): a RAS pays its people, pumps
      and insurance from day one, and the fixed/variable split of the OPEX is not passed in — the
      steady-state table above already shows the full-capacity view. (Ramping only the variable share
      by the plan's feed-days would need that split; it would flatter year 1, this does not.)
    - after the plan horizon each year earns the steady-state revenue: one batch's harvest
      × 365 / batch interval × price — the same full-capacity assumption as payback_years;
    - npv = −capex + Σ net_k / (1 + discount_pct/100)^k over the years the plan spans (capex at year 0);
    - payback_ramped_years: where the cumulative net (after capex) crosses zero, linearly interpolated
      inside the crossing year; beyond the plan the steady-state net repeats. None if it never crosses.
    """
    interval = res.get("interval_days") or 0
    per_year = 365 / interval if interval else float(len(res.get("batches") or []))
    batch_kg = (res["harvests"][0]["biomass_t"] * 1000) if res.get("harvests") else 0.0
    rev_ss = batch_kg * per_year * price_per_kg            # a full-capacity year's revenue
    horizon = int(res.get("horizon") or 0)
    n_years = max(1, math.ceil(horizon / 365))
    harvest_t = [0.0] * n_years
    for h in res.get("harvests") or []:
        harvest_t[min(int(h["day"]) // 365, n_years - 1)] += h["biomass_t"]

    years, cum, npv, payback = [], -capex_total, -capex_total, None
    factor = 1.0 + discount_pct / 100.0
    for k in range(1, n_years + 1):
        revenue = harvest_t[k - 1] * 1000 * price_per_kg
        net = revenue - opex_total
        before = cum
        cum += net
        npv += net / factor ** k
        if payback is None and cum >= 0 and net > 0:
            payback = (k - 1) + abs(before) / net
        years.append({"year": k, "harvest_t": harvest_t[k - 1], "revenue": revenue, "costs": opex_total,
                      "net": net, "cumulative": cum})
    if payback is None:  # the table ends still in the red: full-capacity years repeat until it is paid back
        net_ss = rev_ss - opex_total
        k = n_years
        while cum < 0 and net_ss > 0 and k < n_years + 100:
            k += 1
            before = cum
            cum += net_ss
            if cum >= 0:
                payback = (k - 1) + abs(before) / net_ss
    return {"years": years, "npv": npv, "payback_ramped_years": payback,
            "steady_state_revenue": rev_ss, "discount_pct": discount_pct}
