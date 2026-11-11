"""LLM provider abstraction with automatic fallback to the rule engine.

Step 1 (done): interface, session key store, degraded tracking, status for the UI badge.
Step 5 (TODO): AnthropicProvider / OpenAIProvider implementations and failure classification.
Keys live only in process memory. Never write them to disk or logs.
"""
import time
from dataclasses import dataclass, field
from typing import Protocol

from app import config


class ProviderError(Exception):
    """Any LLM failure. `kind` drives fallback: auth | credit | rate_limit | timeout | bad_json | other."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


@dataclass
class ToolTurn:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)   # [{id, name, input}]
    stop: bool = False


class LLMProvider(Protocol):
    name: str

    def complete_json(self, system: str, user: str | list, schema: dict, timeout: float = 30) -> dict: ...
    def chat_with_tools(self, messages, tools, timeout: float = 45) -> ToolTurn: ...
    def vision_json(self, image_bytes: bytes, mime: str, prompt: str, schema: dict) -> dict: ...


# Session-scoped keys pasted in the UI: {"anthropic": "...", "openai": "..."}. Memory only.
_session_keys: dict[str, str] = {}
_degraded_until = 0.0
_degraded_reason = ""


def set_session_key(provider: str, key: str) -> None:
    global _degraded_until
    if provider not in ("anthropic", "openai"):
        raise ValueError("unknown provider")
    if key:
        _session_keys[provider] = key.strip()
    else:
        _session_keys.pop(provider, None)
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


def mark_degraded(reason: str, seconds: int = config.PROVIDER_DEGRADED_SECONDS) -> None:
    global _degraded_until, _degraded_reason
    _degraded_until = time.time() + seconds
    _degraded_reason = reason


def is_degraded() -> bool:
    return time.time() < _degraded_until


def get_provider() -> LLMProvider | None:
    """Returns a usable provider or None (-> rule engine)."""
    name = configured_provider()
    if not name or is_degraded():
        return None
    # TODO(step 5): return AnthropicProvider(_key_for(name), config.ANTHROPIC_MODEL) / OpenAIProvider(...)
    return None


def status() -> dict:
    """For the header badge and Settings page. Never includes the key itself."""
    name = configured_provider()
    if not name:
        return {"state": "not_set", "label": "AI: not set (rule engine)", "provider": None}
    if is_degraded():
        return {
            "state": "degraded",
            "label": f"AI unavailable ({_degraded_reason}). Running on rule engine.",
            "provider": name,
        }
    model = config.ANTHROPIC_MODEL if name == "anthropic" else config.OPENAI_MODEL
    if get_provider() is None:
        return {"state": "pending", "label": f"AI key set ({name}); agent not wired yet", "provider": name, "model": model}
    return {"state": "connected", "label": f"AI: {name}", "provider": name, "model": model}
