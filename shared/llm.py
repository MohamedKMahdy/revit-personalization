"""
Central model / provider switch for every LLM call in the system.

Pick the model for each role by a short alias or a full id:
    opus | sonnet | haiku   → Claude, DIRECT to the Anthropic API (pristine + prompt-cached;
                              the thesis system of record)
    gemini | gemini-pro     → Gemini, via a local LiteLLM proxy that speaks the Anthropic
                              Messages API, so the same `anthropic` SDK code runs unchanged —
                              only the client base_url + the model name change.

Switching is one env var per role, or a global default:
    EXECUTOR_MODEL / CHATBOT_MODEL / PATTERN_AGENT_MODEL / MACRO_AGENT_MODEL  (role override)
    LLM_MODEL_DEFAULT                                                        (global fallback)
e.g.  LLM_MODEL_DEFAULT=gemini   → everything on Gemini (free tier)
      EXECUTOR_MODEL=opus        → just the executor on Opus

Claude stays direct so the reported runs are not routed through a third-party proxy and keep
prompt caching. Gemini is opt-in; it doesn't support Anthropic prompt caching or the `thinking`
parameter, so callers consult is_gemini()/supports_thinking() to drop those.
"""
from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    """Load the project .env into the environment (keys not already set), so the model switch,
    GEMINI_API_KEY, etc. can be configured from .env without an external dependency. An explicit
    shell variable always wins. Runs once on import."""
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        env = parent / ".env"
        if env.exists():
            try:
                for line in env.read_text(encoding="utf-8").splitlines():
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    k = k.strip()
                    if k and k not in os.environ:
                        os.environ[k] = v.strip().strip('"').strip("'")
            except Exception:
                pass
            return


_load_dotenv()

# alias → the model string actually sent on the request.
# Gemini names are the public model_name the LiteLLM proxy serves (see litellm_config.yaml).
ALIASES = {
    "opus":         "claude-opus-4-8",
    "sonnet":       "claude-sonnet-4-6",
    "haiku":        "claude-haiku-4-5",
    "gemini":       "gemini-flash",
    "gemini-flash": "gemini-flash",
    "gemini-pro":   "gemini-pro",
}

LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://127.0.0.1:4000")
_LITELLM_KEY = os.environ.get("LITELLM_API_KEY", "sk-litellm-local")  # dummy unless proxy needs auth


def resolve(model: str) -> str:
    """Alias → concrete model id; an explicit id passes straight through."""
    m = (model or "").strip()
    return ALIASES.get(m.lower(), m)


def is_gemini(model_id: str) -> bool:
    """Does this model route through the LiteLLM/Gemini proxy rather than direct to Anthropic?"""
    return (model_id or "").lower().startswith("gemini")


def supports_thinking(model_id: str) -> bool:
    """The Anthropic `thinking` parameter is Claude-only — drop it for Gemini."""
    return not is_gemini(model_id)


def supports_prompt_caching(model_id: str) -> bool:
    """Anthropic `cache_control` is Claude-only — strip it for Gemini."""
    return not is_gemini(model_id)


def pick(role_env: str, default: str) -> str:
    """Resolved model id for a role: its own env var → LLM_MODEL_DEFAULT → built-in default."""
    raw = os.environ.get(role_env) or os.environ.get("LLM_MODEL_DEFAULT") or default
    return resolve(raw)


def _client_kwargs(model_id: str) -> dict:
    if is_gemini(model_id):
        # Route through the local LiteLLM proxy (Anthropic Messages format → Gemini).
        return {"base_url": LITELLM_BASE_URL, "api_key": _LITELLM_KEY}
    return {}  # direct to Anthropic; ANTHROPIC_API_KEY resolved from the environment


def client(model_id: str):
    """A sync `anthropic` client wired to the right backend for `model_id`."""
    import anthropic
    return anthropic.Anthropic(**_client_kwargs(model_id))


def async_client(model_id: str):
    """An async `anthropic` client wired to the right backend for `model_id`."""
    import anthropic
    return anthropic.AsyncAnthropic(**_client_kwargs(model_id))
