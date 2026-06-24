"""
Tests for the central model/provider switch (shared/llm.py): alias resolution, provider
detection, env precedence, and client routing (Claude direct vs Gemini via the LiteLLM proxy).
No network / no API key required.

Run:  pytest tests/test_llm.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from shared import llm  # noqa: E402


def test_resolve_aliases():
    assert llm.resolve("opus") == "claude-opus-4-8"
    assert llm.resolve("sonnet") == "claude-sonnet-4-6"
    assert llm.resolve("haiku") == "claude-haiku-4-5"
    assert llm.resolve("gemini") == "gemini-flash"
    assert llm.resolve("gemini-pro") == "gemini-pro"
    assert llm.resolve("GEMINI") == "gemini-flash"               # case-insensitive
    assert llm.resolve("claude-sonnet-4-6") == "claude-sonnet-4-6"  # explicit id passes through


def test_provider_detection():
    assert llm.is_gemini("gemini-flash") is True
    assert llm.is_gemini("gemini-pro") is True
    assert llm.is_gemini("claude-opus-4-8") is False
    # thinking + prompt caching are Claude-only
    assert llm.supports_thinking("claude-opus-4-8") is True
    assert llm.supports_thinking("gemini-flash") is False
    assert llm.supports_prompt_caching("claude-sonnet-4-6") is True
    assert llm.supports_prompt_caching("gemini-flash") is False


def test_pick_precedence(monkeypatch):
    monkeypatch.delenv("EXECUTOR_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL_DEFAULT", raising=False)
    assert llm.pick("EXECUTOR_MODEL", "sonnet") == "claude-sonnet-4-6"   # built-in default
    monkeypatch.setenv("LLM_MODEL_DEFAULT", "gemini")
    assert llm.pick("EXECUTOR_MODEL", "sonnet") == "gemini-flash"        # global override
    monkeypatch.setenv("EXECUTOR_MODEL", "opus")
    assert llm.pick("EXECUTOR_MODEL", "sonnet") == "claude-opus-4-8"     # role override wins


def test_client_routing():
    assert llm._client_kwargs("claude-sonnet-4-6") == {}                 # direct to Anthropic
    kw = llm._client_kwargs("gemini-flash")
    assert kw["base_url"] == llm.LITELLM_BASE_URL and kw["api_key"]      # routed via the proxy
    # A Gemini client can be constructed with no ANTHROPIC_API_KEY (it carries base_url + a dummy key)
    c = llm.client("gemini-flash")
    assert str(c.base_url).rstrip("/").startswith("http")
