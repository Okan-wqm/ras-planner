"""A roaming AI chat panel: available on every screen, aware of the page and the scenario.

Posts to ai.generate with a compact, page-aware context block; the browser panel
(planner/templates/planner/base.html) keeps the conversation and picks the GLM
thinking level (low/high/max) per request. The model can also ACT on the app:
validated tool calls (set_param / set_brief / reset_diagram) at the end of its reply."""
import json
import math
import re

from django.http import JsonResponse
from django.urls import reverse

from ras_engine.params import Params

from . import ai
from .models import AISettings, Proposal, Scenario
from .services import diagram_results, plan_result, step_status
from .views_process import _json_body

THINKING_LEVELS = ("low", "high", "max")
MAX_MESSAGE = 8000          # characters, one chat turn
MAX_HISTORY = 10            # turns of conversation resent with each call

STEP_NAMES = (
    ("brief", "01 Brief (customer targets)"), ("production", "02 Production plan"),
    ("units", "03 RAS units & tanks"), ("requirements", "04 Requirements & flows"),
    ("process", "05 Process design (diagram editor)"), ("equipment", "06 Equipment"),
    ("capex", "07 CAPEX"), ("opex", "08 OPEX"),
    ("proposals", "09 Proposals"), ("dashboard", "Library · single batch (Excel check)"),
    ("library", "Library tools"), ("feeds", "Library · feeds"), ("standards", "Library · standards"),
    ("company", "Library · company"), ("ai-settings", "Library · AI settings"),
)

AI_CHAT_SYSTEM = (
    "You are the assistant built into a RAS farm planner used by an aquaculture engineer. "
    "Answer in the language the user writes in (usually Turkish). Be concise and practical. "
    "Use ONLY the numbers given in the CONTEXT block - never invent figures; if a number is "
    "missing, say which page it lives on.\n\n"
    "ACTING ON THE APP: when the user asks you to change something, end your reply with ONE "
    "fenced json block that lists the actions:\n"
    '```json\n{"actions": [{"tool": "set_param", "args": {"key": "growth.tgc", "value": 1.8}}]}\n```\n'
    "Tools (max 5 actions per reply, use each only when asked for a change):\n"
    '- set_param {key, value}: set a scenario constant. Key must be one of the CONTEXT\'s known '
    "constants (e.g. growth.temperature, flow.co2_max, gas.salinity). Value: number.\n"
    "- set_brief {key, value}: set a brief target (pieces_per_batch, target_weight_g, "
    "batches_per_year, water_m3h). Value: number.\n"
    "- set_stage {unit, tanks?, exit_weight_g?, retention_min?}: change a RAS stage (1-based unit).\n"
    "- remove_stage {unit}: delete a RAS unit from the chain (its diagram and equipment data "
    "are pruned); at least one unit must remain.\n"
    "- set_o2_source {unit, pure}: pure oxygen (cone) when true, aeration when false.\n"
    "- set_equipment {unit, slot, capacity_m3 | standard_pk}: per-unit equipment volume "
    "(slot: tank, biofilter, biomedia, pump, drum_filter, degasser, ...). set_tank = slot tank.\n"
    "- reset_diagram {unit}: forget the drawn process diagram of a unit (1-based index) so the "
    "automatic layout is used again.\n"
    "- proposal_set {field, value}: write a proposal text (title, customer_*, intro, terms, "
    "payment, sizing_note). Value: string.\n"
    "- proposal_clause {title, text}: set or add one general-conditions clause.\n"
    "- read_agent {name}: read one agent's FULL context/role text (use before update_agent, "
    "or when the user asks what an agent's rules are). The result appears in the action log.\n"
    "- update_agent {name, role}: REWRITE one army agent's whole context (the text on the "
    "Library · Agents page). Only when the user asks for a change to an agent; ALWAYS "
    "read_agent FIRST to see the current text, then keep the parts they didn't ask to change; "
    "role: the full new text.\n"
    "- toggle_agent {name, enabled}: enable or disable one agent.\n"
    "The CONTEXT lists the agent army with names, labels and a snippet of each role.\n"
    "Explain in plain text what you changed and why; the app applies the block, refreshes the "
    "calculations and reloads the page. Never invent keys - if unsure, ask."
)

_ACTION_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def provider_chain(s, preferred=None):
    """Configured providers in preference order: the caller's pick, then the setting, then the rest."""
    order = ([preferred] if preferred else []) + [s.provider] + \
        [p for p in ("zai", "claude", "openai") if p not in (preferred, s.provider)]
    seen, out = set(), []
    for p in order:
        if p not in seen and s.key(p):
            seen.add(p)
            out.append(p)
    return out


def call_ai(s, system, prompt, level="", max_tokens=2000, preferred=None):
    """Generate via the first provider that works; remember which one answered."""
    last = None
    for p in provider_chain(s, preferred):
        try:
            return p, ai.generate(p, s.key(p), s.model(p), system, prompt,
                                  max_tokens=max_tokens, thinking_level=level)
        except ai.AIError as exc:
            last = exc
    raise last or ai.AIError("No API key configured - add one on the AI settings page.")


def _number(v):
    if isinstance(v, bool):
        raise ValueError("boolean not expected")
    f = float(v)
    if not math.isfinite(f):
        raise ValueError("not a finite number")
    return f


def _unit_ref(sc, ref):
    """Resolve a unit reference the model gave: 1-based index ('1') or stage name ('Fry')."""
    prod = dict(sc.production or {})
    stages = list(prod.get("stages") or [])
    txt = str(ref).strip()
    if txt.isdigit():
        idx = int(txt) - 1
        if 0 <= idx < len(stages):
            return idx
        raise ValueError(f"unit index out of range (1-{len(stages)})")
    low = txt.lower()
    for i, st in enumerate(stages):
        if str(st.get("name", "")).strip().lower() == low:
            return i
    raise ValueError(f"unknown unit '{ref}' (use 1-{len(stages)} or the stage name)")


def _stages_rw(sc):
    """Return (production_dict, stages_list, index_of_uid) for read-modify-write on stages."""
    prod = dict(sc.production or {})
    stages = [dict(s) for s in (prod.get("stages") or [])]
    return prod, stages


def _proposal_for(sc):
    """The scenario's newest proposal, or a fresh one with the standard texts."""
    from datetime import date, timedelta
    from . import proposal_defaults as D
    from .views_docs import _next_number
    p = Proposal.objects.filter(scenario=sc).order_by("-pk").first()
    if p is None:
        today = date.today()
        p = Proposal.objects.create(
            scenario=sc, number=_next_number(), title=f"RAS facility design — {sc.name}",
            date=today, valid_until=today + timedelta(days=30),
            currency=(sc.capex or {}).get("currency", "EUR"),
            terms=D.TERMS, payment=D.PAYMENT,
            work_packages=[{"name": n, "notes": t, "price": None} for n, t in D.WORK_PACKAGES],
            clauses=[{"title": t, "text": x} for t, x in D.CLAUSES],
            sections={k: True for k, _ in D.SECTIONS})
    return p


PROPOSAL_TEXT_FIELDS = {"title", "customer_name", "customer_address", "customer_contact",
                        "customer_email", "customer_phone", "customer_reg",
                        "intro", "terms", "payment", "sizing_note"}


def _apply_actions(sc, actions, allow_agent_edit=False):
    """Apply the model's requested tool calls. Returns (applied, failed) description lists.
    Agent-editing tools are reserved for the chat assistant (the army must not rewrite itself)."""
    from .services import prewarm
    known = set(Params().to_flat())
    applied, failed = [], []
    params = dict(sc.params or {})
    brief = dict(sc.brief or {})
    touched = {"params": False, "brief": False, "process": False, "production": False, "equipment": False}
    for act in actions[:5]:
        try:
            tool = act["tool"]
            args = act.get("args") or {}
            if tool == "set_param":
                key, value = str(args["key"]).strip(), _number(args["value"])
                if key not in known:
                    raise ValueError(f"unknown constant '{key}'")
                params[key] = value
                touched["params"] = True
                applied.append(f"{key} = {value:g}")
            elif tool == "set_brief":
                key, value = str(args["key"]).strip(), _number(args["value"])
                if key not in {"pieces_per_batch", "target_weight_g", "batches_per_year", "water_m3h"}:
                    raise ValueError(f"brief field '{key}' is not settable")
                brief[key] = value
                touched["brief"] = True
                applied.append(f"brief.{key} = {value:g}")
            elif tool == "reset_diagram":
                idx = _unit_ref(sc, args.get("unit", 1))
                sc.set_diagram(idx, None)
                touched["process"] = True
                applied.append(f"diagram of unit {idx + 1} reset to automatic")
            elif tool == "save_automatic_diagram":
                idx = _unit_ref(sc, args.get("unit", 1))
                from .costing import _diagram_for
                pr, _err = plan_result(sc)
                if not pr:
                    raise ValueError("the plan does not calculate - fix it first")
                d, _default = _diagram_for(sc, pr[1], idx)
                if not (d or {}).get("nodes"):
                    raise ValueError("no automatic layout available")
                sc.set_diagram(idx, d)
                touched["process"] = True
                applied.append(f"automatic process diagram saved for unit {idx + 1} "
                               f"({len(d['nodes'])} nodes) - the app solves it for the lamps")
            elif tool == "set_stage":
                idx = _unit_ref(sc, args.get("unit", 1))
                prod, stages = _stages_rw(sc)
                bits = []
                if args.get("tanks") is not None:
                    n = int(_number(args["tanks"]))
                    if n < 1:
                        raise ValueError("tanks must be >= 1")
                    stages[idx]["n_tanks"] = n
                    bits.append(f"{n} tanks")
                if args.get("exit_weight_g") is not None:
                    stages[idx]["max_weight_g"] = _number(args["exit_weight_g"])
                    bits.append(f"exit {stages[idx]['max_weight_g']:g} g")
                if args.get("retention_min") is not None:
                    stages[idx]["retention_min"] = _number(args["retention_min"])
                    bits.append(f"retention {stages[idx]['retention_min']:g} min")
                if not bits:
                    raise ValueError("nothing to set (tanks/exit_weight_g/retention_min)")
                prod["stages"] = stages
                sc.production = prod
                touched["production"] = True
                applied.append(f"stage {idx + 1}: " + ", ".join(bits))
            elif tool == "set_o2_source":
                idx = _unit_ref(sc, args.get("unit", 1))
                prod, stages = _stages_rw(sc)
                pure = bool(args.get("pure", True))
                ov = dict(stages[idx].get("overrides") or {})
                ov["gas.pure_oxygen"] = pure
                stages[idx]["overrides"] = ov
                prod["stages"] = stages
                sc.production = prod
                touched["production"] = True
                applied.append(f"unit {idx + 1} oxygen: " + ("pure O₂ (cone)" if pure else "aeration"))
            elif tool == "remove_stage":
                idx = _unit_ref(sc, args.get("unit", 1))
                prod, stages = _stages_rw(sc)
                if len(stages) <= 1:
                    raise ValueError("the plan needs at least one RAS unit")
                removed = stages.pop(idx)
                prod["stages"] = stages
                sc.production = prod
                live = {st.get("id") for st in stages}
                process = {k: v for k, v in (sc.process or {}).items() if k in live}
                eq = dict(sc.equipment or {})
                eq_stages = {k: v for k, v in (eq.get("stages") or {}).items() if k in live}
                eq["stages"] = eq_stages
                sc.process, sc.equipment = process, eq
                touched["production"] = touched["process"] = touched["equipment"] = True
                applied.append(f"removed RAS unit '{removed.get('name', idx + 1)}' "
                               "(diagram and equipment data pruned)")
            elif tool in ("set_tank", "set_equipment"):
                slot = "tank" if tool == "set_tank" else str(args.get("slot") or "tank").strip()
                idx = _unit_ref(sc, args.get("unit", 1))
                ids = sc.unit_ids() or []
                eq = dict(sc.equipment or {})
                eq_stages = dict(eq.get("stages") or {})
                entry = dict(eq_stages.get(ids[idx]) or {})
                known_slots = set(entry) | {"tank", "biofilter", "biomedia", "pump",
                                            "drum_filter", "degasser"}
                if slot not in known_slots:
                    raise ValueError(f"equipment slot '{slot}' is not known (has: {sorted(known_slots)})")
                item = dict(entry.get(slot) or {})
                if args.get("capacity_m3") is not None or args.get("capacity") is not None:
                    m3 = _number(args.get("capacity_m3", args.get("capacity")))
                    if m3 <= 0:
                        raise ValueError("capacity must be positive")
                    item["capacity"] = m3
                    item["item"] = ""
                    applied.append(f"unit {idx + 1} {slot}: custom {m3:g} m³")
                elif args.get("standard_pk") is not None:
                    from .models import EquipmentStandard
                    std = EquipmentStandard.objects.filter(pk=int(args["standard_pk"])).first()
                    if std is None:
                        raise ValueError(f"no equipment standard pk={args['standard_pk']}")
                    item["item"] = str(std.pk)
                    item["capacity"] = None
                    applied.append(f"unit {idx + 1} {slot}: standard {std.name} ({std.capacity:g} m³)")
                else:
                    raise ValueError("capacity_m3 or standard_pk required")
                entry[slot] = item
                eq_stages[ids[idx]] = entry
                eq["stages"] = eq_stages
                sc.equipment = eq
                touched["equipment"] = True
            elif tool == "proposal_set":
                field = str(args["field"]).strip()
                value = str(args["value"]).strip()[:20000]
                if field not in PROPOSAL_TEXT_FIELDS:
                    raise ValueError(f"proposal field '{field}' is not settable")
                p = _proposal_for(sc)
                setattr(p, field, value)
                p.save(update_fields=[field])
                applied.append(f"proposal.{field} updated ({len(value)} chars)")
            elif tool == "proposal_clause":
                title = str(args["title"]).strip()[:120]
                text = str(args["text"]).strip()[:8000]
                if not title:
                    raise ValueError("clause title required")
                p = _proposal_for(sc)
                clauses = [dict(c) for c in (p.clauses or [])]
                for c in clauses:
                    if c.get("title") == title:
                        c["text"] = text
                        break
                else:
                    if len(clauses) >= 12:
                        raise ValueError("too many clauses (max 12)")
                    clauses.append({"title": title, "text": text})
                p.clauses = clauses
                p.save(update_fields=["clauses"])
                applied.append(f"proposal clause '{title}' set ({len(text)} chars)")
            elif tool in ("update_agent", "toggle_agent") and allow_agent_edit:
                from .models import AgentSpec
                from .agents import load_specs
                load_specs()            # make sure the whole roster exists
                name = str(args.get("name") or "").strip()
                spec = AgentSpec.objects.filter(name=name).first()
                if spec is None:
                    raise ValueError(f"unknown agent '{name}'")
                if tool == "update_agent":
                    role = str(args.get("role") or "").strip()[:20000]
                    if len(role) < 20:
                        raise ValueError("role text too short (min 20 chars)")
                    spec.role = role
                    spec.save(update_fields=["role"])
                    applied.append(f"agent '{name}' context rewritten ({len(role)} chars)")
                else:
                    enabled = args.get("enabled")
                    if not isinstance(enabled, bool):
                        raise ValueError("toggle_agent requires 'enabled' to be a boolean (true/false)")
                    spec.enabled = enabled
                    spec.save(update_fields=["enabled"])
                    applied.append(f"agent '{name}' " +
                                   ("enabled" if spec.enabled else "disabled"))
            elif tool == "read_agent" and allow_agent_edit:
                from .models import AgentSpec
                from .agents import load_specs
                load_specs()
                name = str(args.get("name") or "").strip()
                spec = AgentSpec.objects.filter(name=name).first()
                if spec is None:
                    raise ValueError(f"unknown agent '{name}'")
                applied.append(f"agent '{name}' ({spec.label}, "
                               f"{'enabled' if spec.enabled else 'DISABLED'}):\n{spec.role[:3000]}")
            elif tool == "calc":
                # safe math on the agent's own numbers: calc {"expr": "34.5 * 1.163 * 4 / 3"}
                import asteval
                expr = str(args.get("expr") or "").strip()
                if not expr or len(expr) > 500:
                    raise ValueError("expr must be 1-500 chars")
                interp = asteval.Interpreter(usersyms={"min": min, "max": max, "abs": abs,
                                                       "round": round, "sqrt": __import__("math").sqrt,
                                                       "exp": __import__("math").exp,
                                                       "log": __import__("math").log10})
                val = interp(expr)
                if interp.error:
                    raise ValueError(f"calc error: {interp.error[0].get_error()[:120]}")
                applied.append(f"calc: {expr} = {val:.6g}")
            elif tool == "preview":
                # what-if: run the engine with hypothetical param overrides, return key metrics
                overrides = args.get("params") or {}
                if not isinstance(overrides, dict) or not overrides:
                    raise ValueError("params dict required, e.g. {'bio.tan_target': 2}")
                known_ov = set(Params().to_flat()) | {"bio." + fn for fn in
                                                      __import__("ras_engine.params", fromlist=["BiofilterParams"]).BiofilterParams().__dataclass_fields__}
                bad = [k for k in overrides if k not in known_ov]
                if bad:
                    raise ValueError(f"unknown keys: {bad[:5]}")
                from .models import Feed as FeedModel
                from ras_engine.params import BiofilterParams, Params as P, cast as _cast
                from ras_engine.simulate import simulate
                from ras_engine.production import growth_curve, run_plan, RasStage
                from ras_engine.biofilter import biofilter_row
                from datetime import date
                p = Params.from_flat({**(sc.params or {}), **{k: str(v) for k, v in overrides.items()
                                                              if not k.startswith("bio.")}})
                # bio overrides on a fresh BiofilterParams
                bp = sc.engine_biofilter()
                for k, v in overrides.items():
                    if k.startswith("bio."):
                        setattr(bp, k[4:], _cast(getattr(BiofilterParams(), k[4:]), v))
                feeds = [f.to_engine() for f in FeedModel.objects.all()] if sc.use_feed_catalog else None
                prod = sc.production or {}
                stages = prod.get("stages") or [{"id": "0", "name": "Fry", "max_weight_g": 5,
                                                  "n_tanks": 4, "retention_min": 30}]
                st = [RasStage(s.get("name", "Fry"), s.get("max_weight_g", 5),
                               s.get("n_tanks", 4), s.get("retention_min", 30),
                               params=p, uid=s.get("id", "")) for s in stages]
                curve = growth_curve(p, st[-1].max_weight_g if st else 5, feeds=feeds,
                                     temperature_steps=sc.temperature_steps, stages=st)
                plan = run_plan(curve, st,
                                __import__("ras_engine.production", fromlist=["PlanParams"]).PlanParams(
                                    date(2027, 1, 1),
                                    int((prod.get("interval_days") or 30)),
                                    int((prod.get("horizon_days") or 730)),
                                    params=p),
                                bp)
                d0 = next((d for d in plan["designs"] if not d["empty"]), None)
                if not d0:
                    raise ValueError("plan produced no designs")
                peak = d0["peak"]
                bio = d0.get("biofilter") or {}
                result = (f"peak_feed={peak['feed_kg']:.1f} kg/d | "
                          f"O2_total={peak.get('o2_total_kgd', 0):.1f} | "
                          f"design_O2={peak.get('design_o2_kgd', 0):.1f} | "
                          f"pure_O2={peak.get('pure_o2_kgd', 0):.1f} | "
                          f"CO2={peak.get('co2_kgd', 0):.1f} | "
                          f"TAN={peak.get('tan_kgd', 0):.2f} | "
                          f"q_design={peak['q_design_m3h']:.0f} m3/h | "
                          f"peak_biomass={peak.get('biomass_kg', 0):.0f} kg | "
                          f"volume={peak.get('volume_m3', 0):.1f} m3 | "
                          f"bio_media={bio.get('media_fresh_m3') or bio.get('media_salt_m3') or 0:.2f} m3 | "
                          f"bio_feasible={bio.get('feasible')} | "
                          f"bio_eff={bio.get('efficiency_pct', 0):.1f}%")
                applied.append(f"preview({', '.join(f'{k}={v}' for k, v in list(overrides.items())[:4])}): {result}")
            else:
                raise ValueError(f"unknown tool '{tool}'")
        except (KeyError, TypeError, ValueError) as exc:
            failed.append(f"{act!r:.90}: {exc}")
    sc.params, sc.brief = params, brief
    fields = [f for f, t in touched.items() if t]
    if fields:
        sc.save(update_fields=fields)
    if applied:
        prewarm(sc)        # evict + recompute so the reply's lamps are fresh
    return applied, failed


def _step_of(path: str) -> str:
    for key, label in STEP_NAMES:
        if key in path:
            return label
    return "main dashboard"


def _page_context(sc, path: str) -> str:
    """A short, honest snapshot of what the user is looking at."""
    lines = [f"Current screen: {_step_of(path)}"]
    if not sc:
        lines.append("No scenario exists yet.")
        return "\n".join(lines)
    lines.append(f"Scenario: {sc.name!r}")
    pr, plan_err = plan_result(sc)
    res = pr[1] if pr else None
    if res is None:
        lines.append(f"The production plan currently fails to calculate: {plan_err}")
    else:
        stages = ", ".join(f"{s.name} → {s.max_weight_g:g} g" for s in res["stages"])
        annual = f", about {res['annual_harvest_t']:.0f} t/year" if res["annual_harvest_t"] else ""
        lines.append(f"Plan: a batch every {res['interval_days']} days, harvest after {res['harvest_age']} days{annual}")
        lines.append(f"RAS units: {stages}")
        if not res["steady_state"]:
            lines.append("The plan horizon is shorter than one full grow-out (not yet at steady state).")
    lamps = step_status(sc)
    lit = [f"{n}={st}" for n, st in sorted(lamps.items()) if st]
    if lit:
        lines.append("Workflow lamps: " + ", ".join(lit))
    # agent roster summary (so the assistant knows the army it can read/edit)
    try:
        from .agents import load_specs
        from .models import AgentSpec
        load_specs()
        roster = AgentSpec.objects.all().order_by("pk")
        lines.append("AGENT ARMY (read_agent {name} to see the full context, "
                     "update_agent {name, role} to rewrite it):")
        for a in roster:
            snippet = a.role[:180].replace("\n", " ").strip()
            lines.append(f"  {a.name} ({a.label}) "
                         f"{'✓' if a.enabled else '✗ OFF'}: {snippet}…")
    except Exception:
        pass  # agent roster is optional context
    for idx, uid in enumerate(sc.unit_ids() or []):
        r = diagram_results(sc.diagram_of(idx))
        name = next((s.name for s in (res["stages"] if res else []) if getattr(s, "id", None) == uid),
                    f"unit {idx + 1}")
        if r is None:
            drawn = bool((sc.diagram_of(idx) or {}).get("nodes"))
            lines.append(f"Process diagram {name}: "
                         + ("DRAWN but NOT solvable - reset it to the automatic layout." if drawn
                            else "not drawn - save the automatic layout so it can be solved."))
        else:
            warn = r.get("warnings") or []
            conv = "settled" if r.get("converged", True) else "DID NOT SETTLE"
            lines.append(f"Process diagram {name}: solved, {conv}, {len(warn)} warning(s)"
                         + (": " + "; ".join(str(w)[:120] for w in warn[:3]) if warn else ""))
    return "\n".join(lines)


def ai_chat(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)
    body, err = _json_body(request)
    if err:
        return err
    message = str(body.get("message") or "").strip()
    history = body.get("history") or []
    thinking = str(body.get("thinking") or "").strip().lower()
    if not message or len(message) > MAX_MESSAGE:
        return JsonResponse({"ok": False, "error": f"Message must be 1-{MAX_MESSAGE} characters."}, status=400)
    if not isinstance(history, list):
        history = []
    history = history[-MAX_HISTORY:]
    turns = []
    for h in history:
        if not isinstance(h, dict):
            continue
        role = "user" if h.get("role") == "user" else "assistant"
        text = str(h.get("content") or "")[:4000]
        if text.strip():
            turns.append(f"{role}: {text}")

    m = re.search(r"/scenario/(\d+)/", str(body.get("path") or ""))
    sc = Scenario.objects.filter(pk=int(m.group(1))).first() if m else Scenario.objects.order_by("-updated").first()
    known_keys = ", ".join(sorted(Params().to_flat()))
    prompt = f"CONTEXT\n{_page_context(sc, str(body.get('path') or ''))}\nKnown constants: {known_keys}"
    if turns:
        prompt += "\n\nCONVERSATION SO FAR\n" + "\n".join(turns)
    prompt += f"\n\nUSER MESSAGE\n{message}"

    s = AISettings.get()
    level = thinking if thinking in THINKING_LEVELS else "low"
    preferred = str(body.get("provider") or "").strip().lower()
    if preferred not in ai.PROVIDERS:
        preferred = ""
    # the panel can override the model per request (per-provider model selector)
    model_override = str(body.get("model") or "").strip()
    if model_override and preferred:
        setattr(s, f"{preferred}_model", model_override)
    elif model_override and not preferred:
        # no provider picked: apply to the current default provider
        setattr(s, f"{s.provider}_model", model_override)
    try:
        provider, text = call_ai(s, AI_CHAT_SYSTEM, prompt, level, preferred=preferred)
    except ai.AIError as exc:
        return JsonResponse({"ok": False, "error": str(exc)})

    applied, failed = [], []
    block = _ACTION_BLOCK.findall(text)
    if block and sc:
        text = _ACTION_BLOCK.sub("", text).strip()
        try:
            actions = json.loads(block[-1]).get("actions")
            applied, failed = _apply_actions(sc, actions if isinstance(actions, list) else [],
                                             allow_agent_edit=True)
        except (ValueError, KeyError, TypeError) as exc:
            failed = [f"action block invalid: {exc}"]
    resp = {"ok": True, "text": text, "provider": ai.PROVIDERS[provider]["label"], "thinking": level,
            "applied": applied, "failed": failed, "reload": bool(applied)}
    if applied and sc:
        resp["lamps"] = step_status(sc)
    return JsonResponse(resp)


def ai_panel_config():
    """Small payload the base template feeds the chat panel."""
    s = AISettings.get()
    provider = s.provider if s.key(s.provider) else next(iter(s.configured() or [""]), "")
    providers = [{"id": p, "label": ai.PROVIDERS[p]["label"]} for p in s.configured()]
    # per-provider model choices the panel can switch between
    MODELS = {
        "claude": [{"value": "claude-sonnet-5-5", "label": "Sonnet 5.5"},
                   {"value": "claude-opus-4-1", "label": "Opus 4.1"},
                   {"value": "claude-haiku-3-5", "label": "Haiku 3.5"}],
        "zai":    [{"value": "glm-5.3", "label": "GLM-5.3"},
                   {"value": "glm-4.6", "label": "GLM-4.6"}],
        "openai": [{"value": "gpt-5", "label": "GPT-5"},
                   {"value": "gpt-4.1", "label": "GPT-4.1"},
                   {"value": "gpt-4o-mini", "label": "GPT-4o mini"}],
    }
    model_defaults = {}
    for p in s.configured():
        saved = s.model(p) or ai.PROVIDERS[p]["default_model"]
        model_defaults[p] = saved
        if not any(m["value"] == saved for m in MODELS.get(p, [])):
            MODELS.setdefault(p, []).insert(0, {"value": saved, "label": saved})
    return {"url": reverse("ai_chat"), "mission": reverse("ai_mission"),
            "provider": ai.PROVIDERS[provider]["label"] if provider else "",
            "providers": providers,
            "provider_default": provider,
            "models": {p: MODELS.get(p, []) for p in s.configured()},
            "model_defaults": model_defaults,
            "glm5": bool(provider == "zai" and (s.model("zai") or ai.PROVIDERS["zai"]["default_model"]).startswith("glm-5")),
            "levels": list(THINKING_LEVELS)}


def agents_page(request):
    """Library · Agents: read and edit every agent's context (role prompt)."""
    from django.shortcuts import render, redirect
    from .models import AgentSpec
    if request.method == "POST":
        spec = AgentSpec.objects.filter(pk=request.POST.get("pk")).first()
        if spec is None:
            return redirect("agents_page")
        spec.label = request.POST.get("label", spec.label)[:60]
        spec.role = (request.POST.get("role") or "")[:20000]
        spec.enabled = bool(request.POST.get("enabled"))
        spec.save()
        return redirect("agents_page")
    from .agents import load_specs
    orch, specialists, _ = load_specs()
    role_map = {str(a.pk): a.role for a in [orch] + list(specialists)}
    return render(request, "planner/agents.html",
                  {"orchestrator": orch, "specialists": specialists, "role_map": role_map})
