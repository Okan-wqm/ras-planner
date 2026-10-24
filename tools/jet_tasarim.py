#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jet platformu oksijenatör - tasarım ve verim hesabı  (sürüm 2)
=============================================================

İnce sarmalayıcı: bütün matematik ras_engine.jet modülünden gelir (tek kaynak);
bu dosya yalnızca komut satırı bayraklarını çözer, sonucu yazdırır ve CSV çıkarır.
Hesabın modeli ve varsayımları ras_engine/jet.py'nin docstring'inde anlatılmıştır;
aşağıdaki özet eskiden burada yaşayan kopyanın hatırası olarak duruyor.

Model (özet)
------------
* Her bölme: gaz fazı iyi karışık, su bölmeden bir kez geçer.
    O2 çıkış = O2 giriş + E * (C*_O2 - O2 giriş),  C* = K_O2 * x_O2 * (P - p_su)
    N2 için E_N2 = 1 - (1 - E)^0.94   (difüzyon oranı)
  Gaz bölmeler arasında seri (1 -> n), su paralel ve eşit dağılır. Tahliye son bölmeden.
* E (bölme başına transfer verimi) FOX France Oxygénation'ın plate-forme à jets föyündeki
  tabloya kalibre edildi: E = 0.725, düşü 1.0 m, batıklık 0.5 m varsayımıyla
  (14 °C, tatlı su, giriş 8 mg/l; tabloyu en fazla 0.8 puan farkla tutturuyor).
* E'nin düzeltmeleri (NTU = -ln(1-E) üzerinden): düşü, sıcaklık (theta = 1.024).
* Etkin gaz basıncı P = 1 + ρ·(batıklık + 0.2 m) / 10.33 m  [atm]
* Aşağı yönlü su hızı sınırı: tatlı suda 0.085 m/s, deniz suyunda 0.050 m/s.
* Nozul: tip seçimine göre (Cd, Cv) = yuvarlak (0.92, 0.95), kısa boru (0.82, 0.82),
  keskin delik (0.62, 0.97).
* Çözünürlük: Weiss (1970) Bunsen katsayıları (tuzluluk düzeltmeli).

Örnekler
--------
  python3 jet_tasarim.py --debi 100
  python3 jet_tasarim.py --debi 100 --debi-min 60 --dusu 1.0 --su-derinligi 1.6
  python3 jet_tasarim.py --debi 180 --dusu 0.6 --tuzluluk 33 --giris-o2 9 --hedef 14 16 18
  python3 jet_tasarim.py --debi 100 --dusu 0.3 --bolme 4 --batiklik 1.0
"""

import argparse
import csv
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # ras_engine'i betik olarak çalıştırırken bul

from ras_engine import jet

NOZUL_EN = {"yuvarlak": "rounded", "kisa-boru": "short-tube", "keskin": "sharp"}


def _design(kw, **override):
    try:
        return jet.design(**kw, **override)
    except jet.JetInputError as exc:  # motorun kapsamlı girdi denetimi
        sys.exit(f"HATA: {exc}")


def main():
    ap = argparse.ArgumentParser(
        description="Jet platformu oksijenatör tasarım hesabı (ön boyutlandırma).",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("Örnekler")[1])
    ap.add_argument("--debi", type=float, required=True, help="tasarım (en yüksek) debi, m3/saat")
    ap.add_argument("--debi-min", type=float, help="en düşük debi, m3/saat (sürgü tablosu için)")
    ap.add_argument("--dusu", type=float, default=1.0,
                    help="besleme haznesi su seviyesi ile çıkış su seviyesi arası, m (vars. 1.0)")
    ap.add_argument("--hazne-payi", type=float, default=0.0,
                    help="hazne seviyesinin düşünün üstüne çıkabileceği pay, m (yalnız pompalı besleme; vars. 0)")
    ap.add_argument("--su-derinligi", type=float,
                    help="kurulum yerinde çıkış su seviyesinin altındaki kullanılabilir derinlik, m; "
                         "verilirse batıklık buradan hesaplanır")
    ap.add_argument("--batiklik", type=float,
                    help="iç su yüzeyinin çıkış su seviyesi altındaki derinliği, m (vars. 0.5)")
    ap.add_argument("--sicaklik", type=float, default=14.0, help="°C (vars. 14)")
    ap.add_argument("--tuzluluk", type=float, default=0.0, help="g/kg (tatlı su 0, deniz ~33-35)")
    ap.add_argument("--giris-o2", type=float, default=8.0, help="giriş çözünmüş O2, mg/l (vars. 8)")
    ap.add_argument("--giris-n2", type=float, default=100.0, help="giriş N2 doygunluğu, %% (vars. 100)")
    ap.add_argument("--hedef", type=float, nargs="*", help="hedef çıkış O2 değerleri, mg/l")
    ap.add_argument("--bolme", type=int, default=4, help="gaz bölmesi sayısı (vars. 4)")
    ap.add_argument("--nozul", type=float, help="nozul çapı, mm (vermezsen otomatik seçilir)")
    ap.add_argument("--nozul-tipi", choices=list(NOZUL_EN), default="yuvarlak",
                    help="yuvarlak = girişi yarıçaplı (r >= 0.2 d); kisa-boru = düz delik, kalın plaka; "
                         "keskin = ince sacda düz delik (vars. yuvarlak)")
    ap.add_argument("--cd", type=float, help="debi katsayısını elle ver (tipi geçersiz kılar)")
    ap.add_argument("--yastik", type=float, default=0.20, help="gaz yastığı yüksekliği, m (vars. 0.20)")
    ap.add_argument("--yedek", type=float, default=10.0, help="yedek delik payı, %% (vars. 10)")
    ap.add_argument("--pompa-verimi", type=float, default=0.45,
                    help="düşü pompayla sağlanıyorsa toplam verim (vars. 0.45; BioMar'ın verdiği "
                         "0.8 kWh/kg ~0.32'ye karşılık gelir)")
    ap.add_argument("--o2-saflik", type=float, default=95.0,
                    help="besleme gazındaki O2, %% (jeneratör ~93-95, sıvı oksijen ~99.5; vars. 95)")
    ap.add_argument("--E", type=float, help="ölçülmüş bölme transfer verimi (0-1); verilirse düzeltme yapılmaz")
    ap.add_argument("--dusu-ussu-alt", type=float, default=3.0,
                    help="0.8 m altı düşü için NTU üssü (vars. 3 = temkinli)")
    ap.add_argument("--csv", help="verim tablosunu bu CSV dosyasına da yaz")
    a = ap.parse_args()

    # --- girdi denetimi
    if a.debi <= 0:
        sys.exit("HATA: --debi sıfırdan büyük olmalı.")
    if a.debi_min is not None and not (0 < a.debi_min <= a.debi):
        sys.exit("HATA: --debi-min 0 ile --debi arasında olmalı.")
    if a.bolme < 1:
        sys.exit("HATA: --bolme en az 1 olmalı.")
    if a.E is not None and not (0 < a.E < 1):
        sys.exit("HATA: --E 0 ile 1 arasında olmalı.")
    if a.yastik <= 0:
        sys.exit("HATA: --yastik sıfırdan büyük olmalı.")
    if not (50 <= a.o2_saflik <= 100):
        sys.exit("HATA: --o2-saflik 50 ile 100 arasında olmalı.")
    if not (0 < a.pompa_verimi <= 1):
        sys.exit("HATA: --pompa-verimi 0 ile 1 arasında olmalı.")

    msgs = []
    Oin, H = a.giris_o2, a.dusu
    pur = a.o2_saflik / 100.0
    kw = dict(flow_m3h=a.debi, flow_min_m3h=a.debi_min, head_m=a.dusu, tank_margin_m=a.hazne_payi,
              temp_c=a.sicaklik, salinity=a.tuzluluk, o2_in=a.giris_o2, n2_in_pct=a.giris_n2,
              compartments=a.bolme, nozzle_mm=a.nozul, nozzle_type=NOZUL_EN[a.nozul_tipi], cd=a.cd,
              cushion_m=a.yastik, spare_pct=a.yedek, pump_eff=a.pompa_verimi,
              o2_purity_pct=a.o2_saflik, E=a.E, low_head_exp=a.dusu_ussu_alt)

    # --- batıklık (CLI anlamı): --batiklık doğrudan; --su-derinligi etek ve taban payını düşer
    if a.batiklik is not None:
        s = max(a.batiklik, 0.0)
    elif a.su_derinligi is not None:
        pre = _design(kw, submergence_m=jet.S_REF, targets=[Oin + 1])  # geometri s'ye bağlı değil
        d_skirt = pre["jets"]["skirt_m"]
        s = a.su_derinligi - d_skirt - 0.30
        if s < 0:
            msgs.append(f"Su derinliği yetersiz: etek ({d_skirt:.2f} m) ve taban payı (0.3 m) için "
                        f"en az {d_skirt + 0.30:.2f} m gerekir. Batıklık 0 alındı; perde ve etek kısaltılırsa "
                        "kabarcık kaçağı riski var.")
            s = 0.0
    else:
        s = jet.S_REF

    # --- hedefler: --hedef ya da CLI'nin eski varsayılan üretimi (O_novent'tan başlar)
    if a.hedef:
        bad = [t for t in a.hedef if t <= Oin]
        if bad:
            msgs.append(f"Girişin ({Oin} mg/l) altındaki/eşitindeki hedefler atlandı: {bad}")
        targets = [t for t in a.hedef if t > Oin]
        if not targets:
            sys.exit("HATA: hedeflerin hepsi giriş O2'sine eşit ya da altında.")
    else:
        pre = _design(kw, submergence_m=s, targets=[Oin + 1])
        tr_pre = pre["transfer"]
        hi = min(tr_pre["O_max"] - 0.5, Oin + 14)
        start = math.floor(max(tr_pre["O_novent"], Oin + 1)) + 1
        targets = [float(t) for t in range(int(start), int(hi) + 1)]
        if not targets:
            targets = [round(max(tr_pre["O_novent"], Oin + 0.5), 1)]

    r = _design(kw, submergence_m=s, targets=targets)
    nz, jt, lv, tr = r["nozzle"], r["jets"], r["levels"], r["transfer"]
    Q = a.debi / 3600.0

    # --- uyarılar (motorunki İngilizce; bu araç eskiden olduğu gibi Türkçesini üretir)
    if H < 0.8 and a.E is None:
        msgs.append("Düşü 0.8 m altında: verim ÖLÇÜMLE DOĞRULANMAMIŞ bölgede. Tablo temkinli varsayımla "
                    "(NTU ~ düşü^3) hesaplandı; 'iyimser' sütunu NTU ~ düşü varsayımıdır. Gerçek değer "
                    "büyük olasılıkla ikisinin arasında; prototipte ölç ve --E ile gir.")
    if H > 1.2:
        msgs.append("Düşü 1.2 m üstünde: ek transfer kazancı varsayılmadı. Fazla düşüyü batıklığa "
                    "(daha derin kurulum) harcamak genelde daha verimli.")
    if jt["vi"] < 3.5:
        msgs.append(f"Çarpma hızı {jt['vi']:.1f} m/s: 3.5 m/s altında jetler zayıf kalır.")
    if nz["d_mm"] < 10:
        msgs.append("Nozul 10 mm'den küçük: tıkanma riski yüksek.")
    if jt["jet_ratio"] < 12:
        msgs.append(f"Jet boyu/çap = {jt['jet_ratio']:.0f}: 12'nin altında gaz sürükleme zayıflar.")
    if nz["U"] > nz["U_max"] * 1.05:
        msgs.append(f"Aşağı yönlü hız {nz['U']:.3f} m/s, sınır {nz['U_max']:.3f} m/s: kabarcık kaçağı riski.")
    aspect = max(nz["W_mm"], nz["L_mm"]) / min(nz["W_mm"], nz["L_mm"])
    if aspect > 3:
        msgs.append(f"Plaka çok uzun (en/boy {aspect:.1f}); birden fazla platforma bölmeyi düşün.")
    if nz["compartment_mm"] / 1000 < 0.12:
        msgs.append(f"Bölme genişliği {nz['compartment_mm']:.0f} mm: dar bölmelerde FOX kalibrasyonu "
                    "geçerli olmayabilir; bölme sayısını azalt.")
    if a.tuzluluk > 5:
        msgs.append("Tuzlu su: E tatlı su verisine kalibre; deniz suyunda kabarcıklar küçülür, gerçek verim "
                    "farklı olabilir.")
    if a.bolme > 1 and a.E is None:
        msgs.append(f"{a.bolme} bölme kazancı modelden: her bölmenin FOX'un tek bölmeli E'sini koruduğu ve "
                    "bölmeler arasında gaz kaçağı olmadığı varsayıldı; prototipte doğrula.")

    # ================================================================ ÇIKTI
    pr = print
    pr("=" * 78)
    pr("JET PLATFORMU OKSİJENATÖR - ÖN BOYUTLANDIRMA")
    pr("=" * 78)
    pr(f"Debi {a.debi:.0f} m3/saat ({Q * 1000:.1f} l/s) | düşü {H:.2f} m | {a.sicaklik:.0f} °C | "
       f"tuzluluk {a.tuzluluk:.0f} g/kg")
    pr(f"Giriş O2 {Oin:.1f} mg/l ({r['water']['o2_in_sat_pct']:.0f}% doygunluk) | giriş N2 {a.giris_n2:.0f}% | "
       f"hava doygunluğu: O2 {r['water']['O_sat']:.2f}, N2 {r['water']['N_sat']:.2f} mg/l")

    pr("\n--- Nozul ve plaka")
    pr(f"Nozul                   : Ø{nz['d_mm']:.0f} mm, {a.nozul_tipi if a.cd is None else 'elle Cd'} "
       f"(Cd {nz['Cd']:.2f}, Cv {nz['Cv']:.2f})")
    pr(f"Delik başına debi       : {nz['q_ls']:.3f} l/s = {nz['q_m3h']:.2f} m3/saat")
    pr(f"Gerekli delik           : {nz['holes_needed']:.0f}  ->  plakada {nz['holes']} delik "
       f"(%{a.yedek:.0f}+ yedek tapalı)")
    pr(f"Yerleşim                : {a.bolme} bölme x {nz['rows']} sıra x {nz['cols']} delik, "
       f"aralık {nz['pitch_mm']:.0f} mm")
    pr(f"Plaka                   : {nz['W_mm']:.0f} x {nz['L_mm']:.0f} mm = {nz['area_m2']:.3f} m2 "
       f"(bölme genişliği {nz['compartment_mm']:.0f} mm)")
    pr(f"Aşağı yönlü su hızı     : {nz['U']:.3f} m/s (sınır {nz['U_max']:.3f})")
    pr(f"Elek                    : en fazla {nz['screen_mm']:.0f} mm göz")
    pr("Çap seçenekleri         : " + " | ".join(
        f"{o['d_mm']:.0f} mm: {o['holes']} delik, jet/çap {o['jet_ratio']:.0f}"
        for o in nz["options"]))

    pr("\n--- Jetler")
    pr(f"Delik yükü              : {jt['h0']:.2f} m (= düşü - yastık)")
    pr(f"Nozul çıkış hızı        : {jt['v0']:.2f} m/s")
    pr(f"Yüzeye çarpma hızı      : {jt['vi']:.2f} m/s")
    pr(f"Jet boyu / çap          : {jt['jet_ratio']:.0f}")
    pr(f"Kabarcık bulutu derinliği: ~{jt['penetration_m']:.2f} m (patent gözlemine oturtulmuş tahmin)")

    pr("\n--- Kotlar (çıkış su seviyesi = 0.00 m)")
    pr(f"Besleme suyu seviyesi   : +{lv['feed']:.2f} m" +
       (f" (en fazla +{lv['top']:.2f})" if a.hazne_payi > 0 else "") +
       f"  | taşma savağı +{lv['weir']:.2f}, duvar üstü +{lv['wall']:.2f}")
    pr(f"Nozul plakası (alt yüz) : {lv['plate']:+.2f} m   -> plaka üstünde {lv['water_on_plate']:.2f} m su")
    pr(f"İç su yüzeyi / tahliye  : {lv['inner']:+.2f} m   (batıklık {lv['submergence']:.2f} m, "
       f"gaz {lv['gas_mbar']:.0f} mbar)")
    pr(f"Bölme perdesi alt ucu   : {lv['baffle']:+.2f} m")
    pr(f"Etek alt ucu            : {lv['skirt']:+.2f} m")
    pr(f"Gereken su derinliği    : {lv['depth_needed']:.2f} m  | kutu yüksekliği ~{lv['box_height']:.2f} m")
    pr(f"Gaz yastığı hacmi       : {lv['cushion_l']:.0f} l  (ilk dolumda 2-3 kat O2 debisi ver)")

    pr("\n--- Gaz transferi")
    src = "ölçülmüş (--E)" if a.E is not None else f"FOX kalibrasyonu, düşü ve {a.sicaklik:.0f} °C düzeltmeli"
    pr(f"Bölme transfer verimi E : {tr['E']:.3f}  [{src}]" +
       (f"  | iyimser: {tr['E_opt']:.3f}" if tr["E_opt"] is not None else ""))
    pr(f"Etkin gaz basıncı       : {tr['P_atm']:.3f} atm  | besleme gazı %{a.o2_saflik:.1f} O2")
    pr(f"Tahliyesiz çalışma      : çıkış ~{tr['O_novent']:.1f} mg/l'ye kadar verim ~%100 "
       f"(O2'yi yastık seviyesiyle besle" + (", inert gaz suya çözünür)" if pur < 0.9999 else ")")
       if tr["O_novent"] > Oin + 0.05 else
       "Tahliyesiz çalışma      : yok (giriş azotu doygunluğun üstünde)")
    pr(f"Teorik tavan            : {tr['O_max']:.1f} mg/l (yastık besleme gazıyla dolu, verim -> 0)")

    hdr = ["Hedef mg/l", "Doygunluk %", f"Verim % ({a.bolme} b.)", "Verim % (1 b.)"]
    if tr["E_opt"] is not None:
        hdr.append(f"İyimser % ({a.bolme} b.)")
    hdr += ["O2 kg/saat", "Gaz Nl/dk", "Tahliye Nl/dk", "N2 çıkış %", "TGP %", "kWh/kg (pompa)"]
    rows = []
    for x in r["rows"]:
        row = [f"{x['target']:.1f}", f"{x['sat_pct']:.0f}"]
        if not x["reachable"]:
            row += ["ulaşılamaz", "-"]
            if tr["E_opt"] is not None:
                row.append(f"{x['ae_opt_pct']:.1f}" if x["ae_opt_pct"] is not None else "ulaşılamaz")
            row += ["-"] * 6
        else:
            row += [f"{x['ae_pct']:.1f}" + (" *" if x["novent"] else ""),
                    f"{x['ae1_pct']:.1f}" if x["ae1_pct"] is not None else "ulaşılamaz"]
            if tr["E_opt"] is not None:
                row.append(f"{x['ae_opt_pct']:.1f}" if x["ae_opt_pct"] is not None else "ulaşılamaz")
            row += [f"{x['o2_kgh']:.2f}", f"{x['gas_nlmin']:.1f}", f"{x['vent_nlmin']:.2f}",
                    f"{x['n2_out_pct']:.0f}", f"{x['tgp_pct']:.1f}", f"{x['kwh_per_kg']:.2f}"]
        rows.append(row)

    widths = [max(len(h), max(len(rw[i]) for rw in rows)) for i, h in enumerate(hdr)]
    pr("\n" + "  ".join(h.rjust(wd) for h, wd in zip(hdr, widths)))
    for rw in rows:
        pr("  ".join(c.rjust(wd) for c, wd in zip(rw, widths)))
    pr("(* = tahliyesiz bölge; Verim = çözünen O2 / beslenen O2; Nl/dk = 15 °C, 1 atm kuru gaz)")
    pr(f"Hidrolik güç (düşü pompalanırsa): {r['hydraulic_w']:.0f} W, elektrik ~{r['electric_w']:.0f} W "
       f"(toplam pompa verimi {a.pompa_verimi:.2f})")

    # --- sürgü tablosu: her debi için hazneyi taşırmayan en az sayıda sıra açılır
    if a.debi_min:
        v = r["valves"]
        pr("\n--- Debi aralığı için sürgü ayarları")
        pr(f"Sıra başına {nz['cols']} delik = {v['q_row']:.1f} m3/saat (delik yükü {jt['h0']:.2f} m). "
           f"Hazne en fazla +{lv['top']:.2f} m'ye çıkar; seviye düştükçe jetler zayıflar.")
        pr(f"Verim sütunu: {v['ref_target']:.1f} mg/l hedefinde, bandın en düşük hazne seviyesinde.")
        if not v["ok"]:
            pr("UYARI: plakadaki delikler bu debiyi geçiremiyor.")
        else:
            pr(f"{'Açık sıra':>9}  {'Debi bandı m3/saat':>19}  {'Hazne seviyesi m':>17}  {'E':>5}  {'Verim %':>7}")
            for b in v["bands"]:
                note = "  (bazı bölmeler kapalı)" if b["some_closed"] else ""
                ae = f"{b['ae_pct']:.1f}" if b["ae_pct"] is not None else "-"
                pr(f"{b['open_rows']:>9}  {b['q_lo']:>8.0f} - {b['q_hi']:<8.0f}  "
                   f"{b['level_lo']:>+7.2f} .. {b['level_hi']:<+7.2f}  {b['E']:>5.2f}  {ae:>7}{note}")
            pr(f"Sabit açık sıra: {v['fixed_rows']}, sürgülü: {v['gated_rows']} (bunlardan {v['spare_rows']} yedek).")
            if v["fixed_rows"] < a.bolme:
                pr(f"UYARI: en düşük debide {v['fixed_rows']} sıra açık, bölme sayısı {a.bolme}: "
                   "bazı bölmeler susuz kalır ve seri gaz akışı bozulur. Sıra başına daha az delik "
                   "(daha çok sıra) seç.")
            pr("Sürgüleri son bölmeden başlayarak kapat; bölmeler arasındaki su payı eşitsizleşir "
               "(model eşit dağılım varsayar).")

    if msgs:
        pr("\n--- Uyarılar")
        for m in msgs:
            pr("- " + m)

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            wr = csv.writer(fh)
            wr.writerow(hdr)
            wr.writerows(rows)
        pr(f"\nTablo yazıldı: {a.csv}")


if __name__ == "__main__":
    main()
