import json

import pytest
from django.urls import reverse

from planner import agents as A
from planner.models import AISettings, AgentSpec, Proposal, Scenario

pytestmark = pytest.mark.django_db


def _action_block(actions):
    return "```json\n" + json.dumps({"actions": actions}) + "\n```"


def _decision(calls=None, done=None):
    payload = {"done": True, "summary": done} if done else {"calls": calls}
    return "```json\n" + json.dumps(payload) + "\n```"


@pytest.fixture
def mission_scenario(client):
    client.get("/")
    sc = Scenario.objects.get()
    client.post(reverse("production", args=[sc.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "2",
        "horizon_unit": "years", "stage_name": ["Fry"], "stage_max": ["5"],
        "stage_tanks": ["4"], "stage_ret": ["30"]})
    sc.refresh_from_db()
    return sc


def _scripted(monkeypatch, script):
    """script: list of (matcher-substring-in-system, reply). Consumed in order."""
    calls = {"n": 0}

    def fake_generate(provider, key, model, system, prompt, max_tokens=2000, thinking_level=""):
        for i, (marker, reply) in enumerate(script):
            if marker in system and not reply.endswith("#used"):
                script[i] = (marker, reply + "#used")
                calls["n"] += 1
                return reply
        raise AssertionError("no scripted reply for system:\n" + system[:200])
    monkeypatch.setattr("planner.agents.ai.generate", fake_generate)
    return calls


def test_mission_runs_specialists_and_applies_actions(client, mission_scenario, monkeypatch):
    sc = mission_scenario
    AISettings.objects.create(provider="zai", zai_key="zai-k", zai_model="glm-5.3")
    _scripted(monkeypatch, [
        ("You coordinate the specialist agents", _decision(calls=[
            {"agent": "brief_growth", "instruction": "hedef 4.5 g, 300k"},
            {"agent": "tanks_hydraulics", "instruction": "6 tank"}])),
        ("stocking and growth specialist", "Hedef ayarlandı.\n" + _action_block(
            [{"tool": "set_brief", "args": {"key": "target_weight_g", "value": 4.5}},
             {"tool": "set_brief", "args": {"key": "pieces_per_batch", "value": 300000}}])),
        ("tank and hydraulics specialist", "6 tank.\n" + _action_block(
            [{"tool": "set_stage", "args": {"unit": 1, "tanks": 6}}])),
        ("You coordinate the specialist agents", _decision(done="Sistem kuruldu.")),
    ])
    r = client.post(reverse("ai_mission"), data=json.dumps({"mission": "0.2→4.5g, 6 tank, 300k"}),
                    content_type="application/json").json()
    assert r["ok"] is True and r["summary"] == "Sistem kuruldu." and r["reload"] is True
    assert any("target_weight_g = 4.5" in a for a in r["applied"])
    assert any("6 tanks" in a for a in r["applied"])
    sc.refresh_from_db()
    assert sc.brief["target_weight_g"] == 4.5 and sc.brief["pieces_per_batch"] == 300000
    assert sc.production["stages"][0]["n_tanks"] == 6
    assert r["lamps"]


def test_mission_editable_agent_context_is_used(client, mission_scenario, monkeypatch):
    AISettings.objects.create(provider="zai", zai_key="zai-k")
    A.load_specs()          # seed defaults
    spec = AgentSpec.objects.get(name="tanks_hydraulics")
    spec.role = "CUSTOM MARKER ROLE"
    spec.save()
    seen = {}
    state = {"n": 0}

    def fake(provider, key, model, system, prompt, max_tokens=2000, thinking_level=""):
        if "Decide the next calls" in prompt:
            state["n"] += 1
            return _decision(done="ok") if state["n"] > 1 else _decision(
                calls=[{"agent": "tanks_hydraulics", "instruction": "kontrol et"}])
        seen["system"] = system
        return "anladım"
    monkeypatch.setattr("planner.agents.ai.generate", fake)
    r = client.post(reverse("ai_mission"), data=json.dumps({"mission": "x"}),
                    content_type="application/json").json()
    assert r["ok"] is True and "CUSTOM MARKER ROLE" in seen["system"]


def test_mission_disabled_specialist_is_not_dispatched(client, mission_scenario, monkeypatch):
    AISettings.objects.create(provider="zai", zai_key="zai-k")
    A.load_specs()
    AgentSpec.objects.filter(name="proposal_writer").update(enabled=False)
    orch, specialists, _ = A.load_specs()
    assert "proposal_writer" not in [s.name for s in specialists]


def test_mission_new_scenario_bootstrap(client, mission_scenario, monkeypatch):
    sc = mission_scenario
    AISettings.objects.create(provider="zai", zai_key="zai-k")
    _scripted(monkeypatch, [("You coordinate the specialist agents", _decision(done="kopyada tamam"))])
    r = client.post(reverse("ai_mission"), data=json.dumps(
        {"mission": "kur", "new_scenario": "Fry ordusu"}), content_type="application/json").json()
    assert r["ok"] is True and r["scenario_pk"] != sc.pk and r["redirect_url"].endswith("/brief/")
    assert Scenario.objects.count() == 2
    assert Scenario.objects.order_by("-pk").first().name == "Fry ordusu"


def test_mission_proposal_writer_tool(client, mission_scenario, monkeypatch):
    AISettings.objects.create(provider="zai", zai_key="zai-k")
    _scripted(monkeypatch, [
        ("You coordinate the specialist agents", _decision(calls=[{"agent": "proposal_writer", "instruction": "intro yaz"}])),
        ("You are the proposal writer", "Intro drafted in British English.\n" + _action_block(
            [{"tool": "proposal_set", "args": {"field": "intro", "value": "Suderra AS proposes a fry RAS."}},
             {"tool": "proposal_clause", "args": {"title": "Warranty", "text": "Two years."}}])),
        ("You coordinate the specialist agents", _decision(done="Teklif hazır.")),
    ])
    r = client.post(reverse("ai_mission"), data=json.dumps({"mission": "teklif yaz"}),
                    content_type="application/json").json()
    assert r["ok"] is True
    p = Proposal.objects.filter(scenario__isnull=False).order_by("-pk").first()
    assert p.intro == "Suderra AS proposes a fry RAS."
    assert any(c["title"] == "Warranty" for c in p.clauses)


def test_agents_page_lists_and_edits(client, mission_scenario):
    client.get("/")
    page = client.get(reverse("agents_page"))
    assert page.status_code == 200
    body = page.content.decode()
    assert "Orchestrator" in body and "Biofilter" in body and "mov" in body.lower()
    spec = AgentSpec.objects.get(name="biofilter")
    r = client.post(reverse("agents_page"), {"pk": spec.pk, "label": "Biyofiltre",
                                             "role": "MOVING BED ONLY", "enabled": ""})
    spec.refresh_from_db()
    assert spec.label == "Biyofiltre" and spec.role == "MOVING BED ONLY" and spec.enabled is False


def test_unit_reference_accepts_stage_name(client):
    client.get("/")
    sc = Scenario.objects.get()
    client.post(reverse("production", args=[sc.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "2",
        "horizon_unit": "years", "stage_name": ["Fry", "Smolt"], "stage_max": ["5", "100"],
        "stage_tanks": ["4", "6"], "stage_ret": ["30", "30"]})
    sc.refresh_from_db()
    from planner.views_ai import _apply_actions, _unit_ref
    assert _unit_ref(sc, "Fry") == 0 and _unit_ref(sc, "2") == 1 and _unit_ref(sc, "smolt") == 1
    with pytest.raises(ValueError):
        _unit_ref(sc, "Growout")
    applied, failed = _apply_actions(sc, [
        {"tool": "set_o2_source", "args": {"unit": "Fry", "pure": True}},
        {"tool": "set_equipment", "args": {"unit": "Smolt", "slot": "biofilter", "capacity_m3": 12.5}}])
    assert not failed and applied == ["unit 1 oxygen: pure O₂ (cone)", "unit 2 biofilter: custom 12.5 m³"]
    sc.refresh_from_db()
    assert sc.production["stages"][0]["overrides"]["gas.pure_oxygen"] is True
    uid2 = sc.unit_ids()[1]
    assert sc.equipment["stages"][uid2]["biofilter"]["capacity"] == 12.5


def test_truncated_decision_block_still_parses():
    nl = chr(10)
    text = ("karar:" + nl + "```json" + nl +
            '{"calls": [{"agent": "brief_growth", "instruction": "hedef 4.5"}]}')  # fence never closed
    d = A._parse_block(text)
    assert d == {"calls": [{"agent": "brief_growth", "instruction": "hedef 4.5"}]}
    assert A._parse_block("no block here") is None


def test_remove_stage_prunes_diagram_and_equipment(client):
    client.get("/")
    sc = Scenario.objects.get()
    client.post(reverse("production", args=[sc.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "2",
        "horizon_unit": "years", "stage_name": ["Fry", "Smolt"], "stage_max": ["5", "100"],
        "stage_tanks": ["4", "6"], "stage_ret": ["30", "30"]})
    sc.refresh_from_db()
    uid2 = sc.unit_ids()[1]
    sc.set_diagram(1, {"nodes": [{"id": "n1", "type": "tank", "x": 0, "y": 0}], "edges": []})
    sc.save()
    from planner.views_ai import _apply_actions
    applied, failed = _apply_actions(sc, [{"tool": "remove_stage", "args": {"unit": "Smolt"}}])
    assert not failed and applied and "removed RAS unit 'Smolt'" in applied[0]
    sc.refresh_from_db()
    stages = sc.production["stages"]
    assert [st["name"] for st in stages] == ["Fry"]
    assert uid2 not in (sc.process or {}) and uid2 not in (sc.equipment or {}).get("stages", {})
    applied, failed = _apply_actions(sc, [{"tool": "remove_stage", "args": {"unit": 1}}])
    assert "at least one" in failed[0]


def test_save_automatic_diagram_tool(client):
    client.get("/")
    sc = Scenario.objects.get()
    client.post(reverse("production", args=[sc.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "2",
        "horizon_unit": "years", "stage_name": ["Fry"], "stage_max": ["5"],
        "stage_tanks": ["4"], "stage_ret": ["30"]})
    sc.refresh_from_db()
    from planner.views_ai import _apply_actions
    applied, failed = _apply_actions(sc, [{"tool": "save_automatic_diagram", "args": {"unit": 1}}])
    assert not failed and applied and "automatic process diagram saved" in applied[0]
    sc.refresh_from_db()
    nodes = (sc.diagram_of(0) or {}).get("nodes") or []
    assert len(nodes) > 3
    from planner.services import diagram_results
    assert diagram_results(sc.diagram_of(0)) is not None


def test_every_default_agent_has_rules(client):
    client.get("/")
    A.load_specs()
    from planner.models import AgentSpec
    for spec in AgentSpec.objects.all():
        assert "YOUR RULES:" in spec.role or spec.name == "orchestrator", spec.name


def test_new_agents_seeded_with_knowledge(client):
    client.get("/")
    orch, specialists, _ = A.load_specs()
    names = {s.name for s in specialists}
    assert "microbial" in names and "energy" in names
    mic = next(s for s in specialists if s.name == "microbial")
    assert "NEVER as the last step before the tanks" in mic.role and "6 WEEKS" in mic.role
    en = next(s for s in specialists if s.name == "energy")
    assert "P~f^3" in en.role and "1.7-2.0 kWh/kg" in en.role


def test_calc_tool(client):
    client.get("/")
    from planner.views_ai import _apply_actions
    sc = Scenario.objects.get()
    applied, failed = _apply_actions(sc, [
        {"tool": "calc", "args": {"expr": "73.5 * 1.163 * 4 / 3"}}])
    assert not failed and "73.5 * 1.163 * 4 / 3 = 113.97" in applied[0]


def test_preview_tool(client):
    client.get("/")
    sc = Scenario.objects.get()
    client.post(reverse("production", args=[sc.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "1",
        "horizon_unit": "years", "stage_name": ["Fry"], "stage_max": ["5"],
        "stage_tanks": ["4"], "stage_ret": ["30"]})
    sc.refresh_from_db()
    from planner.views_ai import _apply_actions
    applied, failed = _apply_actions(sc, [
        {"tool": "preview", "args": {"params": {"bio.tan_target": 2.5}}}])
    assert not failed, failed
    assert any("peak_feed=" in a and "bio_media=" in a for a in applied)


def test_preview_unknown_param_rejected(client):
    client.get("/")
    sc = Scenario.objects.get()
    from planner.views_ai import _apply_actions
    _, failed = _apply_actions(sc, [{"tool": "preview", "args": {"params": {"no.such": 1}}}])
    assert any("unknown keys" in f for f in failed)


def test_update_agent_rejects_short_role(client):
    client.get("/")
    A.load_specs()
    sc = Scenario.objects.get()
    from planner.views_ai import _apply_actions
    applied, failed = _apply_actions(sc, [
        {"tool": "update_agent", "args": {"name": "biofilter", "role": "too short"}}],
        allow_agent_edit=True)
    assert not applied and any("min 20" in f for f in failed)
    assert AgentSpec.objects.get(name="biofilter").role != "too short"


def test_toggle_agent_requires_boolean(client):
    client.get("/")
    A.load_specs()
    sc = Scenario.objects.get()
    from planner.views_ai import _apply_actions
    applied, failed = _apply_actions(sc, [
        {"tool": "toggle_agent", "args": {"name": "economist", "enabled": "yes"}}],
        allow_agent_edit=True)
    assert not applied and any("boolean" in f for f in failed)
    assert AgentSpec.objects.get(name="economist").enabled is True
