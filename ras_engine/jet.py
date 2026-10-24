"""Jet platform oxygenator: pre-sizing and oxygen transfer (engine of tools/jet_tasarim.py, version 2).

Water falls through nozzles in a plate as jets into a pure-O₂ gas cushion; the gas passes the n compartments in
series, the water splits equally over them. Each compartment: gas well mixed, water passes once,
O2_out = O2_in + E·(C* − O2_in), C* = K_O2·x_O2·(P − p_w); N₂ with E_N2 = 1 − (1 − E)^0.94.
E is calibrated to the FOX France Oxygénation jet-platform table (E = 0.725 at 1.0 m head, 0.5 m
submergence, 14 °C) and corrected for head (NTU ∝ head; ∝ head³ below 0.8 m, not verified by measurement)
and temperature (θ = 1.024). Solubility: Weiss (1970). This is a pre-sizing tool: confirm on a prototype
(vent gas flow and its O₂ %). The functions below are the original ones; `design()` returns the results
instead of printing them.
"""
import math

G = 9.81
O2_G_PER_L_15C = 1.354      # g/l, 15 °C 1 atm
ML_PER_MMOL_15C = 23.6      # ideal gaz, 15 °C 1 atm

# Kalibrasyon (FOX tablosu)
E_REF = 0.725
H_REF = 1.0
S_REF = 0.5
T_REF = 14.0
THETA = 1.024

NOZUL_TIPLERI = {           # (Cd, Cv)
    "yuvarlak": (0.92, 0.95),
    "kisa-boru": (0.82, 0.82),
    "keskin": (0.62, 0.97),
}
PEN_C = 0.40 / (4.32 ** 0.775 * 0.012 ** 0.67)   # bulut derinliği katsayısı (~2.49)


# ---------------------------------------------------------------- çözünürlük
_WEISS = {  # Bunsen katsayısı: ln b = A1 + A2(100/T) + A3 ln(T/100) + S[B1 + B2(T/100) + B3(T/100)^2]
    "O2": (-58.3877, 85.8079, 23.8439, -0.034892, 0.015568, -0.0019387),
    "N2": (-59.6274, 85.7661, 24.3696, -0.051580, 0.026329, -0.0037252),
}
_STP_DENS = {"O2": 1429.0, "N2": 1250.5}  # mg/l gaz, 0 °C 1 atm


def bunsen(t_c, sal, gas):
    a1, a2, a3, b1, b2, b3 = _WEISS[gas]
    T = t_c + 273.15
    return math.exp(a1 + a2 * (100 / T) + a3 * math.log(T / 100)
                    + sal * (b1 + b2 * (T / 100) + b3 * (T / 100) ** 2))


def vapor_atm(t_c):
    T = t_c + 273.15
    return math.exp(11.8571 - 3840.70 / T - 216961 / T ** 2)


class Water:
    def __init__(self, t_c, sal):
        self.t = t_c
        self.S = sal
        self.KO = bunsen(t_c, sal, "O2") * _STP_DENS["O2"]   # mg/l per atm saf O2
        self.KN = bunsen(t_c, sal, "N2") * _STP_DENS["N2"]
        self.pw = vapor_atm(t_c)
        self.O_sat = self.KO * 0.2095 * (1 - self.pw)
        self.N_sat = self.KN * 0.7808 * (1 - self.pw)
        self.rho = 1.0 + 0.00078 * sal   # bağıl yoğunluk, kaba


# ---------------------------------------------------------------- yardımcı
def bisect(f, lo, hi, it=200):
    flo = f(lo)
    for _ in range(it):
        mid = 0.5 * (lo + hi)
        fm = f(mid)
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
    return 0.5 * (lo + hi)


def transfer_E(H, t_c=T_REF, n_hi=1.0, n_lo=3.0, H_break=0.8, H_cap=1.2, E_ref=E_REF):
    ntu_ref = -math.log(1 - E_ref)
    Hc = min(H, H_cap)
    if Hc >= H_break:
        ntu = ntu_ref * (Hc / H_REF) ** n_hi
    else:
        ntu = ntu_ref * (H_break / H_REF) ** n_hi * (Hc / H_break) ** n_lo
    ntu *= THETA ** (t_c - T_REF)
    return 1 - math.exp(-ntu)


def _make_run(w, Oin, Nin, E, P, n, purity):
    Pd = P - w.pw
    EN = 1 - (1 - E) ** 0.94
    frac = 1.0 / n

    def run(G0):
        """G0: beslenen toplam gaz, mmol / l su. Dönüş: (O2 çıkış, N2 çıkış, tahliye) ya da None."""
        Gin, xin = G0, purity
        Os, Ns = [], []
        for _ in range(n):
            tO = lambda x: frac * E * (w.KO * x * Pd - Oin) / 32
            tN = lambda x: frac * EN * (w.KN * (1 - x) * Pd - Nin) / 28
            f = lambda x: Gin * xin - (Gin - tO(x) - tN(x)) * x - tO(x)
            x = bisect(f, 1e-9, 1 - 1e-12, it=80)
            Gout = Gin - tO(x) - tN(x)
            if Gout < 0:
                return None
            Os.append(Oin + E * (w.KO * x * Pd - Oin))
            Ns.append(Nin + EN * (w.KN * (1 - x) * Pd - Nin))
            Gin, xin = Gout, x
        return sum(Os) / n, sum(Ns) / n, Gin
    return run


def _boundary(run):
    """Tahliyenin sıfıra indiği en küçük gaz beslemesi (yoksa None)."""
    lo = 1e-9
    if run(lo) is not None:
        return None
    hi = 1e-3
    while run(hi) is None:
        hi *= 2
        if hi > 1e3:
            return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if run(mid) is None:
            lo = mid
        else:
            hi = mid
    return hi


def novent_point(w, Oin, Nin, E, P, n, purity=1.0):
    """Tahliyesiz çalışılabilen en yüksek çıkış O2 (yoksa giriş değeri)."""
    run = _make_run(w, Oin, Nin, E, P, n, purity)
    Gb = _boundary(run)
    if Gb is None:
        return Oin
    return max(Oin, run(Gb)[0])


# ---------------------------------------------------------------- gaz transferi
def staged(w, Oin, Nin, Otarget, E, P, n, purity=1.0):
    """n bölme, gaz seri. Hedef çıkış O2 için gerekli beslemeyi bulur.
    purity: besleme gazındaki O2 oranı (kalanı inert: N2/Ar, N2 gibi davranır).
    Dönüş: dict veya None (ulaşılamaz). feed = beslenen saf O2 (mg/l su)."""
    Pd = P - w.pw
    O_max = Oin + E * (w.KO * purity * Pd - Oin)
    if Otarget >= O_max - 1e-6:
        return None
    run = _make_run(w, Oin, Nin, E, P, n, purity)
    Gb = _boundary(run)
    if Gb is not None:
        Ob = run(Gb)[0]
        if Otarget <= Ob:
            feed = Otarget - Oin
            gas = feed / 32 / purity
            Nout = Nin + gas * (1 - purity) * 28
            return dict(AE=1.0, feed=feed, vent_mmol=0.0, Nout=Nout, novent=True)
        lo = Gb
    else:
        lo = (Otarget - Oin) / 32.0 / purity

    def g(G0):
        r = run(G0)
        return -1.0 if r is None else r[0] - Otarget

    hi = lo * 2
    while g(hi) < 0:
        hi *= 2
        if hi > lo * 1e4:
            return None
    G0 = bisect(g, lo, hi, it=100)
    r = run(G0)
    if r is None:
        return None
    Oo, No, vent = r
    return dict(AE=(Oo - Oin) / 32 / (G0 * purity), feed=G0 * purity * 32, vent_mmol=vent, Nout=No,
                novent=False)


# ---------------------------------------------------------------- hidrolik
def choose_layout(N_inst, A_req, n_ch, d, Q, U_max):
    p_raw = math.sqrt(A_req / N_inst)
    p = max(math.floor(p_raw / 0.005 + 1e-9) * 0.005, 4 * d)
    m_side = max(0.04, 3.3 * d)
    m_end = 0.06
    best = None
    for r in range(1, max(13, math.ceil(math.sqrt(N_inst / n_ch)) * 3)):
        W = n_ch * ((r - 1) * p + 2 * m_side)
        c_min = math.ceil(N_inst / (n_ch * r))
        c_area = math.ceil(max(((A_req / W) - 2 * m_end) / p + 1, 1))
        c = max(c_min, c_area, 2)
        L = (c - 1) * p + 2 * m_end
        area = W * L
        aspect = max(W, L) / min(W, L)
        score = area / A_req + 0.3 * max(0.0, aspect - 1.5)
        if Q / area > U_max * 1.05:
            score += 10
        cand = dict(rows=r, cols=c, pitch=p, W=W, L=L, area=area, holes=n_ch * r * c,
                    aspect=aspect, score=score)
        if best is None or cand["score"] < best["score"]:
            best = cand
    return best




NOZZLE_TYPES = {"rounded": "yuvarlak", "short-tube": "kisa-boru", "sharp": "keskin"}
NOZZLE_LABELS = {"rounded": "Rounded inlet (r ≥ 0.2 d)", "short-tube": "Short tube (straight hole, thick plate)",
                 "sharp": "Sharp-edged (straight hole, thin sheet)"}
DEFAULTS = dict(flow_m3h=100.0, flow_min_m3h=None, head_m=1.0, tank_margin_m=0.0, water_depth_m=None,
                submergence_m=None, temp_c=14.0, salinity=0.0, o2_in=8.0, n2_in_pct=100.0, targets=None,
                compartments=4, nozzle_mm=None, nozzle_type="rounded", cd=None, cushion_m=0.20, spare_pct=10.0,
                pump_eff=0.45, o2_purity_pct=95.0, E=None, low_head_exp=3.0, altitude_m=0.0, shape="rect",
                floor_clear_m=0.30, baffle_m=None, skirt_m=None, plate_below_m=0.10)
SHAPES = {"rect": "Rectangular (square) plate", "round": "Round plate"}


def rect_holes(lay, n, d, N_open):
    """Hole centres (mm) on a rectangular plate: compartments side by side across the width, each with
    `rows` rows of `cols` holes. The spare holes (plugged) are spread evenly over the plate."""
    p, m_side, m_end = lay["pitch"], max(0.04, 3.3 * d), 0.06
    wc = (lay["rows"] - 1) * p + 2 * m_side
    holes = []
    for c in range(n):
        for i in range(lay["rows"]):
            for j in range(lay["cols"]):
                holes.append([(c * wc + m_side + i * p) * 1000, (m_end + j * p) * 1000, c])
    _mark_spares(holes, N_open)
    baffles = [[c * wc * 1000, 0, c * wc * 1000, lay["L"] * 1000] for c in range(1, n)]
    return {"shape": "rect", "W": lay["W"] * 1000, "L": lay["L"] * 1000, "holes": holes, "baffles": baffles,
            "pitch_mm": p * 1000, "edge_side_mm": m_side * 1000, "edge_end_mm": m_end * 1000,
            "compartment_mm": wc * 1000}


def _mark_spares(holes, N_open):
    """Keep N_open holes open, plug the rest — spread evenly so every compartment keeps its share."""
    spare = len(holes) - N_open
    for h in holes:
        h.append(False)
    if spare <= 0:
        return
    step = len(holes) / spare
    for k in range(spare):
        holes[min(int(k * step + step / 2), len(holes) - 1)][3] = True


def round_layout(N_inst, A_req, n_ch, d, Q, U_max, N_open):
    """A round plate: holes on a square grid inside the circle, compartments as equal sectors (radial baffles).
    The diameter grows until the holes fit and the downward velocity stays within its limit."""
    p_raw = math.sqrt(A_req / N_inst)
    p = max(math.floor(p_raw / 0.005 + 1e-9) * 0.005, 4 * d)
    m_edge = max(0.04, 3.3 * d)          # from the rim
    m_baf = max(0.02, 1.6 * d)           # from a radial baffle (each side)
    D = max(math.sqrt(4 * A_req / math.pi), 2 * (m_edge + p))
    for _ in range(2000):
        R = D / 2
        k = int(R / p) + 1
        holes = []
        for i in range(-k, k + 1):
            for j in range(-k, k + 1):
                x, y = (i + 0.5) * p, (j + 0.5) * p
                r = math.hypot(x, y)
                if r > R - m_edge:
                    continue
                if n_ch > 1:
                    ang = math.atan2(y, x) % (2 * math.pi)
                    sector = int(ang / (2 * math.pi / n_ch)) % n_ch
                    # distance to the nearest radial baffle line
                    to_baffle = min(abs(math.sin(ang - b * 2 * math.pi / n_ch)) * r for b in range(n_ch))
                    if to_baffle < m_baf or r < m_baf * 2:
                        continue
                else:
                    sector = 0
                holes.append([(x + R) * 1000, (y + R) * 1000, sector])
        area = math.pi * R ** 2
        if len(holes) >= N_inst and Q / area <= U_max * 1.0001:
            break
        D += 0.005
    _mark_spares(holes, N_open)
    R = D / 2
    baffles = [[R * 1000, R * 1000, (R + R * math.cos(b * 2 * math.pi / n_ch)) * 1000,
                (R + R * math.sin(b * 2 * math.pi / n_ch)) * 1000] for b in range(n_ch)] if n_ch > 1 else []
    counts = [sum(1 for h in holes if h[2] == c) for c in range(n_ch)]
    return {"shape": "round", "D": D * 1000, "W": D * 1000, "L": D * 1000, "holes": holes, "baffles": baffles,
            "pitch": p, "area": math.pi * R ** 2, "per_sector": counts, "pitch_mm": p * 1000,
            "edge_side_mm": m_edge * 1000, "baffle_gap_mm": m_baf * 1000}


def baro_atm(altitude_m: float) -> float:
    """Barometric pressure at a site altitude (standard atmosphere), atm."""
    return (1 - 2.25577e-5 * altitude_m) ** 5.25588


def tgp_pct(do_mgl, temp_c, sal=0.0, pb=1.0):
    """Total gas pressure as % of saturation for water at `do_mgl` with N₂ and Ar at air
    saturation — the usual state after a stripper or the tank itself (LHO course sheet's check).
    Air-saturated water returns ~100 %; above ~103 % there is gas-bubble trauma risk."""
    if do_mgl is None or temp_c is None or not 0.0 <= temp_c <= 40.0:
        return None
    w = Water(temp_c, sal)
    if w.KO <= 0 or pb <= 0:
        return None
    p_o = do_mgl / w.KO
    p_na = (0.7808 + 0.0093) * (1.0 - w.pw)  # N₂ + Ar at air saturation
    return 100.0 * (p_o + p_na + w.pw) / pb


class JetInputError(ValueError):
    pass




def design(**kw) -> dict:
    """The calculation of tools/jet_tasarim.py main(), returning numbers instead of printing them.
    Keyword names are the English versions of the command-line options (see DEFAULTS)."""
    a = dict(DEFAULTS)
    a.update({k: v for k, v in kw.items() if v is not None})
    if a["flow_m3h"] <= 0:
        raise JetInputError("Flow must be above zero.")
    if a["flow_m3h"] > 10_000:
        raise JetInputError("Flow above 10,000 m³/h is out of the calibration range.")
    if a["flow_min_m3h"] is not None and not (0 < a["flow_min_m3h"] <= a["flow_m3h"]):
        raise JetInputError("Minimum flow must be between zero and the design flow.")
    if int(a["compartments"]) < 1:
        raise JetInputError("At least one compartment.")
    if a["E"] is not None and not (0 < a["E"] < 1):
        raise JetInputError("Measured E must be between 0 and 1.")
    if a["cushion_m"] <= 0:
        raise JetInputError("Gas cushion height must be above zero.")
    if not (50 <= a["o2_purity_pct"] <= 100):
        raise JetInputError("Feed gas O₂ must be between 50 and 100 %.")
    if not (0 < a["pump_eff"] <= 1):
        raise JetInputError("Pump efficiency must be between 0 and 1.")
    if not (0 <= a["temp_c"] <= 40):
        raise JetInputError("Temperature must be between 0 and 40 °C (range of the solubility equations).")
    if not (0 <= a["salinity"] <= 40):
        raise JetInputError("Salinity must be between 0 and 40 g/kg.")
    if a["cd"] is not None and not (0 < a["cd"] <= 1):
        raise JetInputError("The discharge coefficient Cd must be between 0 and 1.")
    if a["nozzle_mm"] and not (8 <= a["nozzle_mm"] <= 25):
        raise JetInputError("Nozzle diameter must be between 8 and 25 mm.")
    if a["o2_in"] < 0 or not (50 <= a["n2_in_pct"] <= 150):
        raise JetInputError("Inlet O₂ cannot be negative and inlet N₂ saturation must be between 50 and 150 %.")
    if a["cushion_m"] < 0.1:
        raise JetInputError("Gas cushion height must be at least 0.1 m.")
    if not (1 <= int(a["compartments"]) <= 8):
        raise JetInputError("Use 1 to 8 gas compartments.")
    if a["shape"] not in SHAPES:
        raise JetInputError("Plate shape must be rectangular or round.")
    if a["submergence_m"] is not None and not (0.1 <= a["submergence_m"] <= 2):
        raise JetInputError("Submergence must be between 0.1 and 2 m, or the gas cushion cannot seal and vent.")
    if not (0.0 <= a["plate_below_m"] <= 1.5):
        raise JetInputError("The plate must sit 0 to 1.5 m below the tank water level.")
    for key, label in (("baffle_m", "Baffle depth"), ("skirt_m", "Skirt depth")):
        if a[key] is not None and not (0.1 <= a[key] <= 3):
            raise JetInputError(f"{label} must be between 0.1 and 3 m.")
    if not (0.05 <= a["floor_clear_m"] <= 2):
        raise JetInputError("Floor clearance under the skirt must be between 0.05 and 2 m.")
    if a["tank_margin_m"] < 0:
        raise JetInputError("The feed tank rise cannot be negative.")
    if not (-100 <= a["altitude_m"] <= 4000):
        raise JetInputError("Altitude must be between −100 and 4000 m.")
    n = int(a["compartments"])
    msgs = []
    w = Water(a["temp_c"], a["salinity"])
    Pb = baro_atm(a["altitude_m"])          # site barometric pressure, atm
    w.O_sat = w.KO * 0.2095 * (Pb - w.pw)
    w.N_sat = w.KN * 0.7808 * (Pb - w.pw)
    Q = a["flow_m3h"] / 3600.0
    H, Lj = a["head_m"], a["cushion_m"]
    Cd, Cv = NOZUL_TIPLERI[NOZZLE_TYPES.get(a["nozzle_type"], "yuvarlak")]
    if a["cd"] is not None:
        Cd = a["cd"]
        Cv = Cd if Cd >= 0.75 else 0.97
    h0 = H - Lj
    if h0 <= 0.05:
        raise JetInputError(f"The head ({H} m) must be at least 5 cm more than the gas cushion ({Lj} m).")
    v0 = Cv * math.sqrt(2 * G * h0)
    vi = math.sqrt(v0 ** 2 + 2 * G * Lj)
    if a["nozzle_mm"]:
        d = a["nozzle_mm"] / 1000
    else:
        d_target = min(max(Lj / 16, 0.010), 0.016)
        d = min([0.008, 0.010, 0.012, 0.014, 0.016, 0.020], key=lambda x: abs(x - d_target))

    def qcoef(dd):
        return Cd * math.pi * dd ** 2 / 4 * math.sqrt(2 * G * h0)
    q = qcoef(d)
    N_req = Q / q
    N_inst = math.ceil(N_req * (1 + a["spare_pct"] / 100))
    U_max = 0.085 - (0.085 - 0.050) * min(a["salinity"] / 35.0, 1.0)
    A_req = Q / U_max
    lay = choose_layout(N_inst, A_req, n, d, Q, U_max)
    if lay["holes"] > 50_000:
        raise JetInputError("Nozzle plate layout needs more than 50,000 holes — out of range.")
    N_open = math.ceil(N_req)
    if a["shape"] == "round":
        drawing = round_layout(N_inst, A_req, n, d, Q, U_max, N_open)
        lay = {**lay, "W": drawing["D"] / 1000, "L": drawing["D"] / 1000, "area": drawing["area"],
               "holes": len(drawing["holes"]), "pitch": drawing["pitch"], "aspect": 1.0}
    else:
        drawing = rect_holes(lay, n, d, N_open)
    U = Q / lay["area"]
    pen = PEN_C * vi ** 0.775 * d ** 0.67
    # baffles and skirt reach below the bubble cloud so gas cannot pass under them (estimate unless given)
    d_baf = a["baffle_m"] if a["baffle_m"] is not None else max(0.45, 1.5 * pen)
    d_skirt = a["skirt_m"] if a["skirt_m"] is not None else max(0.60, 2.0 * pen)
    clear = a["floor_clear_m"]
    if a["baffle_m"] is not None and d_baf < pen:
        msgs.append(f"Baffles reach {d_baf:.2f} m below the inner surface, the bubble cloud about {pen:.2f} m: gas may "
                    "pass under them to the next compartment and the compartment gain is lost.")
    if a["skirt_m"] is not None and d_skirt < max(pen, d_baf):
        msgs.append(f"The skirt ({d_skirt:.2f} m) is shorter than the bubble cloud or the baffles: bubbles may escape "
                    "under it.")
    # the whole gas cushion sits inside the tank water: the plate a set distance below the tank water level,
    # the inner water surface a cushion height lower. The tank depth only has to fit the box.
    s = a["submergence_m"] if a["submergence_m"] is not None else Lj + a["plate_below_m"]
    fit = None
    if a["water_depth_m"] is not None:
        need_min = s + d_skirt + clear
        fit = {"available_m": a["water_depth_m"], "needed_m": need_min, "ok": a["water_depth_m"] >= need_min - 1e-9,
               "options": []}
        if not fit["ok"]:
            for dd in (0.010, 0.008):  # a smaller nozzle makes a shallower bubble cloud
                pen_d = PEN_C * vi ** 0.775 * dd ** 0.67
                need_d = s + max(0.60, 2.0 * pen_d) + clear
                if dd < d - 1e-9 and need_d <= a["water_depth_m"]:
                    fit["options"].append(f"Nozzle Ø{dd * 1000:.0f} mm: needs {need_d:.2f} m")
                    break
            for dd in (d, 0.010, 0.008):  # less clearance under the skirt (at least 0.15 m), maybe a smaller nozzle
                pen_d = PEN_C * vi ** 0.775 * dd ** 0.67
                room = a["water_depth_m"] - s - max(0.60, 2.0 * pen_d)
                if dd <= d + 1e-9 and 0.15 <= room < clear:
                    fit["options"].append((f"Nozzle Ø{dd * 1000:.0f} mm and " if dd < d - 1e-9 else "")
                                          + f"{room:.2f} m clearance under the skirt")
                    break
            for hh in [x / 100 for x in range(int(H * 100) - 5, 40, -5)]:  # a lower head, slower jets
                vi_h = math.sqrt((Cv * math.sqrt(2 * G * max(hh - Lj, 0.01))) ** 2 + 2 * G * Lj)
                need_h = s + max(0.60, 2.0 * PEN_C * vi_h ** 0.775 * d ** 0.67) + clear
                if need_h <= a["water_depth_m"] and hh - Lj > 0.05:
                    fit["options"].append(f"Head {hh:.2f} m: needs {need_h:.2f} m (less transfer per compartment)")
                    break
            fit["options"].append(f"Set the platform in a pit or a deeper sump {need_min - a['water_depth_m']:.2f} m "
                                  "below the tank floor")
            msgs.append(f"The tank is too shallow: {a['water_depth_m']:.2f} m of water, the platform needs at least "
                        f"{need_min:.2f} m ({s:.2f} m submergence + {d_skirt:.2f} m skirt + {clear:.2f} m to the floor): "
                        + "; ".join(fit["options"]) + ".")
    P = Pb + w.rho * (s + 0.2) / 10.33
    depth_need = s + d_skirt + clear
    if a["E"] is not None:
        E, E_opt = a["E"], None

        def E_at(Hx):
            return a["E"]
    else:
        E = transfer_E(H, a["temp_c"], n_lo=a["low_head_exp"])
        E_opt = transfer_E(H, a["temp_c"], n_lo=1.0) if H < 0.8 else None

        def E_at(Hx):
            return transfer_E(Hx, a["temp_c"], n_lo=a["low_head_exp"])
    Oin = a["o2_in"]
    Nin = a["n2_in_pct"] / 100 * w.N_sat
    Pd = P - w.pw
    pur = a["o2_purity_pct"] / 100.0
    O_max = Oin + E * (w.KO * pur * Pd - Oin)
    O_nv = novent_point(w, Oin, Nin, E, P, n, pur)
    if a["targets"]:
        bad = [t for t in a["targets"] if t <= Oin]
        if bad:
            msgs.append(f"Targets at or below the inlet ({Oin} mg/L) were skipped: {bad}")
        targets = [t for t in a["targets"] if t > Oin]
        if not targets:
            raise JetInputError("All targets are at or below the inlet O₂.")
    else:
        hi = min(O_max - 0.5, Oin + 14)
        start = math.floor(max(O_nv, Oin)) + 1
        targets = [float(t) for t in range(int(start), int(hi) + 1)]
        if len(targets) < 3:  # narrow range: half steps
            lo = math.floor(max(O_nv, Oin) * 2) / 2 + 0.5
            targets = [lo + 0.5 * i for i in range(int((hi - lo) / 0.5) + 1)] or [round(max(O_nv, Oin + 0.5), 1)]
    if H < 0.8 and a["E"] is None:
        msgs.append("Head below 0.8 m: efficiency in a range NOT VERIFIED by measurement. The table uses the cautious "
                    "assumption (NTU ∝ head³); the 'optimistic' column assumes NTU ∝ head. The real value is most "
                    "likely between the two; measure on a prototype and enter E. Reference point: the Sète-2016 LHO "
                    f"course sheet implies E ≈ 0.49 at a 0.71 m head — below even the cautious curve (E = "
                    f"{transfer_E(0.71, a['temp_c'], n_lo=a['low_head_exp']):.2f} here).")
    if H > 1.2:
        msgs.append("Head above 1.2 m: no extra transfer gain assumed. Spending extra head on submergence "
                    "(a deeper installation) is usually more effective.")
    if vi < 3.5:
        msgs.append(f"Impact velocity {vi:.1f} m/s: below 3.5 m/s the jets are weak.")
    if d < 0.010:
        msgs.append("Nozzle smaller than 10 mm: high risk of clogging.")
    if Lj / d < 12:
        msgs.append(f"Jet length / diameter = {Lj / d:.0f}: below 12 gas entrainment is weak.")
    if U > U_max * 1.05:
        msgs.append(f"Downward velocity {U:.3f} m/s above the limit {U_max:.3f} m/s: risk of bubble loss.")
    if lay["aspect"] > 3:
        msgs.append(f"Plate very long (aspect {lay['aspect']:.1f}); consider splitting into several platforms.")
    if a["shape"] != "round" and lay["W"] / n < 0.12:
        msgs.append(f"Compartment width {lay['W'] / n * 1000:.0f} mm: the FOX calibration may not hold in narrow "
                    "compartments; use fewer compartments.")
    if a["salinity"] > 5:
        msgs.append("Salt water: E is calibrated on freshwater data; bubbles are smaller in seawater, the real "
                    "efficiency may differ.")
    if n > 1 and a["E"] is None:
        msgs.append(f"The gain of {n} compartments comes from the model: each compartment is assumed to keep FOX's "
                    "single-compartment E with no gas leakage between compartments; confirm on a prototype.")
    rows = []
    for t in targets:
        r = staged(w, Oin, Nin, t, E, P, n, pur)
        r1 = staged(w, Oin, Nin, t, E, P, 1, pur)
        ro = staged(w, Oin, Nin, t, E_opt, P, n, pur) if E_opt is not None else None
        row = {"target": t, "sat_pct": 100 * t / w.O_sat, "reachable": r is not None,
               "ae_pct": 100 * r["AE"] if r else None, "novent": bool(r and r["novent"]),
               "ae1_pct": 100 * r1["AE"] if r1 else None, "ae_opt_pct": 100 * ro["AE"] if ro else None}
        if r:
            kgh = a["flow_m3h"] * r["feed"] / 1000
            Nout = r["Nout"]
            pO, pN = t / w.KO, Nout / w.KN
            pAr = 0.0093 * (Pb - w.pw) * Nout / w.N_sat
            row.update(o2_kgh=kgh, gas_nlmin=kgh * 1000 / O2_G_PER_L_15C / 60 / pur,
                       vent_nlmin=r["vent_mmol"] * Q * 1000 * 60 * ML_PER_MMOL_15C / 1000,
                       n2_out_pct=100 * Nout / w.N_sat, tgp_pct=100 * (pO + pN + pAr + w.pw) / Pb,
                       kwh_per_kg=2.725 * H * w.rho / (t - Oin) / a["pump_eff"])
        rows.append(row)
        if r and row["tgp_pct"] > 103:
            msgs.append(f"TGP {row['tgp_pct']:.1f}% above 103% at {t:g} mg/L — gas-bubble trauma risk")
    hyd_w = 1000 * w.rho * G * Q * H
    top = H + a["tank_margin_m"]
    valves = None
    if a["flow_min_m3h"] and a["shape"] == "round":
        msgs.append("The gate valve table is for rectangular plates (rows of holes); a round plate is throttled "
                    "per compartment.")
    if a["flow_min_m3h"] and a["shape"] != "round":
        q_row = lay["cols"] * q * 3600
        R = n * lay["rows"]
        h_top = h0 + a["tank_margin_m"]

        def qmax_k(k):
            return k * q_row * math.sqrt(h_top / h0)
        ref_t = targets[min(len(targets) - 1, len(targets) // 2)]
        k_first = next((k for k in range(1, R + 1) if qmax_k(k) >= a["flow_min_m3h"]), None)
        k_last = next((k for k in range(1, R + 1) if qmax_k(k) >= a["flow_m3h"]), None)
        valves = {"q_row": q_row, "ref_target": ref_t, "rows_total": R,
                  "ok": k_first is not None and k_last is not None, "bands": []}
        if valves["ok"]:
            for k in range(k_first, k_last + 1):
                q_lo = a["flow_min_m3h"] if k == k_first else qmax_k(k - 1)
                q_hi = min(a["flow_m3h"], qmax_k(k))
                lvl_lo = h0 * (q_lo / (k * q_row)) ** 2 + Lj
                lvl_hi = h0 * (q_hi / (k * q_row)) ** 2 + Lj
                E_k = E_at(lvl_lo)
                rk = staged(w, Oin, Nin, ref_t, E_k, P, min(k, n), pur)
                valves["bands"].append({"open_rows": k, "q_lo": q_lo, "q_hi": q_hi, "level_lo": lvl_lo,
                                        "level_hi": lvl_hi, "E": E_k, "ae_pct": 100 * rk["AE"] if rk else None,
                                        "some_closed": k < n})
            valves.update(fixed_rows=k_first, gated_rows=R - k_first, spare_rows=R - k_last)
            if k_first < n:
                msgs.append(f"At the lowest flow only {k_first} rows are open for {n} compartments: some compartments "
                            "run dry and the series gas flow breaks down. Choose fewer holes per row (more rows).")
            if any(b["ae_pct"] is None for b in valves["bands"]):
                msgs.append(f"In some flow bands the feed tank level drops so far that {ref_t:.1f} mg/L cannot be reached; "
                            "give the feed tank a margin above the head or use more, smaller rows.")
    return {
        "inputs": a, "msgs": msgs,
        "water": {"O_sat": w.O_sat, "N_sat": w.N_sat, "o2_in_sat_pct": 100 * Oin / w.O_sat},
        "nozzle": {"d_mm": d * 1000, "type": a["nozzle_type"], "Cd": Cd, "Cv": Cv, "q_ls": q * 1000, "q_m3h": q * 3600,
                   "holes_needed": N_req, "holes": lay["holes"], "rows": lay["rows"], "cols": lay["cols"],
                   "pitch_mm": lay["pitch"] * 1000, "W_mm": lay["W"] * 1000, "L_mm": lay["L"] * 1000,
                   "area_m2": lay["area"], "compartment_mm": lay["W"] / n * 1000, "U": U, "U_max": U_max,
                   "screen_mm": d * 1000 / 2,
                   "options": [{"d_mm": dd * 1000, "holes": math.ceil(Q / qcoef(dd)), "jet_ratio": Lj / dd}
                               for dd in (0.010, 0.012, 0.014, 0.016)]},
        "jets": {"h0": h0, "v0": v0, "vi": vi, "jet_ratio": Lj / d, "penetration_m": pen, "baffle_m": d_baf,
                 "skirt_m": d_skirt, "baffle_plate_m": d_baf + Lj, "baffle_given": a["baffle_m"] is not None, "skirt_given": a["skirt_m"] is not None},
        "levels": {"feed": H, "top": top, "weir": top + 0.10, "wall": top + 0.20, "plate": Lj - s,
                   "water_on_plate": H + s - Lj, "inner": -s, "submergence": s, "gas_mbar": s * 98.1 * w.rho,
                   "baffle": -s - d_baf, "skirt": -s - d_skirt, "depth_needed": depth_need,
                   "box_height": top + 0.20 + s + d_skirt, "cushion_l": lay["area"] * Lj * 1000},
        "transfer": {"E": E, "E_opt": E_opt, "E_measured": a["E"] is not None, "P_atm": P, "Pb_atm": Pb, "O_novent": O_nv,
                     "novent": O_nv > Oin + 0.05, "O_max": O_max},
        "rows": rows, "hydraulic_w": hyd_w, "electric_w": hyd_w / a["pump_eff"], "valves": valves,
        "drawing": {**drawing, "d_mm": d * 1000, "n": n, "open": N_open, "cushion_m": Lj, "clear_m": clear},
        "fit": fit,
    }
