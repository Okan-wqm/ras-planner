"""All constants. Defaults are the values in trout.xlsx ('D. Fry' and 'Sayfa11' sheets), except that oxygen
and CO₂ are split into the fish's share and nitrification's share. Every assumption can be changed on the
Constants page."""
from dataclasses import dataclass, field, fields


def p(default, label, unit="", group="General"):
    return field(default=default, metadata={"label": label, "unit": unit, "group": group})


@dataclass
class GrowthParams:
    initial_count: float = p(400000, "Fish stocked per batch", "fish", "Growth")
    initial_weight_g: float = p(0.2, "Stocking weight", "g", "Growth")
    days: int = p(728, "Simulation length", "days", "Growth")
    # growth: thermal growth coefficient, W^(1/3) grows by TGC/1000 · T every day (degree-days).
    # Without TGC, the Excel model: length grows by (T − Tbase) / Tubase / 30.5 cm a day and W = CF · L³.
    use_tgc: bool = p(False, "Growth by thermal growth coefficient (no = Excel length model)", "", "Growth")
    tgc: float = p(2.5, "Thermal growth coefficient TGC (×1000)", "", "Growth")
    condition_factor: float = p(1.7, "Condition factor (CF)", "", "Growth")
    t_base: float = p(0.0, "Tbase", "°C", "Growth")
    tu_base: float = p(5.12, "Tubase", "°C", "Growth")
    daily_mortality: float = p(0.0002, "Daily mortality", "fraction", "Growth")
    density_length_divisor: float = p(0.32, "Density coefficient (L / x)", "", "Growth")
    max_density: float = p(72.0, "Maximum stocking density", "kg/m³", "Growth")
    temperature: float = p(12.0, "Water temperature (without profile)", "°C", "Growth")


@dataclass
class FeedParams:
    default_fcr: float = p(1.1, "FCR (without feed table)", "", "Feed")
    default_protein: float = p(0.5, "Protein content (without feed table)", "fraction", "Feed")


@dataclass
class GasParams:
    # oxygen is split: what the fish breathe (sets the tank flow) and what nitrification uses in the biofilter
    o2_per_feed: float = p(0.35, "O₂ consumption by the fish", "kg O₂ / kg feed", "Gas balance")
    o2_per_tan: float = p(4.57, "O₂ used by nitrification", "kg O₂ / kg TAN", "Gas balance")
    co2_per_o2: float = p(1.375, "CO₂ / O₂ ratio (fish respiration)", "", "Gas balance")
    co2_per_tan: float = p(5.85, "CO₂ released by nitrification", "kg CO₂ / kg TAN", "Gas balance")
    biofilter_o2_from_pure_o2: bool = p(False, "Biofilter O₂ also from pure oxygen (no = biofilter aeration)", "", "Gas balance")
    diffuser_efficiency: float = p(0.95, "Diffuser efficiency", "fraction", "Gas balance")
    molar_volume: float = p(22.4, "Molar volume", "L/mol", "Gas balance")
    salinity: float = p(3.0, "Salinity", "ppt", "Gas balance")
    pure_oxygen: bool = p(True, "Pure oxygen in use (cone)", "", "Gas balance")
    aeration_saturation: float = p(1.0, "Inlet O₂ saturation without pure O₂ (aeration)", "fraction", "Gas balance")
    # pure O₂: the oxygen cone supersaturates the water that reaches the tanks to this share of saturation
    inlet_o2_saturation: float = p(1.4, "Tank inlet O₂ with pure oxygen (share of saturation)", "fraction", "Gas balance")
    tank_outlet_saturation: float = p(0.8, "Tank outlet O₂ saturation", "fraction", "Gas balance")
    o2_transfer_efficiency: float = p(0.95, "O₂ transfer efficiency", "fraction", "Gas balance")
    # Excel note: 15 g/kg feed for fry under 5 g, 30 g/kg above (per the book)
    ozone_dose_fry: float = p(15.0, "Ozone dose (below threshold)", "g O₃ / kg feed", "Gas balance")
    ozone_dose: float = p(30.0, "Ozone dose (above threshold)", "g O₃ / kg feed", "Gas balance")
    ozone_weight_threshold: float = p(5.0, "Ozone dose weight threshold", "g", "Gas balance")


@dataclass
class FlowParams:
    tan_per_protein: float = p(0.092, "TAN production per protein", "kg N / kg protein", "Flows")
    tan_removal_eff: float = p(0.65, "Biofilter TAN removal efficiency", "fraction", "Flows")
    tan_max: float = p(1.0, "Max. TAN in tank", "mg/L", "Flows")
    # loads peak while the fish are fed: equipment (pumps, filters, degassers, oxygen) is sized for peak × this
    tan_best: float = p(0.0, "Best TAN after biofilter", "mg/L", "Flows")
    co2_removal_eff: float = p(0.66, "CO₂ removal efficiency", "fraction", "Flows")
    co2_max: float = p(15.0, "Max. CO₂ in tank", "mg/L", "Flows")
    co2_best: float = p(0.5, "Best CO₂ after degasser", "mg/L", "Flows")
    tss_per_feed: float = p(0.35, "TSS production", "kg / kg feed", "Flows")
    tss_removal_eff: float = p(0.5, "TSS removal efficiency", "fraction", "Flows")
    tss_max: float = p(10.5, "Max. TSS in tank", "mg/L", "Flows")
    tss_best: float = p(0.5, "Best TSS after filter", "mg/L", "Flows")
    no3_max: float = p(100.0, "Max. NO₃-N", "mg/L", "Flows")
    no3_inlet: float = p(20.0, "Make-up water NO₃-N", "mg/L", "Flows")
    no3_assimilation: float = p(0.0, "Passive nitrification loss", "fraction (0–1)", "Flows")
    denitrification_pct: float = p(0.0, "Passive denitrification", "%", "Flows")
    backflush_60_l_per_kg: float = p(100.0, "Hydrotech 60 µm drum backflush", "L / kg feed / day", "Flows")
    backflush_30_l_per_kg: float = p(200.0, "Hydrotech 30 µm drum backflush", "L / kg feed / day", "Flows")


@dataclass
class BiofilterParams:
    design_weight_g: float = p(0.0, "Design fish weight (0 = manual entry)", "g", "Biofilter")
    feeding_hours: float = p(24, "Feeding hours", "h", "Biofilter")
    temperature: float = p(13.0, "Water temperature", "°C", "Biofilter")
    tank_volume_m3: float = p(60.0, "Single tank volume", "m³", "Biofilter")
    n_tanks: int = p(8, "Tanks per RAS unit", "tanks", "Biofilter")
    protein: float = p(0.5, "Feed protein content", "fraction", "Biofilter")
    tan_per_protein: float = p(0.092, "TAN production per protein", "kg N / kg protein", "Biofilter")
    daily_feed_kg: float = p(100.0, "Daily feed per RAS unit", "kg/day", "Biofilter")
    n_ras_units: int = p(2, "RAS units (peak feed is split)", "units", "Biofilter")
    # Excel note: real feeding increased by 1/3
    feed_safety_factor: float = p(4 / 3, "Feed safety factor (applied to peak feed)", "", "Biofilter")
    media_area_m2m3: float = p(1000.0, "Media specific surface area", "m²/m³", "Biofilter")
    tan_target: float = p(1.0, "Target TAN at tank outlet", "mg/L", "Biofilter")
    makeup_flow_m3h: float = p(0.0, "Make-up water inflow (q)", "m³/h", "Biofilter")
    tan_inlet: float = p(0.0, "Make-up water TAN", "mg/L", "Biofilter")
    fill_ratio_pct: float = p(40.0, "Media fill ratio", "%", "Biofilter")
    # a biofilter cannot take out more than this share of the TAN in one pass
    max_removal_pct: float = p(70.0, "Max. TAN removal per pass", "%", "Biofilter")
    retention_times: str = p("25,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,60",
                             "Tank retention times", "min", "Biofilter")
    # Source: J.P. Blancheton (IFREMER), "Mass balance, biofiltration kinetics and biofilter
    # sizing", Sète 2016 RAS course — slides 19 & 25: seawater 600 m²/m³ media,
    # Kbmax = 9.2 g TAN/m³·h, Ks = 0.65 g/m³; other media × S/600; freshwater ×1.3;
    # Kb(t) = Kb(20)·exp(0.06·(t−20)). Same constants as the trout workbook's Sayfa11.
    monod_max: float = p(9.2, "Max. nitrification rate (20 °C, 600 m²/m³)", "g TAN/m³/h", "Biofilter")
    monod_ks: float = p(0.65, "Monod Ks", "mg/L", "Biofilter")
    reference_area: float = p(600.0, "Reference media area", "m²/m³", "Biofilter")
    temp_coefficient: float = p(0.06, "Temperature coefficient", "", "Biofilter")
    freshwater_factor: float = p(1.3, "Freshwater rate multiplier", "", "Biofilter")


@dataclass
class DesignParams:
    """Safety envelope: every design number sits on the SAFE side. Demands take
    max(calculation, rule) so equipment is never sized below the rule."""
    # e.g. the O₂ calculation may come out at ~0.6 kg/kg feed, but the oxygenation plant is
    # bought for at least 1 kg O₂ per kg feed (the engineer's design rule)
    o2_per_feed: float = p(1.0, "Design O₂ (safety rule: max of this and the calculation)",
                           "kg O₂ / kg feed", "Design")
    co2_per_o2: float = p(1.4, "Design CO₂ per design O₂", "kg CO₂ / kg O₂", "Design")
    biofilter_min_retention: float = p(4.0, "Minimum biofilter residence time (NO₂ risk below)",
                                       "min", "Design")
    tan_knee: float = p(2.0, "TAN knee: kinetics fall sharply below (media volume explodes)",
                        "mg/L", "Design")
    # heating budget (Lekang, Aquaculture Engineering ch.11): energy per m³ per K = 1.163 kWh
    inlet_water_temp: float = p(8.0, "Make-up water temperature at the plant inlet", "°C", "Design")
    room_temp: float = p(18.0, "Machine-room / hall temperature (tank losses)", "°C", "Design")
    # lumped tank + piping loss per RAS unit; 0 = losses off (the classic U·A·(T_water − T_room))
    tank_ua_w_per_k: float = p(0.0, "Tank & pipe heat loss coefficient (0 = off)", "W/K", "Design")
    # system mode: "ras" (recirculating with biofilter/degasser) or "flow_through" (single-pass)
    mode: str = p("ras", "System mode (ras = recirculating, flow_through = single-pass)", "", "Design")


@dataclass
class Params:
    growth: GrowthParams = field(default_factory=GrowthParams)
    feed: FeedParams = field(default_factory=FeedParams)
    gas: GasParams = field(default_factory=GasParams)
    flow: FlowParams = field(default_factory=FlowParams)
    design: DesignParams = field(default_factory=DesignParams)

    @classmethod
    def from_flat(cls, data: dict) -> "Params":
        """Build from a flat dict such as {'growth.days': 300, ...}."""
        obj = cls()
        for section in fields(obj):
            sub = getattr(obj, section.name)
            for f in fields(sub):
                key = f"{section.name}.{f.name}"
                if key in data and data[key] not in (None, ""):
                    setattr(sub, f.name, cast(f.default, data[key]))
        return obj

    def to_flat(self) -> dict:
        return {f"{s.name}.{f.name}": getattr(getattr(self, s.name), f.name)
                for s in fields(self) for f in fields(getattr(self, s.name))}


def cast(default, value):
    """Convert a form/JSON value to the default's type (including booleans such as 'on', 'False')."""
    if isinstance(default, bool):
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "on", "yes")
    return type(default)(value)


def parse_steps(text: str) -> list[tuple[int, float]]:
    """'1:15, 56:30' -> [(1, 15.0), (56, 30.0)], sorted by day.
    Calendar-date text ('1 Jan:10, 10 Feb:12', '10-02:12') is accepted too and
    converted to day numbers, so every entry point (forms, AI, agents) shares one parser."""
    try:
        steps = []
        for part in str(text).replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            day, value = part.split(":")
            steps.append((int(day), float(value)))
        return sorted(steps)
    except ValueError:
        return parse_date_steps(text)


def step_value(steps: list[tuple[int, float]], day: int, default: float) -> float:
    value = default
    for start, v in steps:
        if day >= start:
            value = v
    return value


def step_value_interp(steps: list[tuple[int, float]], day: int, default: float) -> float:
    """Like step_value but with LINEAR INTERPOLATION between steps:
    the temperature ramps gradually from one step to the next instead of jumping.
    Day 20 between (1,10) and (41,12) → 10 + (12-10)×19/40 = 10.95°C."""
    if not steps:
        return default
    if day <= steps[0][0]:
        return steps[0][1]
    for i in range(len(steps) - 1):
        d1, t1 = steps[i]
        d2, t2 = steps[i + 1]
        if d1 <= day <= d2:
            if d2 == d1:
                return t2
            return t1 + (t2 - t1) * (day - d1) / (d2 - d1)
    return steps[-1][1]


def parse_date_steps(text: str) -> list[tuple[int, float]]:
    """Parse calendar-date based steps: '1 Jan:10, 10 Feb:12, 10 May:14'
    or '01-01:10, 10-02:12' or '1/1:10, 10/2:12'.
    Returns day-number steps [(day, temp), ...] ready for step_value_interp."""
    import re
    from datetime import date as _date

    MONTHS = {"jan": 1, "oca": 1, "feb": 2, "sub": 2, "mar": 3, "apr": 4, "nis": 4,
              "may": 5, "jun": 6, "haz": 6, "jul": 7, "tem": 7, "aug": 8, "ağu": 8,
              "sep": 9, "eyl": 9, "oct": 10, "ekı": 10, "ekim": 10, "nov": 11, "kas": 11,
              "dec": 12, "ara": 12}

    steps = []
    for part in str(text).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        # try "day month:temp" format (e.g., "1 Jan:10", "10 Feb:12")
        m = re.match(r"(\d{1,2})\s+(\w+)\s*:\s*([\d.]+)", part, re.I)
        if m:
            day, mon_name, temp = int(m.group(1)), m.group(2).lower()[:3], float(m.group(3))
            mon = MONTHS.get(mon_name)
            if mon is None:
                raise ValueError(f"Unknown month '{m.group(2)}' in '{part}'")
            day_of_year = (_date(2027, mon, min(day, 28)) - _date(2027, 1, 1)).days + 1
            steps.append((day_of_year, temp))
            continue
        # try "DD-MM:temp" or "MM/DD:temp" format
        m = re.match(r"(\d{1,2})[-/](\d{1,2})\s*:\s*([\d.]+)", part)
        if m:
            d1, d2, temp = int(m.group(1)), int(m.group(2)), float(m.group(3))
            # assume DD-MM (European)
            day_num, mon = d1, d2
            day_of_year = (_date(2027, mon, min(day_num, 28)) - _date(2027, 1, 1)).days + 1
            steps.append((day_of_year, temp))
            continue
        # fall back to plain "day:temp"
        try:
            day, value = part.split(":")
            steps.append((int(day.strip()), float(value.strip())))
        except (ValueError, IndexError):
            raise ValueError(f"Invalid step format: '{part}' — use '1 Jan:10' or '41:12'")
    return sorted(steps)
