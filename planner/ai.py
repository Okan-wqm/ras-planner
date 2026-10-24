"""Text generation with the user's own API keys: Anthropic Claude, OpenAI or Z.ai (GLM).

OpenAI and Z.ai share the chat-completions format, so they are called over plain HTTPS;
Claude goes through the official SDK."""
import json
import urllib.error
import urllib.request

PROVIDERS = {
    "claude": {"label": "Claude (Anthropic)", "default_model": "claude-sonnet-5-5"},
    "openai": {"label": "OpenAI", "default_model": "gpt-5",
               "url": "https://api.openai.com/v1/chat/completions"},
    "zai": {"label": "Z.ai (GLM)", "default_model": "glm-4.6",
            "url": "https://api.z.ai/api/paas/v4/chat/completions"},
}


class AIError(Exception):
    pass


def generate(provider: str, api_key: str, model: str, system: str, prompt: str,
             max_tokens: int = 2000, thinking_level: str = "") -> str:
    if provider not in PROVIDERS:
        raise AIError(f"Unknown AI provider '{provider}'.")
    if not api_key:
        raise AIError(f"No API key for {PROVIDERS[provider]['label']}. Add it on the AI settings page.")
    model = model or PROVIDERS[provider]["default_model"]
    if provider == "claude":
        return _claude(api_key, model, system, prompt, max_tokens, effort=thinking_level)
    # newer OpenAI models take max_completion_tokens; Z.ai takes max_tokens
    limit = "max_completion_tokens" if provider == "openai" else "max_tokens"
    body = {limit: max_tokens}
    if provider == "zai" and model.startswith("glm-5"):
        # GLM-5.x always reasons and cannot be disabled; "low" keeps drafting tasks
        # from burning the whole token budget before writing the answer
        body["thinking"] = {"level": thinking_level or "low"}
    try:
        return _chat_completions(PROVIDERS[provider]["url"], api_key, model, system, prompt, body)
    except _ReasoningAteTheBudget:
        # reasoning models (GLM-5.x) can spend the whole budget thinking and return an empty
        # answer; one retry with a bigger budget usually fits the answer too
        try:
            return _chat_completions(PROVIDERS[provider]["url"], api_key, model, system, prompt,
                                     {**body, limit: max_tokens * 4})
        except _ReasoningAteTheBudget:
            raise AIError("The model spent its whole token budget thinking and ran out of "
                          "room for the answer. Try the task again or split it into smaller steps.")


class _ReasoningAteTheBudget(Exception):
    pass


def _claude(api_key, model, system, prompt, max_tokens, effort=""):
    import anthropic
    extra = {}
    if effort:
        # Claude Sonnet 4.5+ / 5.x: adaptive thinking + effort level (low/medium/high/max)
        extra["thinking"] = {"type": "adaptive"}
        extra["output_config"] = {"effort": effort}
    try:
        msg = anthropic.Anthropic(api_key=api_key).messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": prompt}], **extra)
    except anthropic.AuthenticationError:
        raise AIError("Claude rejected the API key.")
    except anthropic.NotFoundError:
        raise AIError(f"Claude does not know the model '{model}'. Change it on the AI settings page.")
    except anthropic.BadRequestError as exc:
        # older Claude models don't know adaptive thinking: retry without it
        if "adaptive" in str(exc) and extra:
            return _claude(api_key, model, system, prompt, max_tokens, effort="")
        raise AIError(f"Claude: {exc}")
    except anthropic.APIError as exc:
        raise AIError(f"Claude: {exc}")
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


def _chat_completions(url, api_key, model, system, prompt, body):
    payload = json.dumps({"model": model, **body, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
    try:
        # reasoning models can think for minutes on long prompts
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        if exc.code == 401:
            raise AIError("The API key was rejected.")
        raise AIError(f"HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AIError(f"Could not reach the AI service: {getattr(exc, 'reason', exc)}")
    try:
        message = data["choices"][0]["message"]
        content = (message["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise AIError(f"Unexpected answer: {str(data)[:300]}")
    if not content and message.get("reasoning_content"):
        raise _ReasoningAteTheBudget
    if not content:
        raise AIError("The model returned an empty answer. Try again or rephrase the task.")
    return content
