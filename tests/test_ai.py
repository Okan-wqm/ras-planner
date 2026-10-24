import io
import json

import pytest
from django.urls import reverse

from planner import ai
from planner.models import AISettings, Proposal, Scenario

pytestmark = pytest.mark.django_db


@pytest.fixture
def proposal(client):
    client.get("/")
    sc = Scenario.objects.get()
    client.post(reverse("production", args=[sc.pk]), {
        "start_date": "2027-01-01", "interval_days": "30", "horizon_value": "2", "horizon_unit": "years",
        "stage_name": ["Fry"], "stage_max": ["5"], "stage_tanks": ["4"], "stage_ret": ["30"]})
    client.post(reverse("proposals"), {"scenario": sc.pk})
    return Proposal.objects.get()


def test_keys_are_kept_masked_and_cleared(client):
    url = reverse("ai_settings")
    client.post(url, {"provider": "openai", "openai_key": "sk-proj-1234567890abcdef", "zai_key": "zai-key-123456789"})
    s = AISettings.get()
    assert s.openai_key == "sk-proj-1234567890abcdef" and s.provider == "openai"
    page = client.get(url).content.decode()
    assert "1234567890abcdef" not in page and "sk-proj…cdef" in page   # never shown again
    client.post(url, {"provider": "openai", "openai_key": "", "clear_zai": "on"})    # empty = keep
    s.refresh_from_db()
    assert s.openai_key == "sk-proj-1234567890abcdef" and s.zai_key == ""
    assert s.configured() == ["openai"]


def test_ai_draft_uses_scenario_facts(client, proposal, monkeypatch):
    AISettings.objects.create(provider="claude", claude_key="sk-ant-test")
    seen = {}

    def fake(provider, key, model, system, prompt, max_tokens=2000):
        seen.update(provider=provider, key=key, prompt=prompt, system=system)
        return "Suderra AS proposes…"
    monkeypatch.setattr("planner.views_docs.generate", fake)
    r = client.post(reverse("proposal_ai", args=[proposal.pk]),
                    {"task": "intro", "current": "Old intro", "instruction": "Mention 4 million eggs"}).json()
    assert r == {"ok": True, "text": "Suderra AS proposes…", "provider": "Claude (Anthropic)"}
    assert seen["provider"] == "claude" and seen["key"] == "sk-ant-test"
    assert "RAS unit Fry" in seen["prompt"] and "Old intro" in seen["prompt"] and "4 million eggs" in seen["prompt"]
    assert "never invent numbers" in seen["system"]


def test_ai_without_key_says_so(client, proposal):
    r = client.post(reverse("proposal_ai", args=[proposal.pk]), {"task": "review", "provider": "zai"}).json()
    assert r["ok"] is False and "No API key for Z.ai" in r["error"]


def test_openai_compatible_request(monkeypatch):
    sent = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        sent.update(url=req.full_url, body=json.loads(req.data), auth=req.headers["Authorization"])
        return Resp(json.dumps({"choices": [{"message": {"content": " Hello "}}]}).encode())
    monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
    assert ai.generate("openai", "k1", "", "sys", "hi") == "Hello"
    assert sent["url"].startswith("https://api.openai.com/") and sent["auth"] == "Bearer k1"
    assert sent["body"]["max_completion_tokens"] == 2000 and sent["body"]["model"] == "gpt-5"
    assert sent["body"]["messages"][0] == {"role": "system", "content": "sys"}
    ai.generate("zai", "k2", "glm-x", "sys", "hi")
    assert "z.ai" in sent["url"] and sent["body"]["max_tokens"] == 2000 and sent["body"]["model"] == "glm-x"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_reasoning_model_gets_one_budget_retry(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(json.loads(req.data)["max_tokens"])
        msg = ({"content": "", "reasoning_content": "thinking..."} if len(calls) == 1
               else {"content": " The answer. "})
        return _Resp(json.dumps({"choices": [{"message": msg}]}).encode())
    monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
    assert ai.generate("zai", "k", "glm-5.3", "sys", "hi") == "The answer."
    assert calls == [2000, 8000]      # one retry with 4x the budget


def test_persistently_empty_answer_is_an_error(monkeypatch):
    def fake_urlopen(req, timeout):
        return _Resp(json.dumps({"choices": [{"message": {"content": "", "reasoning_content": "..."}}]}).encode())
    monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ai.AIError, match="budget"):
        ai.generate("zai", "k", "glm-5.3", "sys", "hi")


def test_read_timeout_becomes_a_friendly_error(monkeypatch):
    import socket

    def fake_urlopen(req, timeout):
        raise TimeoutError("The read operation timed out")
    monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ai.AIError, match="Could not reach"):
        ai.generate("zai", "k", "glm-5.3", "sys", "hi")


def test_glm5_drafts_use_low_thinking(monkeypatch):
    sent = []

    def fake_urlopen(req, timeout):
        sent.append(json.loads(req.data))
        return _Resp(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())
    monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
    ai.generate("zai", "k", "glm-5.3", "sys", "hi")
    ai.generate("zai", "k", "glm-4.6", "sys", "hi")
    assert sent[0]["thinking"] == {"level": "low"}          # reasoning generation
    assert "thinking" not in sent[1]                        # plain models untouched


def _chat(client, **payload):
    return client.post(reverse("ai_chat"), data=json.dumps(payload),
                       content_type="application/json").json()


def test_chat_panel_is_on_every_screen(client):
    client.get("/")
    sc = Scenario.objects.get()
    page = client.get(reverse("brief", args=[sc.pk])).content.decode()
    assert 'id="ai-panel"' in page and 'id="ai-toggle"' in page and "/ai/chat/" in page
    assert 'id="ai-model"' in page and 'id="ai-thinking"' in page  # dynamic selectors
    assert "EFFORTS" in page and "'medium'" in page   # Claude effort choices in JS


def test_ai_chat_sends_page_context_and_thinking_level(client, monkeypatch):
    client.get("/")
    sc = Scenario.objects.get()
    AISettings.objects.create(provider="zai", zai_key="zai-k", zai_model="glm-5.3")
    seen = {}

    def fake(provider, key, model, system, prompt, max_tokens=2000, thinking_level=""):
        seen.update(prompt=prompt, system=system, level=thinking_level, provider=provider)
        return "Yanıt: plan 30 günde bir parti."
    monkeypatch.setattr("planner.views_ai.ai.generate", fake)
    r = _chat(client, message="Planı özetle", path=f"/scenario/{sc.pk}/process/",
              history=[{"role": "user", "content": "merhaba"},
                       {"role": "assistant", "content": "selam"}], thinking="high")
    assert r["ok"] is True and r["thinking"] == "high" and "Z.ai" in r["provider"]
    assert "05 Process design" in seen["prompt"] and sc.name in seen["prompt"]
    assert "merhaba" in seen["prompt"] and "Planı özetle" in seen["prompt"]
    assert "never invent" in seen["system"].lower()
    _chat(client, message="tekrar", path="/", thinking="banana")
    assert seen["level"] == "low"                     # invalid falls back


def test_ai_chat_input_guards(client):
    client.get("/")
    assert client.get(reverse("ai_chat")).status_code == 405
    r = client.post(reverse("ai_chat"), data="[]", content_type="application/json")
    assert r.status_code == 400
    assert _chat(client, message="x" * 8001, path="/")["ok"] is False
    AISettings.objects.create(provider="claude")      # no key anywhere
    r = _chat(client, message="selam", path="/")
    assert r["ok"] is False and "No API key" in r["error"]


def test_ai_chat_changes_a_constant(client, monkeypatch):
    client.get("/")
    sc = Scenario.objects.get()
    AISettings.objects.create(provider="zai", zai_key="zai-k", zai_model="glm-5.3")

    def fake(provider, key, model, system, prompt, max_tokens=2000, thinking_level=""):
        return ("CO2 hedefini 8'e yükselttim.\n```json\n" +
                json.dumps({"actions": [{"tool": "set_param", "args": {"key": "flow.co2_max", "value": 8}}]}) +
                "\n```")
    monkeypatch.setattr("planner.views_ai.ai.generate", fake)
    r = _chat(client, message="CO2 sınırını 8 yap", path=f"/scenario/{sc.pk}/requirements/")
    assert r["ok"] is True and r["applied"] == ["flow.co2_max = 8"] and r["reload"] is True
    assert "```json" not in r["text"] and "CO2" in r["text"]
    sc.refresh_from_db()
    assert sc.params["flow.co2_max"] == 8.0
    assert "lamps" in r and "02" in r["lamps"]


def test_ai_action_validation(client, monkeypatch):
    client.get("/")
    sc = Scenario.objects.get()
    AISettings.objects.create(provider="zai", zai_key="k")
    before = dict(sc.params or {})

    def fake(*a, **k):
        return "deneme\n```json\n" + json.dumps({"actions": [
            {"tool": "set_param", "args": {"key": "no.such_key", "value": 1}},
            {"tool": "make_coffee", "args": {}},
            {"tool": "set_brief", "args": {"key": "customer", "value": 5}},
        ]}) + "\n```"
    monkeypatch.setattr("planner.views_ai.ai.generate", fake)
    r = _chat(client, message="şunları dene", path=f"/scenario/{sc.pk}/brief/")
    assert r["ok"] is True and r["applied"] == [] and len(r["failed"]) == 3 and r["reload"] is False
    sc.refresh_from_db()
    assert sc.params == before                      # nothing written


def test_ai_action_caps_and_reset(client, monkeypatch):
    client.get("/")
    sc = Scenario.objects.get()
    AISettings.objects.create(provider="zai", zai_key="k")
    client.post(reverse("units", args=[sc.pk]), {   # two units so a diagram exists
        "stage_name": ["A", "B"], "stage_max": ["10", "50"],
        "stage_tanks": ["2", "2"], "stage_ret": ["30", "30"]})
    sc.refresh_from_db()
    sc.set_diagram(0, {"nodes": [{"id": "n1", "type": "tank", "x": 0, "y": 0}], "edges": []})
    sc.save()

    def fake(*a, **k):
        acts = [{"tool": "reset_diagram", "args": {"unit": 1}}]
        acts += [{"tool": "set_param", "args": {"key": "growth.tgc", "value": 1 + i}} for i in range(7)]
        return "ok\n```json\n" + json.dumps({"actions": acts}) + "\n```"
    monkeypatch.setattr("planner.views_ai.ai.generate", fake)
    r = _chat(client, message="hepsini yap", path=f"/scenario/{sc.pk}/process/")
    assert len(r["applied"]) == 5                                   # capped at 5
    assert any("reset to automatic" in a for a in r["applied"])
    sc.refresh_from_db()
    assert sc.diagram_of(0) in (None, {}) or not (sc.diagram_of(0) or {}).get("nodes")


def test_chat_honours_provider_pick(client, monkeypatch):
    client.get("/")
    AISettings.objects.create(provider="zai", zai_key="zai-k", claude_key="cl-k")
    seen = {}

    def fake(provider, key, model, system, prompt, max_tokens=2000, thinking_level=""):
        seen["provider"] = provider
        return "tamam"
    monkeypatch.setattr("planner.views_ai.ai.generate", fake)
    r = _chat(client, message="selam", path="/", provider="claude")
    assert r["ok"] is True and seen["provider"] == "claude"
    _chat(client, message="selam", path="/", provider="openai")   # no key: falls back
    assert seen["provider"] in ("zai", "claude")


def test_chat_rewrites_an_agent_context(client, monkeypatch):
    client.get("/")
    AISettings.objects.create(provider="zai", zai_key="zai-k")
    from planner.agents import load_specs
    from planner.models import AgentSpec
    load_specs()
    new_role = "You are the biofilter specialist.\nYOUR RULES:\n- always prefer moving bed."
    assert AgentSpec.objects.get(name="biofilter").role != new_role

    def fake(*a, **k):
        return "Degistirdim.\n```json\n" + json.dumps({"actions": [
            {"tool": "update_agent", "args": {"name": "biofilter", "role": new_role}},
            {"tool": "toggle_agent", "args": {"name": "economist", "enabled": False}}]}) + "\n```"
    monkeypatch.setattr("planner.views_ai.ai.generate", fake)
    r = _chat(client, message="biyofiltre agentini soyle degistir", path="/")
    assert r["ok"] is True and any("biofilter" in a for a in r["applied"])
    assert AgentSpec.objects.get(name="biofilter").role == new_role
    assert AgentSpec.objects.get(name="economist").enabled is False
