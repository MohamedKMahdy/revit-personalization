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


# Gemini's FREE tier is rate-limited per MINUTE (~10-15 requests/min on Flash), and the executor's
# agentic loop fires one request per step — so a multi-step routine bursts past the cap and gets a
# 429. Let the SDK absorb that: more retries + exponential backoff (~1,2,4,8,16,32s) spans the 60s
# window, so the run pauses and recovers instead of dying. Override with GEMINI_MAX_RETRIES.
_GEMINI_MAX_RETRIES = int(os.environ.get("GEMINI_MAX_RETRIES", "6"))


def _client_kwargs(model_id: str) -> dict:
    if is_gemini(model_id):
        # Route through the local LiteLLM proxy (Anthropic Messages format → Gemini).
        return {"base_url": LITELLM_BASE_URL, "api_key": _LITELLM_KEY,
                "max_retries": _GEMINI_MAX_RETRIES}
    return {}  # direct to Anthropic; ANTHROPIC_API_KEY resolved from the environment


# Approximate USD per 1M tokens: (input, output, cache_read, cache_write). These are CONFIGURABLE
# estimates for cost logging/telemetry, not billing — verify against https://www.anthropic.com/pricing
# before quoting them in the thesis. Anthropic's structure is cache_read ~0.1x input, cache_write
# ~1.25x input. Gemini runs on the FREE tier here, so it is costed at 0 (see price()).
PRICING = {
    "claude-opus-4-8":   (15.0, 75.0, 1.50, 18.75),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75),
    "claude-haiku-4-5":  (1.0, 5.0, 0.10, 1.25),
}


def price(model_id: str) -> tuple:
    """(input, output, cache_read, cache_write) USD/MTok for a model. Gemini free tier → all zero;
    an unknown Claude id falls back to Sonnet rates so cost is never silently undercounted."""
    m = resolve(model_id)
    if is_gemini(m):
        return (0.0, 0.0, 0.0, 0.0)
    return PRICING.get(m, PRICING["claude-sonnet-4-6"])


def est_cost_usd(model_id: str, usage: dict) -> float:
    """Estimated USD for a run's token usage {input, output, cache_read, cache_write} on `model_id`.
    Routes through price(), so a Gemini run correctly costs 0 instead of being billed at Claude rates."""
    pin, pout, pcr, pcw = price(model_id)
    u = usage or {}
    return round((u.get("input", 0) * pin + u.get("output", 0) * pout
                  + u.get("cache_read", 0) * pcr + u.get("cache_write", 0) * pcw) / 1e6, 6)


def client(model_id: str):
    """A sync `anthropic` client wired to the right backend for `model_id`."""
    import anthropic
    return anthropic.Anthropic(**_client_kwargs(model_id))


def async_client(model_id: str):
    """An async `anthropic` client wired to the right backend for `model_id`."""
    import anthropic
    return anthropic.AsyncAnthropic(**_client_kwargs(model_id))
