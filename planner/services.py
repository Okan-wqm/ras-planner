"""Shared calculations for the workflow pages."""
import json
import logging
from collections import OrderedDict
from functools import lru_cache

from ras_engine import growth_curve, run_plan
from ras_engine.process import DiagramError, solve

from .models import Feed

logger = logging.getLogger("planner")


@lru_cache(maxsize=64)
def _solve_cached(text: str):
    try:
        return solve(json.loads(text))
    except (DiagramError, ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
        logger.warning("solve_cached failed: %s: %s", type(exc).__name__, exc)
        return None


def diagram_results(diagram: dict | None):
    """Balance of a saved diagram (None when there is none or it cannot be solved). Cached on its content."""
    if not diagram or not diagram.get("nodes"):
        return None
    return _solve_cached(json.dumps(diagram, sort_keys=True))


def run_production(scenario):
    plan, stages = scenario.engine_plan()
    feeds = [f.to_engine() for f in Feed.objects.all()] if scenario.use_feed_catalog else None
    curve = growth_curve(scenario.engine_params(), stages[-1].max_weight_g, feeds=feeds,
                         temperature_steps=scenario.temperature_steps, stages=stages)
    return plan, run_plan(curve, stages, plan, scenario.engine_biofilter())


_PLAN_CACHE_LIMIT = 16
_plan_cache: OrderedDict = OrderedDict()  # key -> ((plan, res) | None, error message | None)


def plan_result(scenario) -> tuple[tuple | None, str | None]:
    """The one path every plan consumer goes through: ((plan, res), None), or (None, message)
    when the plan cannot be calculated. Recalculated when the scenario or the feed table changes;
    failures are cached too, keyed like successes."""
    from .models import Feed
    feeds = tuple(Feed.objects.values_list("name", "min_weight_g", "max_weight_g", "protein_pct", "fcr"))
    key = (scenario.pk, scenario.updated.isoformat() if scenario.updated else "", feeds)
    if key not in _plan_cache:
        try:
            _plan_cache[key] = (run_production(scenario), None)
        except (ValueError, ZeroDivisionError, TypeError, KeyError, ArithmeticError) as exc:
            logger.warning("plan_result failed for scenario %s: %s: %s",
                           scenario.pk, type(exc).__name__, exc)
            _plan_cache[key] = (None, f"{type(exc).__name__}: {exc}")
        if len(_plan_cache) >= _PLAN_CACHE_LIMIT:
            try:
                _plan_cache.popitem(last=False)  # the oldest entry, atomically
            except KeyError:
                pass
    return _plan_cache[key]


def plan_cached(scenario):
    """(plan, res) for the rail's status lamps; None when the plan cannot be calculated."""
    return plan_result(scenario)[0]


def evict_scenario(pk):
    """Drop every cached plan of one scenario (successes and failures alike)."""
    for key in [k for k in _plan_cache if k[0] == pk]:
        _plan_cache.pop(key, None)


def prewarm(scenario):
    """Fill the plan cache right after a save, so the page the save redirects to is instant.
    Evict first: two saves can land on the same `updated` value (one clock tick), and the entry
    under that key must not survive as a stale plan."""
    evict_scenario(scenario.pk)
    plan_cached(scenario)


def _tanks_status(scenario, n) -> str:
    tanks = [bool((scenario.equipment_of(i).get("tank") or {}).get("steps") or
                  (scenario.equipment_of(i).get("tank") or {}).get("item")) for i in range(n)]
    return "ok" if all(tanks) else "warn" if any(tanks) else ""


def _priced_drawn(scenario, n) -> bool:
    """Priced CAPEX content without running the plan: a drawn diagram item with its own price, or an
    equipment selection pointing at a standard that carries a price (JSON presence checks, one query)."""
    from .models import EquipmentStandard
    priced = set(EquipmentStandard.objects.exclude(unit_price__isnull=True).values_list("pk", flat=True))
    for i in range(n):
        for nd in (scenario.diagram_of(i) or {}).get("nodes") or []:
            if ((nd.get("data") or {}).get("unit_price")) not in (None, ""):
                return True
        for sel in (scenario.equipment_of(i) or {}).values():
            item = str((sel or {}).get("item") or "")
            if item.isdigit() and int(item) in priced:
                return True
    return False


def step_status(scenario) -> dict:
    """Status lamp per workflow step: 'ok', 'warn', 'alarm' or '' (not started)."""
    from ras_engine.compare import compare, summary_status, unit_targets
    st = {}
    b = scenario.brief or {}
    st["01"] = "ok" if b.get("pieces_per_batch") and b.get("batches_per_year") else ""
    st["02"] = "ok" if scenario.production else ""
    n = len(scenario.unit_ids())
    # these steps do not need the plan: they say what the saved settings already hold
    st["03"] = _tanks_status(scenario, n)
    st["06"] = "ok" if any(scenario.equipment_of(i) for i in range(n)) or any(scenario.diagram_of(i) for i in range(n)) else ""
    st["07"] = "ok" if ((scenario.capex or {}).get("price_overrides") or (scenario.capex or {}).get("extras")
                        or _priced_drawn(scenario, n)) else ""
    st["08"] = "ok" if scenario.opex else ""
    st["09"] = "ok" if scenario.proposals.exists() else ""
    pr = plan_cached(scenario)
    if pr is None:
        # the plan fails: say what can still be said instead of blanking the rail — 02 alarms,
        # 04 needs a look (feasibility unknown), 05 needs the plan and stays unlit
        st["02"], st["04"], st["05"] = "alarm", "warn", ""
        return st
    _plan, res = pr
    designs = [d for d in res["designs"] if not d["empty"]]
    st["04"] = "ok" if designs and all((d.get("biofilter") or {}).get("feasible") for d in designs) else "alarm"
    water = (scenario.equipment or {}).get("water") or "fresh"
    states = []
    for i, d in enumerate(res["designs"]):
        if d["empty"]:
            continue
        diagram = scenario.diagram_of(i)
        r = diagram_results(diagram)
        if r is None:
            # drawn but unsolvable is an alarm; a unit with nothing drawn stays 'none' (lamp unlit)
            states.append("alarm" if diagram and diagram.get("nodes") else "none")
            continue
        s = summary_status(compare(unit_targets(d, water), r))
        if r.get("converged") is False and s not in ("warn", "alarm"):
            s = "warn"  # the solve did not settle: its numbers are a hint, not an answer
        states.append(s)
    if states and all(s == "ok" for s in states):
        st["05"] = "ok"
    elif "alarm" in states:
        st["05"] = "alarm"
    elif any(s != "none" for s in states):
        st["05"] = "warn"
    else:
        st["05"] = ""
    return st
