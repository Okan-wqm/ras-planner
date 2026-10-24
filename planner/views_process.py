"""Step 05 Process design: the diagram editor page, its solve/save/reset endpoints and the diagram library."""
import json

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from ras_engine.compare import compare, summary_status, unit_targets
from ras_engine.process import (COMPONENT_LABEL, COMPONENT_UNIT, DEFAULT_LIMITS, NODE_TYPES, DiagramError,
                                solve as solve_process)

from .costing import _diagram_basis, _diagram_for, _fish_basis, _media_basis
from .models import EquipmentStandard, ProcessTemplate, Scenario
from .parse import _finite, _int_or_none
from .services import plan_result
from .views import _library_list


def process(request, pk):
    scenario = get_object_or_404(Scenario, pk=pk)
    ctx = {"scenario": scenario, "scenarios": Scenario.objects.all()}
    got, err = plan_result(scenario)
    if err:
        ctx["error"] = err
        return render(request, "planner/process.html", ctx)
    plan, res = got
    index = min(max(_int_or_none(request.GET.get("ras")) or 0, 0), len(res["stages"]) - 1)
    diagram, is_default = _diagram_for(scenario, res, index)
    design = res["designs"][index]
    peak = design.get("peak") or {}
    water = (scenario.equipment or {}).get("water") or "fresh"
    targets = {k: _finite(v, 3) for k, v in unit_targets(design, water).items()}
    if targets:
        targets["fish"] = _fish_basis(design)
    batch_days = _days_of(res, index)
    peak_day = max(range(len(batch_days)), key=lambda k: batch_days[k][0]) if batch_days else 0
    limits = (_diagram_basis(design) or {}).get("limits") or DEFAULT_LIMITS
    ctx.update({
        "index": index, "stages": res["stages"], "is_default": is_default, "print_mode": bool(request.GET.get("print")),
        "editor": {
            "index": index, "isDefault": is_default, "diagram": diagram, "print": bool(request.GET.get("print")),
            "types": {k: {**{x: v for x, v in t.items() if x != "default_outlets"},
                          "default_outlets": t["default_outlets"]} for k, t in NODE_TYPES.items()},
            "components": COMPONENT_LABEL, "units": COMPONENT_UNIT, "limits": limits, "targets": targets,
            "days": batch_days, "peakDay": peak_day,
            "requirementsUrl": reverse("requirements", args=[pk]),
            "standards": [{"id": s.pk, "category": s.category, "name": s.name, "capacity": s.capacity,
                           "unit": s.unit, "price": s.unit_price} for s in EquipmentStandard.objects.all()],
            "peak": {k: round(peak.get(k, 0.0), 3) for k in ("tan_kgd", "co2_kgd", "tss_kgd", "o2_kgd")},
            "designFlow": (round(peak.get("q_design_m3h", 0.0), 1)
                           if peak.get("q_design_m3h", 0) != float("inf") else None),
            "urls": {"solve": reverse("process_solve", args=[pk]),
                     "save": reverse("process_save", args=[pk, index]),
                     "reset": reverse("process_reset", args=[pk, index]),
                     "library": reverse("process_library"), "jet": reverse("jet_oxygenator"),
                     "customSolve": reverse("process_custom_solve", args=[pk]),
                     "libraryItem": reverse("process_library_item", args=[0]).replace("/0/", "/{id}/")},
            "source": f"{scenario.name} · RAS {index + 1} {res['stages'][index].name}",
            "library": _library_list(),
        },
    })
    return render(request, "planner/process.html", ctx)


def _solve_json(diagram):
    """Solve a posted diagram. A malformed shape or junk values (non-dict node/edge entries, a node
    without an id, junk targets) come back as {'ok': False, 'error': …} — never a 500. The catch set
    is services._solve_cached's, plus AttributeError, which non-dict entries raise."""
    try:
        targets = diagram.pop("targets", None)
        if not isinstance(targets, dict):
            targets = None
        if targets:  # biofilters share the media required on step 04; tanks take their loads from the feed
            diagram["basis"] = {"media_m3": targets.get("media_m3"), "fill_pct": targets.get("fill_pct"),
                                "fish": targets.get("fish")}
        results = solve_process(diagram)
        if targets:  # the unit's requirements (step 04) against what the diagram does
            results["compare"] = compare(targets, results)
            results["compare_status"] = summary_status(results["compare"])
    except (DiagramError, ValueError, TypeError, KeyError, AttributeError, ZeroDivisionError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **results}


def _json_body(request):
    """(parsed body, None) for a JSON object, or (None, a 400 response) for anything else."""
    try:
        data = json.loads(request.body or "{}")
    except ValueError:
        return None, JsonResponse({"ok": False, "error": "invalid JSON body"}, status=400)
    if not isinstance(data, dict):
        return None, JsonResponse({"ok": False, "error": "invalid JSON body"}, status=400)
    return data, None


def process_solve(request, pk):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)
    get_object_or_404(Scenario, pk=pk)
    body, error = _json_body(request)
    if error:
        return error
    return JsonResponse(_solve_json(body))


def process_save(request, pk, index):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)
    scenario = get_object_or_404(Scenario, pk=pk)
    body, error = _json_body(request)
    if error:
        return error
    diagram = _clean_diagram(body)
    basis = _media_basis(scenario, index)
    if basis:
        diagram["basis"] = basis
    scenario.set_diagram(index, diagram)
    # diagram-only save: plan cache key and ordering intentionally unaffected;
    # the solve cache is keyed by the diagram JSON
    scenario.save(update_fields=["process"])
    return JsonResponse({**_solve_json(diagram), "saved": True})


def _days_of(res, index):
    """One batch through a RAS unit, day by day, compact for the diagram's day slider: [feed, biomass, count,
    age (days), tan, tss, co2 (fish), o2, ozone, weight g] in kg/day (count in fish)."""
    out = []
    for d in res["curve"]:
        if res["stage_of_age"][d["day"] - 1 if d["day"] - 1 < len(res["stage_of_age"]) else -1] != index:
            continue
        out.append([round(d.get(k, 0.0), 4) for k in ("feed_kg", "biomass_kg")] + [round(d.get("count", 0.0)), d["day"]]
                   + [round(d.get("tan_kgd", 0.0), 5), round(d.get("tss_kgd", 0.0), 4),
                      round(max(d.get("co2_kgd", 0.0) - d.get("co2_bio_kgd", 0.0), 0.0), 4),
                      round(d.get("o2_kgd", 0.0), 4), round(d.get("ozone_kgd", 0.0), 5), round(d.get("weight_g", 0.0), 3)])
    return out


def _clean_diagram(diagram):
    """Whitelist on save: only node types registered in the engine (NODE_TYPES) are kept, and only
    edges whose both ends survived. Non-dict entries and nodes without an id are dropped too —
    nothing before this point guards them, and solve() would crash on them."""
    if not isinstance(diagram, dict):  # a junk posted diagram (e.g. a string) is treated as empty
        diagram = {}
    raw_nodes, raw_edges = diagram.get("nodes"), diagram.get("edges")
    nodes = [n for n in (raw_nodes if isinstance(raw_nodes, list) else [])
             if isinstance(n, dict) and n.get("id") and n.get("type") in NODE_TYPES]
    ids = {n["id"] for n in nodes}
    edges = [e for e in (raw_edges if isinstance(raw_edges, list) else [])
             if isinstance(e, dict) and e.get("source") in ids and e.get("target") in ids]
    limits = diagram.get("limits")
    return {"nodes": nodes, "edges": edges,
            "limits": limits if isinstance(limits, dict) and limits else DEFAULT_LIMITS}


def process_library(request):
    """GET: saved diagrams. POST {name, diagram, source, inputs}: save one (same name overwrites)."""
    if request.method == "POST":
        body, error = _json_body(request)
        if error:
            return error
        name = (body.get("name") or "").strip()
        if not name:
            return JsonResponse({"ok": False, "error": "Give the diagram a name."}, status=400)
        t, created = ProcessTemplate.objects.update_or_create(
            name=name[:120], defaults={"diagram": _clean_diagram(body.get("diagram") or {}),
                                       "source": (body.get("source") or "")[:200],
                                       "inputs": body.get("inputs") or {}})
        return JsonResponse({"ok": True, "created": created, "id": t.pk, "library": _library_list()})
    return JsonResponse({"ok": True, "library": _library_list()})


def process_custom_solve(request, pk):
    """POST: solve the CURRENT diagram with MANUAL inputs (feed, temp, flow overrides).
    Returns the solved results + warnings without saving anything — a what-if sandbox."""
    scenario = get_object_or_404(Scenario, pk=pk)
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)
    body, error = _json_body(request)
    if error:
        return error
    diagram = body.get("diagram")
    if not diagram or not isinstance(diagram, dict):
        return JsonResponse({"ok": False, "error": "diagram required"}, status=400)
    inputs = body.get("inputs") or {}
    nodes = diagram.get("nodes") or []

    # apply manual overrides to the diagram before solving
    feed_kgd = inputs.get("feed_kgd")
    temp_c = inputs.get("temp_c")
    flow_m3h = inputs.get("flow_m3h")
    if feed_kgd is not None:
        n_tanks = max(sum(1 for n in nodes if n.get("type", "").startswith("tank")), 1)
        for n in nodes:
            if n.get("type", "").startswith("tank"):
                n.setdefault("data", {}).setdefault("spec", {})
                n["data"]["spec"]["feed_kgd"] = round(float(feed_kgd) / n_tanks, 4)
    if temp_c is not None:
        for n in nodes:
            if n.get("type") == "makeup":
                n.setdefault("data", {}).setdefault("spec", {})
                n["data"]["spec"]["temp"] = float(temp_c)
    if flow_m3h is not None:
        for n in nodes:
            if n.get("type") == "makeup":
                n.setdefault("data", {}).setdefault("spec", {})
                n["data"]["spec"]["flow_m3h"] = float(flow_m3h)

    result = _solve_json(request, scenario, {"diagram": diagram, "ras": body.get("ras", 0)})
    if isinstance(result, JsonResponse):
        return result
    return result


def process_reset(request, pk, index):
    scenario = get_object_or_404(Scenario, pk=pk)
    if request.method == "POST":
        scenario.set_diagram(index, None)
        scenario.save()
        messages.success(request, "Diagram reset to the automatic layout.")
    return redirect(reverse("process", args=[pk]) + f"?ras={index}")
