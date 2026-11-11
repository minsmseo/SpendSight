"""LLM provider abstraction with automatic fallback to the rule engine.

`get_provider()` returns Anthropic if an Anthropic key is present, else OpenAI, else None.
Any failure surfaces as ProviderError; callers report it with `report_failure` and carry on with
the rule engine. Keys live only in process memory and are never logged or written to disk.
"""
import base64
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

from app import config

log = logging.getLogger("spendsight.ai")

REASON_LABELS = {
    "auth": "invalid API key",
    "credit": "out of credit",
    "rate_limit": "rate limited",
    "timeout": "timed out",
    "refusal": "request declined",
    "bad_json": "invalid response",
    "network": "network error",
    "other": "provider error",
}
# How long to stop calling the provider after each kind of failure (seconds).
DEGRADE_SECONDS = {"auth": config.PROVIDER_DEGRADED_SECONDS, "credit": config.PROVIDER_DEGRADED_SECONDS, "rate_limit": 60}


class ProviderError(Exception):
    """Any LLM failure. `kind` is a key of REASON_LABELS."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind if kind in REASON_LABELS else "other"


@dataclass
class ToolTurn:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)   # [{id, name, input}]
    raw_assistant: dict | None = None                       # provider-native message to append to history


class LLMProvider(Protocol):
    """Messages are provider-native; the agent only appends `raw_assistant` and
    `tool_result_messages(...)`. Tools use the neutral shape {name, description, input_schema}."""

    name: str
    model: str

    def complete_json(self, system: str, user: str | list, schema: dict, timeout: float = 30) -> dict: ...
    def chat_with_tools(self, messages: list, tools: list[dict], timeout: float = 45, system: str = "") -> ToolTurn: ...
    def tool_result_messages(self, results: list[dict]) -> list[dict]: ...   # results: [{id, content, is_error}]
    def vision_json(self, image_bytes: bytes, mime: str, prompt: str, schema: dict) -> dict: ...


def parse_json_text(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


# --- Anthropic --------------------------------------------------------------
class AnthropicProvider:
    name = "anthropic"
    # Models that accept the server-side refusal fallback (re-runs a declined request on another model).
    FALLBACK_MODELS = ("claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
    FALLBACK_BETA = "server-side-fallback-2026-07-01"
    MAX_TOKENS = 16000

    def __init__(self, api_key: str, model: str):
        import anthropic

        self._sdk = anthropic
        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=1)
        supports = "fallbacks" in inspect.signature(self._client.beta.messages.create).parameters
        self._use_fallbacks = supports and model in self.FALLBACK_MODELS

    def _create(self, *, timeout: float, effort: str, schema: dict | None = None, **kwargs):
        """One Messages API call. Thinking is left to the model default (adaptive); depth is set by effort."""
        sdk = self._sdk
        output_config: dict = {"effort": effort}
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        kwargs.update(model=self.model, max_tokens=self.MAX_TOKENS, output_config=output_config, timeout=timeout)
        try:
            if self._use_fallbacks:
                response = self._client.beta.messages.create(betas=[self.FALLBACK_BETA], fallbacks="default", **kwargs)
            else:
                response = self._client.messages.create(**kwargs)
        except (sdk.AuthenticationError, sdk.PermissionDeniedError) as exc:
            raise ProviderError("auth", exc.message)
        except sdk.RateLimitError as exc:
            raise ProviderError("rate_limit", exc.message)
        except sdk.APIStatusError as exc:
            low = (exc.message or "").lower()
            if exc.status_code == 402 or "credit balance" in low or "billing" in low:
                raise ProviderError("credit", exc.message)
            raise ProviderError("other", f"HTTP {exc.status_code}: {exc.message}")
        except sdk.APITimeoutError:
            raise ProviderError("timeout", f"no response within {timeout:.0f}s")
        except sdk.APIConnectionError:
            raise ProviderError("network", "could not reach the Anthropic API")
        if response.stop_reason == "refusal":
            raise ProviderError("refusal", "the model declined this request")
        if response.stop_reason == "max_tokens":
            raise ProviderError("other", "response was cut off at max_tokens")
        return response

    @staticmethod
    def _text(response) -> str:
        return "".join(b.text for b in response.content if b.type == "text")

    def _json(self, system: str, content, schema: dict, timeout: float, effort: str = "low") -> dict:
        messages = [{"role": "user", "content": content}]
        response = self._create(timeout=timeout, effort=effort, schema=schema, system=system, messages=messages)
        try:
            return parse_json_text(self._text(response))
        except ValueError:
            pass
        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": "That was not valid JSON for the schema. Reply again with only the JSON object."},
        ]
        response = self._create(timeout=timeout, effort=effort, schema=schema, system=system, messages=messages)
        try:
            return parse_json_text(self._text(response))
        except ValueError:
            raise ProviderError("bad_json", "model did not return valid JSON")

    def complete_json(self, system: str, user: str | list, schema: dict, timeout: float = 30) -> dict:
        return self._json(system, user, schema, timeout)

    def vision_json(self, image_bytes: bytes, mime: str, prompt: str, schema: dict, timeout: float = 45) -> dict:
        data = base64.standard_b64encode(image_bytes).decode("ascii")
        kind = "document" if mime == "application/pdf" else "image"
        content = [
            {"type": kind, "source": {"type": "base64", "media_type": mime, "data": data}},
            {"type": "text", "text": prompt},
        ]
        return self._json("You read receipts precisely and never guess.", content, schema, timeout)

    def chat_with_tools(self, messages: list, tools: list[dict], timeout: float = 45, system: str = "") -> ToolTurn:
        response = self._create(timeout=timeout, effort="medium", system=system, messages=messages, tools=tools)
        calls = [{"id": b.id, "name": b.name, "input": b.input} for b in response.content if b.type == "tool_use"]
        # Keep the full content (thinking and fallback blocks included) so the next request replays it unchanged.
        return ToolTurn(text=self._text(response), tool_calls=calls,
                        raw_assistant={"role": "assistant", "content": response.content})

    def tool_result_messages(self, results: list[dict]) -> list[dict]:
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"], "is_error": bool(r.get("is_error"))}
            for r in results
        ]}]


# --- selection, session keys, degraded state ----------------------------------
_session_keys: dict[str, str] = {}      # {"anthropic": "...", "openai": "..."}; memory only
_providers: dict[tuple, LLMProvider] = {}
_degraded_until = 0.0
_degraded_reason = ""


def set_session_key(provider: str, key: str) -> None:
    global _degraded_until
    if provider not in ("anthropic", "openai"):
        raise ValueError("unknown provider")
    if key and key.strip():
        _session_keys[provider] = key.strip()
    else:
        _session_keys.pop(provider, None)
    _providers.clear()
    _degraded_until = 0.0


def _key_for(provider: str) -> str:
    env = config.ANTHROPIC_API_KEY if provider == "anthropic" else config.OPENAI_API_KEY
    return _session_keys.get(provider) or env


def configured_provider() -> str | None:
    """Anthropic wins if both keys are present."""
    for name in ("anthropic", "openai"):
        if _key_for(name):
            return name
    return None


def model_for(name: str) -> str:
    return config.ANTHROPIC_MODEL if name == "anthropic" else config.OPENAI_MODEL


def mark_degraded(reason: str, seconds: float = config.PROVIDER_DEGRADED_SECONDS) -> None:
    global _degraded_until, _degraded_reason
    _degraded_until = time.time() + seconds
    _degraded_reason = REASON_LABELS.get(reason, reason)


def is_degraded() -> bool:
    return time.time() < _degraded_until


def report_failure(exc: Exception) -> str:
    """Record an LLM failure; auth / credit / rate-limit errors pause the provider. Returns the kind."""
    kind = exc.kind if isinstance(exc, ProviderError) else "other"
    log.warning("AI call failed (%s); using the rule engine", REASON_LABELS[kind])   # never log keys or prompts
    if kind in DEGRADE_SECONDS:
        mark_degraded(kind, DEGRADE_SECONDS[kind])
    return kind


def build_provider(name: str) -> LLMProvider | None:
    """Provider for the configured key, ignoring the degraded state (used by the Settings test button)."""
    key = _key_for(name)
    if not key:
        return None
    cache_key = (name, key, model_for(name))
    if cache_key not in _providers:
        try:
            if name == "anthropic":
                _providers[cache_key] = AnthropicProvider(key, model_for(name))
            else:
                from app.ai.openai_provider import OpenAIProvider

                _providers[cache_key] = OpenAIProvider(key, model_for(name))
        except Exception:
            log.warning("could not initialise the %s client", name)
            return None
    return _providers[cache_key]


def get_provider() -> LLMProvider | None:
    """A usable provider, or None (-> rule engine)."""
    name = configured_provider()
    if not name or is_degraded():
        return None
    return build_provider(name)


def status() -> dict:
    """For the header badge and Settings page. Never includes the key itself."""
    name = configured_provider()
    if not name:
        return {"state": "not_set", "label": "AI: not set (rule engine)", "provider": None}
    model = model_for(name)
    if is_degraded():
        return {
            "state": "degraded",
            "label": f"AI unavailable ({_degraded_reason}). Running on rule engine.",
            "provider": name, "model": model,
        }
    return {"state": "connected", "label": f"AI: {name} · {model}", "provider": name, "model": model}
