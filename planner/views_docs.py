"""OPEX page, company profile and client proposals (HTML document + PDF)."""
import base64
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib import messages
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from ras_engine.opex import DEFAULT_PRICES, PAYROLL_GROUPS, VARIABLE_GROUPS, annual_opex, ramped_cashflow
from ras_engine.process import DiagramError, solve as solve_process

from . import proposal_defaults as D
from .ai import PROVIDERS, AIError, generate
from .models import AISettings, CompanyProfile, Proposal, Scenario
from .services import diagram_results, plan_result
from ras_engine.compare import compare, summary_status, unit_targets
from .views import (_backup_json, _capex_data, _equipment_units, _float_or_none, _has_diagram, _with_basis)

SNAPSHOT_DIR = Path(settings.BASE_DIR) / "cache" / "snapshots"


class _Keep(dict):
    """format_map that leaves unknown {placeholders} as they are."""
    def __missing__(self, key):
        return "{" + key + "}"


def _fill(text, values):
    return (text or "").format_map(_Keep(values))


# ---------- OPEX ----------

def _diagram_info(scenario, n_stages, res=None):
    """Per RAS unit with a process diagram, solved on its busiest day with the plan's fish: running pumps'
    power and flow, chiller power, make-up water, oxygen, ozone and sludge."""
    out = {}
    for i in range(n_stages):
        if not _has_diagram(scenario, i):
            continue
        r = diagram_results(_with_basis(scenario, i, res))
        if r is None:
            continue
        tot = r["totals"]
        out[i] = {"pump_kw": r["pump_power_kw"], "chiller_kw": r["chiller_power_kw"],
                  "makeup_m3h": r["makeup_m3h"], "pumped_m3h": tot["recirculated_m3h"],
                  "pure_o2_kgd": tot["pure_o2_kgd"], "ozone_kgd": tot["ozone_kgd"],
                  "sludge_tss_kgd": tot["sludge_tss_kgd"]}
    return out


def opex_data(scenario, res, capex_total=None):
    if capex_total is None:
        capex_total = _capex_data(scenario, res)["grand_total"]
    params = scenario.engine_params()
    return annual_opex(res, scenario.opex or {}, _diagram_info(scenario, len(res["stages"]), res), capex_total,
                       design_o2_per_feed=params.design.o2_per_feed,
                       diffuser_efficiency=params.gas.diffuser_efficiency,
                       design_params=params.design, water_temp=params.growth.temperature)


def batch_options(scenario, res, capex_total, current):
    """The same farm run with 2, 3 … 6 batches a year (and the current number): OPEX, cost per batch and per
    fish, revenue and profit. Quantities typed as yearly totals stay as typed."""
    saved = scenario.opex or {}
    diagrams = _diagram_info(scenario, len(res["stages"]), res)
    plan_n = round(365 / res["interval_days"], 2) if res.get("interval_days") else None
    rows = []
    options = {2, 3, 4, 5, 6, *([plan_n] if plan_n else [])}
    options = {n for n in options if abs(n - current) >= 0.005} | {current}
    for n in sorted(options):
        o = annual_opex(res, {**saved, "prices": {**(saved.get("prices") or {}), "batches_per_year": n}},
                        diagrams, capex_total)
        rows.append({"n": n, "current": abs(n - current) < 0.005, "total": o["total"], "per_batch": o["cost_per_batch"],
                     "per_fish": o["cost_per_piece"], "fish": o["harvest_pcs"], "revenue": o["revenue"],
                     "profit": o["profit"], "per_fish_full": o["full_cost_per_piece"]})
    return rows


def _parse_opex(post):
    """Every input on the OPEX page; empty = use the calculated or default value."""
    prices = {k: _float_or_none(post.get(k, "")) for k in DEFAULT_PRICES}
    out = {"prices": {k: v for k, v in prices.items() if v is not None},
           "feed_prices": {}, "line_prices": {}, "qty": {}, "units": {}, "extras": []}
    for key, value in post.items():
        v = _float_or_none(value)
        if v is None:
            continue
        if key.startswith("fp:"):
            out["feed_prices"][key[3:]] = v
        elif key.startswith("q:"):
            out["qty"][key[2:]] = v
        elif key.startswith("pr:"):
            out["line_prices"][key[3:]] = v
        elif key.startswith(("head:", "eff:")):
            kind, idx = key.split(":", 1)
            out["units"].setdefault(idx, {})["head_m" if kind == "head" else "eff_pct"] = v
    for name, group, qty, unit, price in zip(post.getlist("x_name"), post.getlist("x_group"), post.getlist("x_qty"),
                                             post.getlist("x_unit"), post.getlist("x_price")):
        if name.strip():
            q = _float_or_none(qty)
            out["extras"].append({"name": name.strip(), "group": group or "Other", "qty": 1.0 if q is None else q,
                                  "unit": unit.strip(), "unit_price": _float_or_none(price)})
    return out


OPEX_SECTIONS = [("Variable costs", VARIABLE_GROUPS), ("Operational expenses — payroll", PAYROLL_GROUPS)]


def opex(request, pk):
    scenario = get_object_or_404(Scenario, pk=pk)
    if request.method == "POST":
        scenario.opex = _parse_opex(request.POST)
        scenario.save()
        messages.success(request, "OPEX saved.")
        return redirect("opex", pk=pk)
    ctx = {"scenario": scenario, "scenarios": Scenario.objects.all()}
    got, err = plan_result(scenario)
    if err:
        ctx["error"] = err
        return render(request, "planner/opex.html", ctx)
    plan, res = got
    capex = _capex_data(scenario, res)
    o = opex_data(scenario, res, capex["grand_total"])
    saved = scenario.opex or {}
    sp = saved.get("prices") or {}
    ctx["batch_rows"] = batch_options(scenario, res, capex["grand_total"], o["batches_per_year"])
    sections = []
    for title, names in OPEX_SECTIONS:
        groups = [{"name": g, "total": o["by_group"].get(g, 0.0),
                   "lines": [ln for ln in o["lines"] if ln["group"] == g and not ln["key"].startswith("x:")]}
                  for g in names]
        sections.append({"title": title, "groups": [g for g in groups if g["lines"]],
                         "total": sum(g["total"] for g in groups)})
    top = max(o["by_group"].values()) if o["total"] > 0 else 1
    # the ramp card: revenue at the plan's real harvest dates, the page's own sale price and discount rate
    ctx["ramped"] = ramped_cashflow(res, o["total"], capex["grand_total"], o["prices"]["sale_price"],
                                    o["prices"]["discount_pct"])
    ctx["payroll_share"] = o["payroll"] / o["total"] * 100 if o["total"] else None
    ctx.update({
        "o": o, "interval": res.get("interval_days"), "capex_total": capex["grand_total"], "currency": capex["currency"], "sections": sections,
        "saved_qty": saved.get("qty") or {}, "saved_prices": sp, "saved_feed": saved.get("feed_prices") or {},
        "extras": saved.get("extras") or [], "group_names": VARIABLE_GROUPS[:-1], "defaults": DEFAULT_PRICES,
        "by_group": [(g, v, v / top * 100 if top else 0) for g, v in o["by_group"].items()],
        "brief_batches": (scenario.brief or {}).get("batches_per_year"),
    })
    return render(request, "planner/opex.html", ctx)


# ---------- process snapshots (the drawn diagram as a picture) ----------

def _browser_job(fn):
    """Run a Playwright job in a worker thread (keeps its event loop away from Django's thread)."""
    def job():
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(channel="msedge", headless=True)
            except Exception:  # no Edge: Playwright's own Chromium
                browser = pw.chromium.launch(headless=True)
            try:
                return fn(browser)
            finally:
                browser.close()
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(job).result(timeout=180)


def process_snapshot(request, pk, index):
    scenario = get_object_or_404(Scenario, pk=pk)
    diagram = scenario.diagram_of(index)
    if not diagram or not diagram.get("nodes"):
        raise Http404("This RAS unit has no process diagram.")
    key = hashlib.sha1(json.dumps(diagram, sort_keys=True).encode()).hexdigest()[:16]
    path = SNAPSHOT_DIR / f"{pk}-{index}-{key}.png"
    if not path.exists():
        url = request.build_absolute_uri(reverse("process", args=[pk]) + f"?ras={index}&print=1")

        def shoot(browser):
            page = browser.new_page(viewport={"width": 1700, "height": 1100}, device_scale_factor=2, color_scheme="light")
            page.goto(url)
            page.wait_for_selector("body[data-ready='1']", timeout=30000)
            page.wait_for_timeout(500)
            # crop to the drawing: union of the equipment and pipe boxes, plus a margin
            box = page.evaluate("""() => {
                const els = [...document.querySelectorAll('.react-flow__node, .react-flow__edge path, .pipe-label')];
                const r = els.map(e => e.getBoundingClientRect()).filter(b => b.width || b.height);
                if (!r.length) return null;
                const x0 = Math.min(...r.map(b => b.left)), y0 = Math.min(...r.map(b => b.top));
                const x1 = Math.max(...r.map(b => b.right)), y1 = Math.max(...r.map(b => b.bottom));
                return {x: Math.max(x0 - 16, 0), y: Math.max(y0 - 16, 0),
                        width: Math.min(x1 - x0 + 32, innerWidth), height: Math.min(y1 - y0 + 32, innerHeight)};
            }""")
            return page.screenshot(clip=box) if box else page.locator(".react-flow").screenshot()
        png = _browser_job(shoot)
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        for old in SNAPSHOT_DIR.glob(f"{pk}-{index}-*.png"):
            old.unlink(missing_ok=True)
        path.write_bytes(png)
    return FileResponse(open(path, "rb"), content_type="image/png")


# ---------- company profile ----------

class CompanyForm(forms.ModelForm):
    logo_file = forms.FileField(label="Logo (PNG, JPG or SVG)", required=False)
    remove_logo = forms.BooleanField(label="Remove logo", required=False)

    class Meta:
        model = CompanyProfile
        exclude = ["logo"]
        widgets = {"address": forms.Textarea(attrs={"rows": 3})}


def company(request):
    profile = CompanyProfile.get()
    form = CompanyForm(request.POST or None, request.FILES or None, instance=profile)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        f = form.cleaned_data.get("logo_file")
        if form.cleaned_data.get("remove_logo"):
            obj.logo = ""
        elif f:
            mime = f.content_type or "image/png"
            obj.logo = f"data:{mime};base64,{base64.b64encode(f.read()).decode()}"
        obj.save()
        messages.success(request, "Company profile saved.")
        return redirect("company")
    return render(request, "planner/company.html", {"form": form, "profile": profile,
                                                    "scenarios": Scenario.objects.all()})


# ---------- proposals ----------

class ProposalForm(forms.ModelForm):
    class Meta:
        model = Proposal
        fields = ["scenario", "number", "title", "date", "valid_until", "currency", "vat_pct",
                  "customer_name", "customer_address", "customer_contact", "customer_email", "customer_phone",
                  "customer_reg", "intro", "terms", "sizing_note", "lump_sum", "payment"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "valid_until": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "customer_address": forms.Textarea(attrs={"rows": 3}),
            "intro": forms.Textarea(attrs={"rows": 6, "placeholder": "Leave empty to write it from the scenario"}),
            "terms": forms.Textarea(attrs={"rows": 9}),
            "payment": forms.Textarea(attrs={"rows": 7}),
            "sizing_note": forms.Textarea(attrs={"rows": 3}),
        }


def _next_number():
    year = date.today().year
    n = Proposal.objects.filter(number__startswith=f"{year}-").count() + 1
    return f"{year}-{n:03d}"


def proposals(request):
    if request.method == "POST":  # new proposal with the standard texts
        sc = Scenario.objects.filter(pk=request.POST.get("scenario")).first() or Scenario.objects.first()
        today = date.today()
        p = Proposal.objects.create(
            scenario=sc, number=_next_number(), title=f"RAS facility design — {sc.name}" if sc else "RAS facility design",
            date=today, valid_until=today + timedelta(days=30), currency=(sc.capex or {}).get("currency", "EUR") if sc else "EUR",
            terms=D.TERMS, payment=D.PAYMENT,
            work_packages=[{"name": n, "notes": t, "price": None} for n, t in D.WORK_PACKAGES],
            clauses=[{"title": t, "text": x} for t, x in D.CLAUSES],
            sections={k: True for k, _ in D.SECTIONS})
        return redirect("proposal_edit", pid=p.pk)
    return render(request, "planner/proposals.html", {
        "proposals": Proposal.objects.select_related("scenario"), "scenarios": Scenario.objects.all(),
        "company": CompanyProfile.get()})


def proposal_edit(request, pid):
    p = get_object_or_404(Proposal, pk=pid)
    form = ProposalForm(request.POST or None, instance=p)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.work_packages = [{"name": n.strip(), "notes": t.strip(), "price": _float_or_none(pr)}
                             for n, t, pr in zip(request.POST.getlist("wp_name"), request.POST.getlist("wp_notes"),
                                                 request.POST.getlist("wp_price")) if n.strip()]
        obj.clauses = [{"title": t.strip(), "text": x.strip()}
                       for t, x in zip(request.POST.getlist("cl_title"), request.POST.getlist("cl_text")) if t.strip()]
        obj.sections = {k: (k in request.POST.getlist("section")) for k, _ in D.SECTIONS}
        obj.save()
        messages.success(request, "Proposal saved.")
        if request.POST.get("then") == "pdf":
            return redirect("proposal_pdf", pid=pid)
        return redirect("proposal_edit", pid=pid)
    ai = AISettings.get()
    scenario_changed = False
    if p.scenario and p.snapshot and p.snapshot.get("generated_at"):
        try:  # the scenario moved on after the PDF was made: its figures are stale
            scenario_changed = p.scenario.updated > datetime.fromisoformat(p.snapshot["generated_at"])
        except ValueError:
            pass
    return render(request, "planner/proposal_form.html", {
        "preflight": _preflight(p),
        "p": p, "form": form, "ai_providers": [(k, PROVIDERS[k]["label"]) for k in ai.configured()],
        "ai_default": ai.provider, "sections": [(k, label, p.sections.get(k, True)) for k, label in D.SECTIONS],
        "scenario_changed": scenario_changed,
        "scenarios": Scenario.objects.all(), "scenario": p.scenario})


def _preflight(p):
    """What should be in place before the PDF goes to the customer: (label, ok, hint)."""
    company = CompanyProfile.get()
    priced = [w for w in p.work_packages if w.get("price") is not None]
    fee = p.lump_sum if p.lump_sum is not None else sum(w["price"] for w in priced)
    checks = [("Customer named", bool(p.customer_name), "Customer section above"),
              ("Design fee set", bool(fee), "Pricing: package prices or one total fee"),
              ("Company address and e-mail", bool(company.address and company.email), "Library › Company profile"),
              ("Company logo", bool(company.logo), "Library › Company profile")]
    sc = p.scenario
    if sc:
        from .services import plan_cached, step_status
        status = step_status(sc)
        pr = plan_cached(sc)
        capex_total = _capex_data(sc, pr[1])["grand_total"] if pr else 0
        checks += [("Production plan calculates", pr is not None, "Step 02"),
                   ("CAPEX has prices", capex_total > 0, "Step 07 CAPEX"),
                   ("Every RAS unit drawn and meeting its requirements", status.get("05") == "ok",
                    "Step 05 Process design: performance panel")]
    return checks


def proposal_delete(request, pid):
    p = get_object_or_404(Proposal, pk=pid)
    if request.method == "POST":
        _backup_json(f"proposal_{p.pk}", {
            "number": p.number, "title": p.title, "date": p.date.isoformat(),
            "valid_until": p.valid_until.isoformat() if p.valid_until else None,
            "currency": p.currency, "vat_pct": p.vat_pct, "customer_name": p.customer_name,
            "customer_address": p.customer_address, "customer_contact": p.customer_contact,
            "customer_email": p.customer_email, "customer_phone": p.customer_phone, "customer_reg": p.customer_reg,
            "intro": p.intro, "terms": p.terms, "payment": p.payment, "sizing_note": p.sizing_note,
            "work_packages": p.work_packages, "lump_sum": p.lump_sum, "clauses": p.clauses,
            "sections": p.sections, "scenario_pk": p.scenario.pk if p.scenario else None,
            "snapshot": p.snapshot})
        p.delete()
        messages.success(request, f"Proposal {p.number} deleted.")
    return redirect("proposals")


def proposal_copy(request, pid):
    p = get_object_or_404(Proposal, pk=pid)
    if request.method == "POST":
        p.pk = None
        p.number = _next_number()
        p.date = date.today()
        p.save()
        return redirect("proposal_edit", pid=p.pk)
    return redirect("proposals")


def _gantt(res, limit=24):
    horizon = res["horizon"]
    rows = []
    for b in res["batches"][:limit]:
        rows.append({"label": f"B{b['batch']}", "bars": [
            {"x": s["start"] / horizon * 100, "w": max((s["end"] - s["start"] + 1) / horizon * 100, 0.3),
             "stage": s["stage"]} for s in b["segments"]]})
    return rows


def proposal_context(p, request):
    company = CompanyProfile.get()
    values = {"company": company.name, "customer": p.customer_name or "the customer", "currency": p.currency,
              "valid_until": f"{p.valid_until.day} {p.valid_until:%B %Y}" if p.valid_until else "the date agreed"}
    on = {k: p.sections.get(k, True) for k, _ in D.SECTIONS}
    ctx = {"p": p, "company": company, "error": None,
           "terms": _fill(p.terms, values), "payment": _fill(p.payment, values),
           "clauses": [{"title": c["title"], "text": _fill(c["text"], values)} for c in p.clauses]}
    # pricing: per package or one design fee
    priced = [w for w in p.work_packages if w.get("price") is not None]
    total = p.lump_sum if p.lump_sum is not None else sum(w["price"] for w in priced)
    ctx.update(packages=p.work_packages, package_prices=bool(priced), fee_total=total,
               vat=total * p.vat_pct / 100 if p.vat_pct else None,
               fee_incl_vat=total * (1 + p.vat_pct / 100) if p.vat_pct else total)

    sc = p.scenario
    plan, res = None, None
    if sc:
        from .schema import CURRENT, upgrade  # lazy import, like views._current (off the import cycle)
        if sc.schema_version != CURRENT:
            import logging
            for note in upgrade(sc):  # stamps the row; blobs are never touched (DG-1)
                logging.getLogger("planner").warning("scenario %s (%s): %s", sc.pk, sc.name, note)
        got, err = plan_result(sc)
        if err:
            ctx["error"] = f"The production plan of '{sc.name}' could not be calculated: {err}"
        else:
            plan, res = got
    if res:
        units, _occ, _water = _equipment_units(sc, res)
        sizing = []
        for u, d in zip(units, res["designs"]):
            if d["empty"]:
                continue
            tank = u.get("tank") or {}
            peak = d["peak"]
            bio = d.get("biofilter") or {}
            drawn = diagram_results(_with_basis(sc, u["index"], res)) if _has_diagram(sc, u["index"]) else None
            sizing.append({
                "flow_src": "diagram" if drawn else "plan",
                "name": d["stage"].name, "w_in": d["weight_in"], "w_out": d["weight_out"], "days": d["days_in_stage"],
                "tanks": tank.get("total_tanks") or d["stage"].n_tanks,
                "tank_m3": tank.get("tank_volume") or tank.get("required_tank_volume") or d["tank_volume_m3"],
                "biomass_t": peak["biomass_kg"] / 1000, "volume_m3": peak["volume_m3"],
                # the recirculation the process diagram decides; the plan's peak design flow until one is drawn
                "flow": drawn["totals"]["recirculated_m3h"] if drawn else
                        (max(d.get("q_unit_m3h") or 0.0, peak["q_design_m3h"]) if peak["q_design_m3h"] != float("inf") else None),
                "feed": peak["feed_kg"], "biofilter_m3": bio.get("volume_fresh_m3") if bio.get("feasible") else None,
                "temp": d["temperature"], "oxygen": "Pure oxygen" if d["pure_oxygen"] else "Aeration"})
        capex = _capex_data(sc, res)
        o = opex_data(sc, res, capex["grand_total"])
        harvests = res["harvests"]
        process = []
        for i, st in enumerate(res["stages"]):
            if not _has_diagram(sc, i):
                continue
            r = diagram_results(_with_basis(sc, i, res))
            nodes = (sc.diagram_of(i) or {}).get("nodes") or []
            checks = compare(unit_targets(res["designs"][i], (sc.equipment or {}).get("water") or "fresh"), r) if r else []
            process.append({"index": i, "name": st.name, "items": len(nodes), "checks": checks,
                            "status": summary_status(checks) if r else "none",
                            "img": reverse("process_snapshot", args=[sc.pk, i]),
                            "makeup": r and r["makeup_m3h"], "pump_kw": r and r["pump_power_kw"],
                            "cooling": r and r["cooling_kw"],
                            "flow": r and r["totals"]["recirculated_m3h"]})
        by_unit = [(name, v) for name, v in capex["by_stage"]]
        auto_intro = (
            f"This document sets out the proposal of {company.name} to {values['customer']} for the project “{p.title}”. "
            f"The facility is planned as {len(res['stages'])} recirculating aquaculture system (RAS) unit"
            f"{'s' if len(res['stages']) != 1 else ''} ({', '.join(s.name for s in res['stages'])}). A new batch is stocked "
            f"every {plan.interval_days} days and fish are harvested at {res['stages'][-1].max_weight_g:g} g, "
            f"{res['harvest_age']} days after stocking"
            + (f"; this gives about {res['annual_harvest_t']:.1f} t of fish per year." if res["annual_harvest_t"] else ".")
        )
        ctx.update(res=res, plan=plan, sizing=sizing, capex=capex, o=o, process=process, by_unit=by_unit,
                   harvests=harvests[:12], more_harvests=max(len(harvests) - 12, 0), gantt=_gantt(res),
                   gantt_more=max(len(res["batches"]) - 24, 0), auto_intro=auto_intro,
                   stage_names=[s.name for s in res["stages"]],
                   batches=len(res["batches"]))
    elif p.scenario is None and p.snapshot:
        # the scenario is gone; the archived figures keep the proposal presentable
        snap = p.snapshot or {}
        ctx["from_snapshot"] = True
        try:
            ctx["snapshot_at"] = datetime.fromisoformat(snap.get("generated_at") or "")
        except ValueError:
            ctx["snapshot_at"] = None
        if snap.get("sizing"):
            ctx["sizing"] = snap["sizing"]
        cap = snap.get("capex") or {}
        if cap:
            ctx["capex"] = {"grand_total": cap.get("grand_total") or 0,
                            "currency": cap.get("currency") or p.currency,
                            "by_group": [tuple(g) for g in cap.get("by_group") or []],
                            "per_tonne": None}
            ctx["by_unit"] = [tuple(s) for s in cap.get("by_stage") or []]
        if snap.get("opex_total") is not None:
            ctx["o"] = {"total": snap["opex_total"], "lines": [], "scaled": False, "harvest_kg": None,
                        "cost_per_kg": None, "full_cost_per_kg": None, "kwh": None, "revenue": None}
    # numbered sections: only those included (and with something to show)
    have = bool(res) or bool(ctx.get("from_snapshot"))
    available = {"sizing": have, "plan": bool(res), "process": bool(res and ctx.get("process")),
                 "capex": have, "opex": have, "clauses": bool(ctx["clauses"])}
    numbered, n = [], 0
    for k, label in D.SECTIONS:
        if on[k] and available.get(k, True):
            n += 1
            numbered.append((k, n, label))
    ctx["toc"] = numbered
    ctx["num"] = {k: n for k, n, _ in numbered}
    ctx["intro"] = p.intro or ctx.get("auto_intro") or ""
    return ctx


def proposal_doc(request, pid):
    p = get_object_or_404(Proposal, pk=pid)
    ctx = proposal_context(p, request)
    ctx["pdf"] = bool(request.GET.get("pdf"))
    ctx["draft"] = bool(request.GET.get("draft"))  # set by the PDF maker when the preflight fails
    return render(request, "planner/proposal_doc.html", ctx)


def _snapshot_from_ctx(ctx):
    """The figures the document showed when its PDF was made, kept on the proposal so it survives its
    scenario. Everything is defensive: a context without a plan yields the metadata alone."""
    p = ctx["p"]
    res, capex = ctx.get("res"), ctx.get("capex")
    snap = {"number": p.number,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scenario_pk": p.scenario.pk if p.scenario else None,
            "scenario_name": p.scenario.name if p.scenario else None}
    if res:
        snap.update(annual_harvest_t=res.get("annual_harvest_t"), steady_state=res.get("steady_state"),
                    opex_total=ctx["o"]["total"] if ctx.get("o") else None,
                    sizing=ctx.get("sizing") or [],
                    harvests_first12=[{"date": str(h.get("date")), "biomass_t": h.get("biomass_t")}
                                      for h in (ctx.get("harvests") or [])])
    if capex:
        snap["capex"] = {"grand_total": capex.get("grand_total"), "equipment_total": capex.get("equipment_total"),
                         "extras_total": capex.get("extras_total"), "currency": capex.get("currency"),
                         "by_group": [list(g) for g in capex.get("by_group") or []],
                         "by_stage": [list(s) for s in capex.get("by_stage") or []]}
    return snap


def proposal_pdf(request, pid):
    p = get_object_or_404(Proposal, pk=pid)
    ctx = proposal_context(p, request)
    if ctx.get("error"):  # the plan failed: a document without its figures must not reach a customer
        messages.error(request, ctx["error"])
        return redirect("proposal_edit", pid=pid)
    checks = _preflight(p)
    draft = any(not ok for _label, ok, _hint in checks)
    if draft and os.environ.get("RASPLAN_STRICT_PDF") == "1":
        missing = ", ".join(label for label, ok, _hint in checks if not ok)
        messages.error(request, f"No PDF while the preflight is incomplete: {missing}. "
                                "Finish these, or unset RASPLAN_STRICT_PDF.")
        return redirect("proposal_edit", pid=pid)
    url = request.build_absolute_uri(reverse("proposal_doc", args=[pid])
                                     + "?pdf=1" + ("&draft=1" if draft else ""))
    company = CompanyProfile.get()
    footer = (f'<div style="width:100%;font:8px Arial,sans-serif;color:#666;padding:0 15mm;display:flex;justify-content:space-between">'
              f'<span>{company.name} · Proposal {p.number}</span>'
              f'<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>')

    def render_pdf(browser):
        page = browser.new_page(color_scheme="light")
        page.goto(url, wait_until="networkidle", timeout=170000)
        return page.pdf(format="A4", print_background=True, display_header_footer=True,
                        header_template="<span></span>", footer_template=footer,
                        margin={"top": "16mm", "bottom": "18mm", "left": "15mm", "right": "15mm"})
    try:
        pdf = _browser_job(render_pdf)
    except Exception as exc:  # browser missing or timed out: say what happened
        messages.error(request, f"The PDF could not be made: {exc}")
        return redirect("proposal_edit", pid=pid)
    p.snapshot = _snapshot_from_ctx(ctx)  # archive what this PDF said, before the scenario can change or go
    p.save()
    resp = HttpResponse(pdf, content_type="application/pdf")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in p.number)
    resp["Content-Disposition"] = f'attachment; filename="Proposal_{safe}.pdf"'
    return resp


# ---------- AI: settings and drafting ----------

class AISettingsForm(forms.ModelForm):
    """Keys are never shown again: an empty key field keeps the saved key."""
    class Meta:
        model = AISettings
        fields = ["provider", "claude_key", "claude_model", "openai_key", "openai_model", "zai_key", "zai_model"]
        widgets = {k: forms.PasswordInput(render_value=False, attrs={"autocomplete": "off", "placeholder": "paste key"})
                   for k in ("claude_key", "openai_key", "zai_key")}


def _mask(key):
    return f"{key[:7]}…{key[-4:]}" if len(key) > 14 else ("set" if key else "")


def ai_settings(request):
    obj = AISettings.get()
    saved_keys = {p: obj.key(p) for p in PROVIDERS}
    form = AISettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        new = form.save(commit=False)
        for p in PROVIDERS:
            if request.POST.get(f"clear_{p}"):
                setattr(new, f"{p}_key", "")
            elif not form.cleaned_data.get(f"{p}_key"):
                setattr(new, f"{p}_key", saved_keys[p])  # left empty: keep the saved key
            else:
                setattr(new, f"{p}_key", form.cleaned_data[f"{p}_key"].strip())
        new.save()
        messages.success(request, "AI settings saved.")
        return redirect("ai_settings")
    providers = [{"key": p, "label": v["label"], "default_model": v["default_model"], "masked": _mask(saved_keys[p]),
                  "key_field": form[f"{p}_key"], "model_field": form[f"{p}_model"]} for p, v in PROVIDERS.items()]
    return render(request, "planner/ai_settings.html", {"form": form, "providers": providers,
                                                        "scenarios": Scenario.objects.all()})


AI_SYSTEM = (
    "You help Suderra AS, a Norwegian engineering company that designs recirculating aquaculture systems (RAS), "
    "write client proposals. Write in clear, professional British English for a fish-farming client. "
    "Use only the facts given; never invent numbers, prices, dates, guarantees or legal commitments. "
    "Return plain text only: no markdown, no headings, no greeting or sign-off."
)

AI_TASKS = {
    "intro": ("Write the Introduction section (one or two short paragraphs): who proposes what to whom, the "
              "facility and what it produces, and what this document contains."),
    "sizing_note": ("Write a short note (2-4 sentences) to print under the system sizing table: the design basis "
                    "and anything the reader should know when reading the table."),
    "terms": "Improve the wording of the 'Terms of the proposal' text. Keep every commercial term exactly as it is.",
    "payment": "Improve the wording of the payment terms. Keep every percentage, period and condition exactly as it is.",
    "review": ("Review this proposal before it is sent. List concrete problems as short lines starting with '- ': "
               "inconsistencies, missing information, numbers that look wrong, unclear wording, risks for the supplier. "
               "If nothing is wrong, say so in one line."),
}


def _facts(p, ctx):
    lines = [f"Supplier: {ctx['company'].name} ({ctx['company'].country})",
             f"Customer: {p.customer_name or 'not given'}", f"Project title: {p.title}",
             f"Proposal no. {p.number}, date {p.date}, valid until {p.valid_until or 'not set'}",
             f"Design fee: {ctx['fee_total']:,.0f} {p.currency}" + (f" + VAT {p.vat_pct:g} %" if p.vat_pct else " excl. VAT"),
             "Work packages: " + "; ".join(w["name"] for w in p.work_packages)]
    res = ctx.get("res")
    if res:
        plan = ctx["plan"]
        lines.append(f"Production plan: first stocking {plan.start_date}, a batch every {plan.interval_days} days, "
                     f"harvest at {res['stages'][-1].max_weight_g:g} g after {res['harvest_age']} days"
                     + (f", about {res['annual_harvest_t']:.1f} t harvested per year" if res["annual_harvest_t"] else ""))
        for s in ctx["sizing"]:
            lines.append(f"RAS unit {s['name']}: fish {s['w_in']:.1f}-{s['w_out']:.0f} g for {s['days']} days, "
                         f"{s['tanks']} tanks of {s['tank_m3']:.1f} m³, peak biomass {s['biomass_t']:.2f} t, "
                         f"design flow {s['flow'] or 0:.0f} m³/h, peak feed {s['feed']:.1f} kg/day, "
                         f"biofilter {s['biofilter_m3'] or 0:.1f} m³, {s['temp']:.1f} °C, {s['oxygen'].lower()}")
        cap, o = ctx["capex"], ctx["o"]
        lines.append(f"CAPEX estimate: {cap['grand_total']:,.0f} {cap['currency']}"
                     + ("" if cap["grand_total"] else " (no prices entered yet)"))
        lines.append(f"OPEX estimate: {o['total']:,.0f} {cap['currency']} per year"
                     + (f", {o['cost_per_kg']:.2f} per kg" if o["cost_per_kg"] else ""))
        if ctx.get("process"):
            lines.append("Process diagrams drawn for: " + ", ".join(u["name"] for u in ctx["process"]))
    elif ctx.get("error"):
        lines.append(ctx["error"])
    return "\n".join(lines)


def proposal_ai(request, pid):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)
    p = get_object_or_404(Proposal, pk=pid)
    task = request.POST.get("task", "")
    if task not in AI_TASKS:
        return JsonResponse({"ok": False, "error": "Unknown task."}, status=400)
    settings_ = AISettings.get()
    provider = request.POST.get("provider") or settings_.provider
    ctx = proposal_context(p, request)
    current = request.POST.get("current", "").strip()
    instruction = request.POST.get("instruction", "").strip()
    prompt = f"FACTS\n{_facts(p, ctx)}\n\nTASK\n{AI_TASKS[task]}"
    if task == "review":
        prompt += ("\n\nPROPOSAL TEXTS\nIntroduction:\n" + (ctx["intro"] or "(empty)") + "\n\nTerms:\n" + ctx["terms"]
                   + "\n\nPayment:\n" + ctx["payment"] + "\n\nGeneral conditions:\n"
                   + "\n".join(f"{c['title']}: {c['text']}" for c in ctx["clauses"]))
    elif current:
        prompt += f"\n\nCURRENT TEXT (start from it)\n{current}"
    if instruction:
        prompt += f"\n\nEXTRA INSTRUCTIONS FROM THE USER\n{instruction}"
    try:
        # 6000 so GLM-5.x finishes its reasoning AND the draft in one call
        text = generate(provider, settings_.key(provider), settings_.model(provider), AI_SYSTEM, prompt,
                        max_tokens=6000)
    except AIError as exc:
        return JsonResponse({"ok": False, "error": str(exc)})
    return JsonResponse({"ok": True, "text": text, "provider": PROVIDERS[provider]["label"]})
