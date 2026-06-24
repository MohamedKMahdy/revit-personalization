# Switching models / providers

Every LLM call in the system goes through `shared/llm.py`, so you can switch each agent between
**Claude** (Opus / Sonnet / Haiku, direct to the Anthropic API) and **Gemini** (free tier, via a
local LiteLLM proxy) with one environment variable — no code changes.

## The four roles

| Role | Env var | Default |
|------|---------|---------|
| Executor (agentic self-healing loop) | `EXECUTOR_MODEL` | `sonnet` |
| Chatbot (conversational replies) | `CHATBOT_MODEL` | `sonnet` |
| Pattern Agent (motif extraction — the thesis contribution) | `PATTERN_AGENT_MODEL` | `opus` |
| Macro Agent (tool-sequence generation) | `MACRO_AGENT_MODEL` | `sonnet` |

Each accepts an **alias** (`opus`, `sonnet`, `haiku`, `gemini`, `gemini-pro`) or a full model id.
`LLM_MODEL_DEFAULT` is the global fallback for any role you don't set.

```powershell
$env:LLM_MODEL_DEFAULT = "gemini"   # whole system on Gemini's free tier
$env:EXECUTOR_MODEL    = "gemini"   # just the executor
$env:CHATBOT_MODEL     = "opus"     # just the chatbot back to Opus
```

Claude routes **direct** (pristine + prompt-cached — the reported system of record). Gemini routes
through the proxy and automatically drops the Claude-only `thinking` and `cache_control` params.

## Using Gemini (one-time setup)

1. Get a free key at Google AI Studio → set it: `$env:GEMINI_API_KEY = "..."`
2. `pip install --user "litellm[proxy]"`
3. Start the proxy: `litellm --config litellm_config.yaml --port 4000`
4. Point any role at `gemini` (above) and run as normal.

## Caveats (read before relying on it)

- **Develop on one model, report on another = a validity gap.** Keep prompts model-neutral, and
  report thesis results on the model you actually tuned on (Claude, by default).
- Free tier is **Flash-class** — weaker/different at long agentic tool-use + reasoning than Claude
  Sonnet/Opus; the executor's recovery behavior was tuned for Claude.
- Free-tier **rate limits** (~1,500 req/day) can throttle batch eval runs.
- In the **EEA/UK/CH** the free tier gets the paid-tier (no-training) data policy; elsewhere the
  free tier may train on your inputs — don't send sensitive data there.

## Recommended use

Use the switch as a **deliberate model-portability ablation** ("the pipeline generalizes across
model families; the motif is the stable interface") — a thesis *strength* — not a silent dev
substitute. Claude stays the primary, reported system.
