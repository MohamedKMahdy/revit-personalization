"""
BIM Pattern Chatbot Server
==========================
Serves a streaming chat UI at http://localhost:5000

The flow:
  1. RevitLogger / orchestrator detects a repeated pattern
  2. It calls POST /api/pattern with the motif + tool_sequence
  3. The pattern is SAVED to a browsable history (one conversation each)
  4. The chat UI opens; Claude greets the user, presents the pattern
  5. User can ask questions, change parameters, then confirm or dismiss
  6. On confirmation  → POST /api/execute → revit_bridge.execute_shortcut()
  7. Result shown in the chat
  8. A new detection does NOT destroy the old one — every detected pattern
     stays in the left-hand history and can be re-opened, with its full
     conversation, at any time.

Run standalone (with sample data for testing):
  python chatbot/chat_server.py

Trigger programmatically:
  from chatbot.trigger import notify_pattern
  notify_pattern(label="...", count=5, motif={...}, tool_sequence=[...])
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
import webbrowser
from pathlib import Path
from typing import AsyncIterator

import anthropic
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))
from mcp_server.revit_bridge import execute_shortcut, _extract_element_id, _call_plugin, pick_point
from orchestrator.executor_agent import run_executor, build_goal

# ── Config ────────────────────────────────────────────────────────────────────
PORT  = int(os.environ.get("CHATBOT_PORT", "5000"))
MODEL = "claude-opus-4-8"

# Where the detected-pattern history is persisted (survives server restarts).
HISTORY_PATH = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                / "RevitPersonalization" / "pattern_history.json")

# ── App + Anthropic client ────────────────────────────────────────────────────
def _load_api_key() -> None:
    """Ensure ANTHROPIC_API_KEY is set before constructing the client.

    The server is often launched WITHOUT the key in its environment — e.g. by the
    BIMAssistant add-in (it inherits Revit's environment) or via pythonw. Read the
    key from the project .env (or the %LOCALAPPDATA% .env) so the Anthropic client
    can authenticate; otherwise every chat/greeting fails with 'Could not resolve
    authentication method'.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    candidates = [
        Path(__file__).resolve().parent.parent / ".env",          # project .env
        Path(os.environ.get("LOCALAPPDATA", str(Path.home())))    # add-in .env
        / "RevitPersonalization" / ".env",
    ]
    for env in candidates:
        try:
            if not env.exists():
                continue
            for line in env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("ANTHROPIC_API_KEY="):
                    os.environ["ANTHROPIC_API_KEY"] = (
                        line.split("=", 1)[1].strip().strip('"').strip("'"))
                    return
        except Exception:
            continue


_load_api_key()

app     = FastAPI(title="BIM Pattern Assistant")
_client = anthropic.AsyncAnthropic()

# ═══════════════════════════════════════════════════════════════════════════════
# Pattern store
# ═══════════════════════════════════════════════════════════════════════════════
# Each detected pattern is its own record so the user can browse a HISTORY of
# them and re-open any one with its conversation intact. A record is:
#   {
#     id, label, count, motif, tool_sequence, examples,
#     detected_at,                    # epoch seconds
#     status,                         # "new" | "seen" | "executed" | "dismissed"
#     history,                        # Anthropic messages (per-pattern conversation)
#     last_element_id,                # element placed by this pattern's last execution
#     pending_location,               # {x,y,z} parsed from this pattern's conversation
#   }
_patterns: dict[str, dict] = {}     # id -> record
_active_id: str | None = None       # which pattern a fresh client loads first
_locks: dict[str, asyncio.Lock] = {}  # id -> lock; serializes streams per pattern


def _lock_for(pid: str) -> asyncio.Lock:
    """One lock per pattern so two requests can't interleave turns into the same
    conversation (which would produce non-alternating roles → Anthropic 400)."""
    lock = _locks.get(pid)
    if lock is None:
        lock = _locks[pid] = asyncio.Lock()
    return lock

_TOKEN_RE = re.compile(
    r"##EXECUTE##|##DISMISS##|##ISOLATE##|##ZOOM##|##SELECT##|##PICK##|##LOCATION:[^#]*##"
)


def _strip_tokens(text: str) -> str:
    return _TOKEN_RE.sub("", text).strip()


def _derive_id(payload: dict) -> str:
    """Stable id for a detection. Prefer the detector's routine id; otherwise
    derive one from the label + tool shape so the SAME routine re-detected maps
    to the SAME history entry (updates it instead of duplicating)."""
    rid = payload.get("id")
    if rid:
        return str(rid)
    sig = (payload.get("label", "") + "|"
           + json.dumps([s.get("tool") for s in payload.get("tool_sequence", [])]))
    return "pat_" + hashlib.sha1(sig.encode("utf-8")).hexdigest()[:12]


def _has_user_turns(rec: dict | None) -> bool:
    """True once the user has actually said something to this pattern (the hidden
    __INIT__ greeting trigger doesn't count)."""
    if not rec:
        return False
    return any(m.get("role") == "user" and m.get("content") != "__INIT__"
               for m in rec.get("history", []))


def _active() -> dict | None:
    return _patterns.get(_active_id) if _active_id else None


def _rec_for(pattern_id: str | None) -> dict | None:
    """Resolve the pattern a request targets. An EXPLICIT id must match exactly —
    we never silently retarget to a different pattern, because a stale id means the
    pattern was deleted and acting on whatever happens to be active could place the
    wrong family in the model. Only a None id (fresh client load) falls back to the
    active default."""
    if pattern_id:
        return _patterns.get(pattern_id)
    return _active()


def _save_history() -> None:
    """Atomically persist the whole store (temp file + replace)."""
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {"active_id": _active_id, "patterns": list(_patterns.values())}
        tmp = HISTORY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(HISTORY_PATH)
    except Exception as exc:
        print(f"[history] save failed: {exc}", file=sys.stderr)


def _load_history() -> None:
    global _patterns, _active_id
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    for rec in data.get("patterns", []):
        # Tolerate older/partial records by backfilling fields.
        rec.setdefault("history", [])
        rec.setdefault("last_element_id", None)
        rec.setdefault("pending_location", {})
        rec.setdefault("status", "seen")
        rec.setdefault("examples", [])
        rec.setdefault("detected_at", 0)
        if "id" in rec:
            _patterns[rec["id"]] = rec
    _active_id = data.get("active_id")
    if _active_id not in _patterns:
        _active_id = None


def _summary_item(rec: dict) -> dict:
    return {
        "id":               rec["id"],
        "label":            rec.get("label", "Routine"),
        "count":            rec.get("count", 0),
        "steps":            len(rec.get("tool_sequence", [])),
        "detected_at":      rec.get("detected_at", 0),
        "status":           rec.get("status", "seen"),
        "has_conversation": _has_user_turns(rec),
    }


def _visible_messages(rec: dict) -> list[dict]:
    """The conversation as the UI should render it: hide the __INIT__ trigger and
    strip control tokens from assistant turns."""
    out = []
    for m in rec.get("history", []):
        role, content = m.get("role"), m.get("content", "")
        if role == "user" and content == "__INIT__":
            continue
        if role == "assistant":
            content = _strip_tokens(content)
            if not content:
                continue
        out.append({"role": role, "content": content})
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# System prompt
# ═══════════════════════════════════════════════════════════════════════════════

_SYSTEM_TEMPLATE = """\
You are a BIM Workflow Assistant embedded in Autodesk Revit.
You have detected a repeated modeling routine that the user performs manually over and over.
Your job is to present this routine clearly, answer questions about it, let the user adjust \
parameters if needed, then execute or dismiss based on their decision.

━━━ DETECTED ROUTINE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{summary}

━━━ EXECUTION STEPS (what will run in Revit) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{steps}

━━━ RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Keep every response SHORT (2–4 sentences). This is a quick decision, not a lecture.
- BEFORE outputting ##EXECUTE##, you MUST know WHERE to place the element. There are TWO
  ways to get a location — offer BOTH when you don't have one yet ("Want to click the spot
  in Revit, or give me X, Y in metres?"):
  1) CLICK IN REVIT (best, and required-in-spirit for a door/window, which must sit on a
     wall). If the user wants to pick / select / click / "do it manually" / "let me choose"
     / "show me where", output a token on its own line:
       ##PICK##
     then ONE line like: "Click the spot in Revit — for a door or window, click ON a wall so
     it has something to host on." Do NOT also output ##EXECUTE##: picking runs the placement
     automatically as soon as they click.
  2) TYPED COORDINATES. When the user gives X, Y (metres), output on its own line:
       ##LOCATION:x,y,z##
     (numeric values, e.g. ##LOCATION:10.5,3.0,0## ; "at the origin" → ##LOCATION:0,0,0##),
     then confirm and output ##EXECUTE## on the next line.
- When the user confirms AND a location is known (yes / go / execute / do it / confirm / sure), output exactly:
    ##EXECUTE##
  as its own line, then a brief "Running now..." message.
- When the user declines (no / dismiss / cancel / not now / skip), output exactly:
    ##DISMISS##
  as its own line, then a brief acknowledgment.
- If the user wants to change a parameter (e.g. "change the mark to D-201"), acknowledge
  the change in your reply. The execution engine will apply it.
- Be friendly and concise. You are saving a BIM professional time.

━━━ POST-EXECUTION FOLLOW-UP ACTIONS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
After the shortcut has been executed you may be asked follow-up questions.
You can trigger these Revit actions by outputting the exact token on its own line:

  ##ISOLATE##   — isolate the last placed element in the current view
  ##ZOOM##      — zoom the view to fit the last placed element
  ##SELECT##    — select the last placed element

Only output a token when the user clearly asks for that action.
{post_exec_context}"""

_NO_PATTERN_SYSTEM = """\
You are a BIM Workflow Assistant embedded in Autodesk Revit. No repeated routine is
loaded yet. Briefly let the user know that you'll surface their repeated modeling
routines here as soon as the detector finds one, and invite them to keep working.
Keep it to one or two sentences."""


def _build_system(rec: dict | None) -> str:
    if not rec:
        return _NO_PATTERN_SYSTEM

    label = rec.get("label", "Unnamed Routine")
    count = rec.get("count", 0)
    motif = rec.get("motif", {})
    seq   = rec.get("tool_sequence", [])

    lines = [f"Name:       {label}", f"Repetitions: {count}×"]
    steps_m = motif.get("steps", [])
    if steps_m:
        lines.append(f"Step count:  {len(steps_m)}")
        lines.append("Steps:")
        for i, s in enumerate(steps_m, 1):
            action = s.get("action", "?")
            ft = s.get("family_type", "")
            pn = s.get("param_name", "")
            pv = s.get("param_value", "")
            if ft:
                lines.append(f"  {i}. {action}: {ft}")
            elif pn:
                lines.append(f"  {i}. {action}: {pn} = {pv}")
            else:
                lines.append(f"  {i}. {action}")
    elif seq:
        lines.append(f"Step count:  {len(seq)}")

    summary   = "\n".join(lines)
    steps_str = json.dumps(seq, indent=2) if seq else "[]"

    last_eid = rec.get("last_element_id")
    if last_eid:
        post_exec = f"\nThe shortcut was already executed. Last placed element ID: {last_eid}."
    else:
        post_exec = ""

    return _SYSTEM_TEMPLATE.format(summary=summary, steps=steps_str, post_exec_context=post_exec)


# ═══════════════════════════════════════════════════════════════════════════════
# SSE streaming helpers
# ═══════════════════════════════════════════════════════════════════════════════

async def _stream(rec: dict, user_text: str | None = None) -> AsyncIterator[str]:
    """Append the user's turn, stream Claude's reply, and persist the assistant
    turn — all under the pattern's lock so two requests to the SAME pattern can't
    interleave and produce non-alternating roles (which Anthropic 400s on)."""
    async with _lock_for(rec["id"]):
        if user_text is not None:
            rec["history"].append({"role": "user", "content": user_text})
        messages = [{"role": m["role"], "content": m["content"]} for m in rec["history"]]

        full = ""
        try:
            async with _client.messages.stream(
                model=MODEL,
                max_tokens=512,
                system=_build_system(rec),
                messages=messages,
            ) as stream:
                async for chunk in stream.text_stream:
                    full += chunk
                    yield f"data: {json.dumps({'t': chunk})}\n\n"
        except Exception as exc:
            # Roll back the dangling user turn so history stays alternating, then
            # surface the error in-band so the UI unlocks instead of hanging.
            if (user_text is not None and rec["history"]
                    and rec["history"][-1].get("role") == "user"):
                rec["history"].pop()
            _save_history()
            yield f"data: {json.dumps({'t': f'⚠ Error: {exc}'})}\n\n"
            yield f"data: {json.dumps({'done': True, 'action': None})}\n\n"
            return

        rec["history"].append({"role": "assistant", "content": full})

        # Parse optional location token: ##LOCATION:x,y,z##
        loc_match = re.search(r"##LOCATION:([-\d.]+),([-\d.]+),([-\d.]+)##", full)
        if loc_match:
            rec["pending_location"] = {
                "x": float(loc_match.group(1)),
                "y": float(loc_match.group(2)),
                "z": float(loc_match.group(3)),
            }

        # Signal completion + any action token
        action = None
        if   "##EXECUTE##"  in full: action = "execute"
        elif "##DISMISS##"  in full: action = "dismiss"; rec["status"] = "dismissed"
        elif "##ISOLATE##"  in full: action = "isolate"
        elif "##ZOOM##"     in full: action = "zoom"
        elif "##SELECT##"   in full: action = "select"
        elif "##PICK##"     in full: action = "pick"

        _save_history()
        yield f"data: {json.dumps({'done': True, 'action': action})}\n\n"


# ═══════════════════════════════════════════════════════════════════════════════
# API models
# ═══════════════════════════════════════════════════════════════════════════════

class PatternIn(BaseModel):
    id: str | None      = None
    label: str          = "Repeated Workflow"
    count: int          = 0
    motif: dict         = {}
    tool_sequence: list = []
    examples: list      = []

class MessageIn(BaseModel):
    text: str = ""
    pattern_id: str | None = None

class StartIn(BaseModel):
    pattern_id: str | None = None

class ActionIn(BaseModel):
    pattern_id: str | None = None

class ExecuteIn(BaseModel):
    x: float | None = None
    y: float | None = None
    z: float | None = None
    pattern_id: str | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# Pattern history routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/pattern")
async def api_set_pattern(payload: PatternIn):
    """Record a detected pattern. ADDS to history (or updates the matching entry);
    never destroys a previous one. Makes it the active pattern only when the user
    isn't in the middle of an unresolved conversation with another."""
    global _active_id
    data = payload.model_dump()
    pid  = _derive_id(data)
    now  = time.time()

    existing = _patterns.get(pid)
    if existing:
        prev_count = existing.get("count", 0)
        existing["count"]         = max(prev_count, payload.count)
        existing["label"]         = payload.label
        existing["motif"]         = payload.motif
        existing["tool_sequence"] = payload.tool_sequence
        existing["examples"]      = payload.examples
        existing["detected_at"]   = now
        # A routine the user DISMISSED that keeps recurring is worth re-alerting:
        # re-badge it as new so it re-surfaces instead of staying silenced forever.
        if existing.get("status") == "dismissed" and payload.count > prev_count:
            existing["status"] = "new"
        rec, is_new = existing, False
    else:
        rec = {
            "id": pid, "label": payload.label, "count": payload.count,
            "motif": payload.motif, "tool_sequence": payload.tool_sequence,
            "examples": payload.examples, "detected_at": now,
            "status": "new", "history": [],
            "last_element_id": None, "pending_location": {},
        }
        _patterns[pid] = rec
        is_new = True

    prev = _active()
    if pid == _active_id:
        pass  # updating the one already in focus
    elif (_active_id is None or prev is None
          or not _has_user_turns(prev)
          or prev.get("status") in ("executed", "dismissed")):
        # Safe to surface the new one — nobody is mid-conversation with the old.
        _active_id = pid
    # else: keep the user where they are; the new one waits in history (badged).

    _save_history()
    return {"ok": True, "id": pid, "active_id": _active_id, "is_new": is_new}


@app.get("/api/pattern")
async def api_get_pattern():
    """Back-compat: the currently active pattern (or {} if none)."""
    return _active() or {}


@app.get("/api/patterns")
async def api_list_patterns():
    """History list, newest first, for the sidebar."""
    items = sorted((_summary_item(r) for r in _patterns.values()),
                   key=lambda x: x["detected_at"], reverse=True)
    return {"patterns": items, "active_id": _active_id}


@app.post("/api/patterns/{pid}/activate")
async def api_activate_pattern(pid: str):
    """Switch focus to a historical pattern and return its conversation so the UI
    can re-render it."""
    global _active_id
    rec = _patterns.get(pid)
    if not rec:
        raise HTTPException(status_code=404, detail="pattern not found")
    _active_id = pid
    if rec.get("status") == "new":
        rec["status"] = "seen"
    _save_history()
    return {
        "ok": True, "id": pid,
        "label":   rec.get("label", "Routine"),
        "count":   rec.get("count", 0),
        "steps":   len(rec.get("tool_sequence", [])),
        "status":  rec.get("status", "seen"),
        "messages": _visible_messages(rec),
    }


@app.delete("/api/patterns/{pid}")
async def api_delete_pattern(pid: str):
    """Remove a pattern from the history."""
    global _active_id
    _patterns.pop(pid, None)
    if _active_id == pid:
        rest = sorted(_patterns.values(),
                      key=lambda r: r.get("detected_at", 0), reverse=True)
        _active_id = rest[0]["id"] if rest else None
    _save_history()
    return {"ok": True, "active_id": _active_id}


# ═══════════════════════════════════════════════════════════════════════════════
# Conversation routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/start")
async def api_start(body: StartIn = StartIn()):
    """Generate the opening greeting from Claude for a given pattern.
    The user message is hidden from the UI — only the assistant reply is shown."""
    rec = _rec_for(body.pattern_id)
    if not rec:
        raise HTTPException(status_code=409, detail="no pattern loaded")
    return StreamingResponse(
        _stream(rec, user_text="__INIT__"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat")
async def api_chat(msg: MessageIn):
    """Send a user message to a specific pattern and stream Claude's reply."""
    rec = _rec_for(msg.pattern_id)
    if not rec:
        raise HTTPException(status_code=409, detail="no pattern loaded")
    return StreamingResponse(
        _stream(rec, user_text=msg.text),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/execute")
async def api_execute(body: ExecuteIn = ExecuteIn()):
    """Run a pattern's shortcut in Revit, optionally overriding placement."""
    rec = _rec_for(body.pattern_id)
    if not rec:
        return {"error": "No pattern loaded", "success": False}
    seq = rec.get("tool_sequence", [])
    if not seq:
        return {"error": "No tool sequence loaded", "success": False}

    # Merge caller-provided coords with any location extracted from conversation
    merged = {**rec.get("pending_location", {})}
    if body.x is not None: merged["x"] = body.x
    if body.y is not None: merged["y"] = body.y
    if body.z is not None: merged["z"] = body.z
    if merged:
        import copy
        seq = copy.deepcopy(seq)
        for step in seq:
            if step.get("tool") == "place_element":
                loc = step.setdefault("arguments", {}).setdefault("location", {})
                loc.update(merged)

    # execute_shortcut does blocking TCP round-trips to the Revit plugin; run it off
    # the event loop so SSE streams and the sidebar poll don't freeze meanwhile.
    result = await asyncio.to_thread(execute_shortcut, "<chatbot>", tool_sequence=seq)

    # Track the last PLACED element id for follow-up actions (isolate/zoom/select).
    # The plugin returns AIResult {"Success":true,"Response":[123]} — there is NO
    # flat "elementId" key — so use the bridge's extractor, gated to place_element
    # steps (a Tag/SetParam result must not be mistaken for the subject element).
    for step in result.get("results", []):
        if step.get("tool") == "place_element":
            eid = _extract_element_id(step.get("result") or {})
            if eid:
                rec["last_element_id"] = eid
    if result.get("success") and result.get("errors", 0) == 0:
        rec["status"] = "executed"
    _save_history()
    return result


async def _operate_last_element(rec: dict | None, action: str) -> dict:
    """Run an operate_element action on a pattern's last placed element and
    normalize the AIResult envelope ({Success, Message}) into the {error}/success
    shape the UI expects.

    Backend contract: operate_element ← {"data": {"elementIds": [...], "action": ...}}.
    Valid actions: Select, Isolate, Hide, TempHide, Unhide, ResetIsolate, etc.
    """
    last_eid = rec.get("last_element_id") if rec else None
    if not last_eid:
        return {"error": f"No element to {action.lower()} — run the shortcut first",
                "success": False}
    # _call_plugin blocks on a TCP round-trip — keep it off the event loop.
    result = await asyncio.to_thread(_call_plugin, "operate_element", {
        "data": {"elementIds": [last_eid], "action": action},
    })
    if isinstance(result, dict) and result.get("Success") is False:
        return {"error": result.get("Message", f"{action} failed"),
                "success": False, "elementId": last_eid}
    body = result if isinstance(result, dict) else {"result": result}
    return {**body, "elementId": last_eid}


@app.post("/api/isolate")
async def api_isolate(body: ActionIn = ActionIn()):
    """Isolate the last placed element of a pattern in the current Revit view."""
    return await _operate_last_element(_rec_for(body.pattern_id), "Isolate")


@app.post("/api/zoom")
async def api_zoom(body: ActionIn = ActionIn()):
    """The backend has no 'zoom' action; 'Select' selects the element so the user
    can locate it (closest available behaviour)."""
    return await _operate_last_element(_rec_for(body.pattern_id), "Select")


@app.post("/api/select")
async def api_select(body: ActionIn = ActionIn()):
    """Select the last placed element of a pattern in Revit."""
    return await _operate_last_element(_rec_for(body.pattern_id), "Select")


@app.post("/api/pick")
async def api_pick(body: ActionIn = ActionIn()):
    """Let the user CLICK the placement location in Revit, then store it for /api/execute.

    Blocks on the human click (long socket timeout, run off the loop). For a hosted family
    the user clicks ON a wall and the backend snaps the door/window to that wall.
    """
    rec = _rec_for(body.pattern_id)
    if not rec:
        return {"error": "No pattern loaded", "success": False}
    result = await asyncio.to_thread(
        pick_point, "point",
        "Click the placement point — for a door/window, click ON a wall so it can host.")
    if not (isinstance(result, dict) and result.get("Success")):
        if isinstance(result, dict):
            msg = result.get("Message") or result.get("error") or "pick cancelled"
        else:
            msg = "pick failed"
        return {"error": msg, "success": False}
    resp = result.get("Response") or {}
    loc = {"x": resp.get("x"), "y": resp.get("y"), "z": resp.get("z")}
    rec["pending_location"] = loc
    _save_history()
    return {"success": True, **loc}


@app.post("/api/execute-smart")
async def api_execute_smart(body: ExecuteIn = ExecuteIn()):
    """Agentic, self-healing execution. An LLM tool-use loop reproduces the routine in the
    live model and RECOVERS from failures (no host wall → ask the user to pick one; family
    not loaded → list available + pick the closest). Streams its reasoning, tool calls, and
    results as SSE so the chat shows the self-correction transcript (like Claude Code)."""
    rec = _rec_for(body.pattern_id)

    async def _err(msg):
        yield f"data: {json.dumps({'kind': 'error', 'payload': msg})}\n\n"
        yield f"data: {json.dumps({'kind': 'final', 'payload': {'done': False, 'summary': msg}})}\n\n"

    if not rec:
        return StreamingResponse(_err("No pattern loaded"), media_type="text/event-stream")

    motif = rec.get("motif", {})
    merged = {**rec.get("pending_location", {})}
    if body.x is not None: merged["x"] = body.x
    if body.y is not None: merged["y"] = body.y
    if body.z is not None: merged["z"] = body.z
    location = merged or None

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_event(kind, payload):
        # bridge the synchronous executor thread → the async SSE queue
        loop.call_soon_threadsafe(queue.put_nowait, {"kind": kind, "payload": payload})

    async def gen():
        task = asyncio.create_task(
            asyncio.to_thread(run_executor, build_goal(motif, location), on_event=on_event))
        while not (task.done() and queue.empty()):
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=0.25)
                yield f"data: {json.dumps(ev)}\n\n"
            except asyncio.TimeoutError:
                continue
        result = await task
        for c in result.get("tool_calls", []):
            if (c["name"] == "place_element" and c["result"].get("success")
                    and c["result"].get("element_id")):
                rec["last_element_id"] = int(c["result"]["element_id"])
        if result.get("done"):
            rec["status"] = "executed"
        _save_history()
        yield ("data: " + json.dumps({"kind": "final", "payload": {
            "done": result.get("done"), "summary": result.get("summary"),
            "attempts": result.get("attempts")}}) + "\n\n")

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ═══════════════════════════════════════════════════════════════════════════════
# Chat UI  (served at /)
# ═══════════════════════════════════════════════════════════════════════════════

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BIM Assistant</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     background:#f0f2f5;height:100vh;display:flex;flex-direction:row;overflow:hidden}

/* ── History sidebar ── */
.sidebar{width:248px;flex-shrink:0;background:#1f2733;color:#cdd6e0;
         display:flex;flex-direction:column;height:100vh;
         transition:margin-left .2s ease}
.sidebar.hidden{margin-left:-248px}
.sb-hdr{padding:15px 16px;font-size:13px;font-weight:600;letter-spacing:.02em;
        color:#fff;border-bottom:1px solid rgba(255,255,255,.08);
        display:flex;align-items:center;gap:8px}
.sb-hdr .ic{font-size:15px}
.sb-list{flex:1;overflow-y:auto;padding:6px}
.hist-item{padding:10px 12px;border-radius:8px;cursor:pointer;margin-bottom:4px;
           border:1px solid transparent;transition:.12s;position:relative}
.hist-item:hover{background:rgba(255,255,255,.06)}
.hist-item.active{background:rgba(6,150,215,.22);border-color:rgba(6,150,215,.5)}
.hist-top{display:flex;align-items:center;gap:8px}
.hist-label{font-size:13px;font-weight:500;color:#fff;white-space:nowrap;
            overflow:hidden;text-overflow:ellipsis}
.hist-meta{font-size:11px;opacity:.6;margin-top:3px;margin-left:16px}
.sdot{width:8px;height:8px;border-radius:50%;flex-shrink:0;background:#8a94a6}
.sdot.new{background:#0696d7;animation:pulse 1.6s infinite}
.sdot.executed{background:#27ae60}
.sdot.dismissed{background:#6c757d}
.sdot.seen{background:#8a94a6}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(6,150,215,.55)}
                 70%{box-shadow:0 0 0 7px rgba(6,150,215,0)}
                 100%{box-shadow:0 0 0 0 rgba(6,150,215,0)}}
.hist-del{position:absolute;top:8px;right:8px;opacity:0;border:none;background:none;
          color:#aeb7c4;cursor:pointer;font-size:14px;line-height:1;padding:2px 4px;
          border-radius:4px;transition:.12s}
.hist-item:hover .hist-del{opacity:.7}
.hist-del:hover{opacity:1;background:rgba(255,255,255,.12);color:#fff}
.sb-empty{padding:18px 14px;font-size:12px;opacity:.55;line-height:1.55}

/* ── Main column ── */
.main{flex:1;min-width:0;display:flex;flex-direction:column;height:100vh}

/* ── Header ── */
.hdr{background:#0696d7;color:#fff;padding:14px 18px;
     display:flex;align-items:center;gap:12px;flex-shrink:0;
     box-shadow:0 2px 8px rgba(0,0,0,.18)}
.sb-toggle{background:rgba(255,255,255,.16);color:#fff;border:none;border-radius:6px;
           width:34px;height:34px;font-size:16px;cursor:pointer;flex-shrink:0;
           position:relative;display:flex;align-items:center;justify-content:center}
.sb-toggle:hover{background:rgba(255,255,255,.26)}
.sb-badge{position:absolute;top:-5px;right:-5px;background:#e74c3c;color:#fff;
          font-size:10px;font-weight:700;min-width:16px;height:16px;border-radius:8px;
          display:none;align-items:center;justify-content:center;padding:0 4px}
.hdr-icon{font-size:22px;line-height:1}
.hdr-text{min-width:0}
.hdr-text h1{font-size:15px;font-weight:600;letter-spacing:.01em;
             white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hdr-text p{font-size:11px;opacity:.8;margin-top:3px}
.hdr-actions{margin-left:auto;display:flex;gap:8px;flex-shrink:0}
.btn{padding:8px 18px;border:none;border-radius:6px;font-size:13px;
     font-weight:500;cursor:pointer;transition:.15s}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn-exec{background:#27ae60;color:#fff}
.btn-exec:not(:disabled):hover{background:#219150}
.btn-dismiss{background:rgba(255,255,255,.18);color:#fff;border:1px solid rgba(255,255,255,.3)}
.btn-dismiss:not(:disabled):hover{background:rgba(255,255,255,.28)}

/* ── Chat area ── */
.chat{flex:1;overflow-y:auto;padding:20px 16px;
      display:flex;flex-direction:column;gap:14px}

/* ── Message bubbles ── */
.msg{display:flex;gap:10px;max-width:78%;animation:fadein .2s ease}
@keyframes fadein{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.msg.bot{align-self:flex-start}
.msg.usr{align-self:flex-end;flex-direction:row-reverse}
.avatar{width:34px;height:34px;border-radius:50%;display:flex;align-items:center;
        justify-content:center;font-size:15px;flex-shrink:0;margin-top:2px}
.msg.bot .avatar{background:#0696d7;color:#fff}
.msg.usr .avatar{background:#dee2e6;color:#555}
.bubble{background:#fff;border-radius:14px;padding:10px 15px;
        font-size:14px;line-height:1.6;color:#222;
        box-shadow:0 1px 3px rgba(0,0,0,.09)}
.msg.usr .bubble{background:#0696d7;color:#fff}

/* ── Typing dots ── */
.dots{display:flex;gap:5px;padding:4px 2px}
.dot{width:8px;height:8px;border-radius:50%;background:#adb5bd;
     animation:bounce 1.1s ease-in-out infinite}
.dot:nth-child(2){animation-delay:.18s}
.dot:nth-child(3){animation-delay:.36s}
@keyframes bounce{0%,80%,100%{transform:scale(.65);opacity:.5}40%{transform:scale(1);opacity:1}}

/* ── Status bar ── */
.status{padding:11px 18px;font-size:13px;font-weight:500;text-align:center;
        border-top:1px solid #e5e5e5;flex-shrink:0;display:none}
.status.info   {background:#d1ecf1;color:#0c5460;display:block}
.status.success{background:#d4edda;color:#155724;display:block}
.status.error  {background:#f8d7da;color:#721c24;display:block}

/* ── Input area ── */
.input-row{display:flex;gap:8px;padding:12px 16px;background:#fff;
           border-top:1px solid #e5e5e5;flex-shrink:0}
#inp{flex:1;padding:10px 16px;border:1px solid #ced4da;border-radius:22px;
     font-size:14px;outline:none;transition:.15s}
#inp:focus{border-color:#0696d7;box-shadow:0 0 0 3px rgba(6,150,215,.12)}
#inp:disabled{background:#f8f9fa}
.btn-send{background:#0696d7;color:#fff;padding:10px 18px;
          border:none;border-radius:22px;font-size:13px;font-weight:500;cursor:pointer}
.btn-send:hover{background:#0584c0}
.btn-send:disabled{opacity:.45;cursor:not-allowed}
</style>
</head>
<body>

<aside class="sidebar" id="sidebar">
  <div class="sb-hdr"><span class="ic">🗂️</span> Detected Patterns</div>
  <div class="sb-list" id="sb-list"></div>
</aside>

<div class="main">
  <div class="hdr">
    <button class="sb-toggle" id="sb-toggle" onclick="toggleSidebar()">☰<span class="sb-badge" id="sb-badge"></span></button>
    <div class="hdr-icon">🔍</div>
    <div class="hdr-text">
      <h1 id="p-label">Pattern Detected</h1>
      <p id="p-meta">Loading…</p>
    </div>
    <div class="hdr-actions">
      <button class="btn btn-exec"    id="btn-exec" onclick="clickExec()">▶ Execute</button>
      <button class="btn btn-dismiss" id="btn-dis"  onclick="clickDismiss()">✕ Dismiss</button>
    </div>
  </div>

  <div class="chat" id="chat"></div>
  <div class="status" id="status"></div>

  <div class="input-row">
    <input id="inp" type="text"
           placeholder="Ask something, or say 'yes' to execute…"
           onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();send()}">
    <button class="btn-send" id="btn-send" onclick="send()">Send ↑</button>
  </div>
</div>

<script>
"use strict";
let _busy      = false;  // true while Claude is streaming a reply
let _done      = false;  // true after dismiss (actions closed)
let _executing = false;  // true while /api/execute is in-flight
let _switching = false;  // true while a pattern switch is in flight (before its stream)
let _curId     = null;   // pattern currently shown in the chat
let _activeId  = null;   // server's active pattern (from last poll)
let _cache     = [];     // last patterns list
let _knownIds  = new Set();

/* ── Helpers ────────────────────────────────────────────────────── */
const chat   = () => document.getElementById('chat');
const status = () => document.getElementById('status');
const inp    = () => document.getElementById('inp');

function esc(s){
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\n/g,'<br>');
}
function stripTokens(s){
  return s.replace(/##EXECUTE##|##DISMISS##|##ISOLATE##|##ZOOM##|##SELECT##|##PICK##|##LOCATION:[^#]*##/g,'').trim();
}
function withPid(body){ return Object.assign({pattern_id:_curId}, body||{}); }

function addBubble(role, text){
  const wrap = document.createElement('div');
  wrap.className = `msg ${role}`;
  const em = role === 'bot' ? '🤖' : '👤';
  wrap.innerHTML =
    `<div class="avatar">${em}</div>`+
    `<div class="bubble">${esc(text)}</div>`;
  chat().appendChild(wrap);
  chat().scrollTop = 99999;
  return wrap.querySelector('.bubble');
}
function showTyping(){
  const wrap = document.createElement('div');
  wrap.className = 'msg bot'; wrap.id = 'typing';
  wrap.innerHTML =
    `<div class="avatar">🤖</div>`+
    `<div class="bubble"><div class="dots">`+
    `<div class="dot"></div><div class="dot"></div><div class="dot"></div>`+
    `</div></div>`;
  chat().appendChild(wrap);
  chat().scrollTop = 99999;
}
function hideTyping(){ const t=document.getElementById('typing'); if(t) t.remove(); }

function setStatus(msg, type){ const s=status(); s.className=`status ${type}`; s.textContent=msg; }
function clearStatus(){ status().className='status'; }

function lockUI(){
  _busy = true;
  inp().disabled = true;
  document.getElementById('btn-send').disabled = true;
}
function unlockUI(){
  _busy = false;
  inp().disabled = false;
  document.getElementById('btn-send').disabled = false;
  inp().focus();
}
function freezeActions(){
  _done = true;
  document.getElementById('btn-exec').disabled = true;
  document.getElementById('btn-dis').disabled  = true;
}
function unfreezeActions(){
  _done = false; _busy = false; _executing = false;
  document.getElementById('btn-exec').disabled = false;
  document.getElementById('btn-exec').textContent = '▶ Execute';
  document.getElementById('btn-dis').disabled  = false;
  inp().disabled = false;
  document.getElementById('btn-send').disabled = false;
}

/* ── Sidebar / history ──────────────────────────────────────────── */
function timeAgo(sec){
  if(!sec) return 'just now';
  const d = Date.now()/1000 - sec;
  if(d < 60)    return 'just now';
  if(d < 3600)  return Math.floor(d/60)+'m ago';
  if(d < 86400) return Math.floor(d/3600)+'h ago';
  return Math.floor(d/86400)+'d ago';
}
function renderSidebar(items){
  const list = document.getElementById('sb-list');
  if(!items || !items.length){
    list.innerHTML = '<div class="sb-empty">No patterns detected yet. Keep modeling — repeated routines will appear here as they\'re found.</div>';
    return;
  }
  list.innerHTML = items.map(p=>{
    const active = p.id===_curId ? 'active' : '';
    const dot = ['executed','dismissed','new','seen'].includes(p.status) ? p.status : 'seen';
    return `<div class="hist-item ${active}" onclick="switchTo('${p.id}')">
      <button class="hist-del" title="Remove" onclick="event.stopPropagation();delPattern('${p.id}')">🗑</button>
      <div class="hist-top"><span class="sdot ${dot}"></span><span class="hist-label">${esc(p.label)}</span></div>
      <div class="hist-meta">${p.count}× · ${p.steps} step(s) · ${timeAgo(p.detected_at)}</div>
    </div>`;
  }).join('');
}
function updateBadge(items){
  const n = (items||[]).filter(p=>p.status==='new').length;
  const b = document.getElementById('sb-badge');
  if(n>0){ b.textContent=n; b.style.display='flex'; } else { b.style.display='none'; }
}
async function loadPatterns(){
  try{
    const r = await fetch('/api/patterns');
    const data = await r.json();
    _cache = data.patterns || [];
    _activeId = data.active_id;
    renderSidebar(_cache);
    updateBadge(_cache);
    return data;
  } catch(e){ return {patterns:_cache, active_id:_activeId}; }
}
function toggleSidebar(){ document.getElementById('sidebar').classList.toggle('hidden'); }

function setHeader(label, count, steps){
  document.getElementById('p-label').textContent = label || 'Pattern';
  document.getElementById('p-meta').textContent =
    `Detected ${count||'?'} time(s) · ${steps||0} step(s)`;
}

async function switchTo(id){
  // Guard with a synchronous flag set BEFORE any await, so a concurrent poll tick
  // or a second click can't start a second switch/stream that interleaves into the
  // same pane (the _busy guard alone has a gap during the pre-stream fetches).
  if(_busy || _executing || _switching) return;
  if(id === _curId && chat().children.length) return;
  _switching = true;
  try{
    let res;
    try{
      const r = await fetch(`/api/patterns/${id}/activate`, {method:'POST'});
      if(!r.ok) return;
      res = await r.json();
    } catch(e){ return; }

    _curId = id;
    unfreezeActions();
    chat().innerHTML = '';
    setHeader(res.label, res.count, res.steps);
    (res.messages||[]).forEach(m => addBubble(m.role==='assistant'?'bot':'usr', m.content));

    if(res.status === 'dismissed'){
      freezeActions();
      setStatus('Shortcut dismissed.', 'info');
      inp().placeholder = 'Ask a follow-up question…';
    } else if(res.status === 'executed'){
      clearStatus();
      document.getElementById('btn-exec').textContent = '▶ Execute Again';
      inp().placeholder = 'Ask a follow-up question…';
    } else {
      clearStatus();
      inp().placeholder = "Ask something, or say 'yes' to execute…";
    }

    await loadPatterns();                       // refresh highlight + new→seen dot
    if(!(res.messages||[]).length){             // fresh pattern → generate greeting
      await startGreeting(id);
    }
  } finally {
    _switching = false;
  }
}

async function delPattern(id){
  try{ await fetch(`/api/patterns/${id}`, {method:'DELETE'}); } catch(e){}
  if(id === _curId){
    chat().innerHTML = '';
    _curId = null;
    const data = await loadPatterns();
    if(data.active_id) await switchTo(data.active_id);
    else { setHeader('No pattern', 0, 0); addBubble('bot','That pattern was removed. Nothing else is in the history yet.'); }
  } else {
    await loadPatterns();
  }
}

/* ── SSE streaming ──────────────────────────────────────────────── */
async function consumeStream(resp){
  hideTyping();
  if(!resp.ok){
    // 409 (no/stale pattern) etc. — surface it and unlock instead of deadlocking.
    let detail = '';
    try{ detail = (await resp.json()).detail || ''; } catch(e){}
    addBubble('bot', detail ? `⚠ ${detail}` : `⚠ Request failed (${resp.status}).`);
    unlockUI();
    loadPatterns();
    return;
  }
  const bubble = addBubble('bot','');
  let full = '', buf = '';
  const reader  = resp.body.getReader();
  const decoder = new TextDecoder();
  const handleLine = (line)=>{
    if(!line.startsWith('data: ')) return;
    let ev; try{ ev = JSON.parse(line.slice(6)); } catch{ return; }
    if(ev.t !== undefined){
      full += ev.t;
      bubble.innerHTML = esc(stripTokens(full));
      chat().scrollTop = 99999;
    }
    if(ev.done) handleAction(ev.action);
  };
  while(true){
    const {done, value} = await reader.read();
    if(done) break;
    buf += decoder.decode(value, {stream:true});
    const parts = buf.split('\n');
    buf = parts.pop();                 // keep the trailing partial line for next read
    for(const line of parts) handleLine(line);
  }
  if(buf) handleLine(buf);             // flush any complete trailing frame
}
function handleAction(action){
  if     (action === 'execute')  runExec();
  else if(action === 'dismiss')  runDismiss();
  else if(action === 'pick')     runPick();
  else if(action === 'isolate')  runAction('/api/isolate', 'Isolating element');
  else if(action === 'zoom')     runAction('/api/zoom',    'Zooming to element');
  else if(action === 'select')   runAction('/api/select',  'Selecting element');
  else unlockUI();
}
async function runPick(){
  if(!_curId) return;
  _busy = false; _executing = false;
  inp().disabled = true; document.getElementById('btn-send').disabled = true;
  setStatus('🖱 Click the placement point in Revit (for a door/window, click on a wall)…', 'info');
  try{
    const r = await fetch('/api/pick', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(withPid())
    });
    const res = await r.json();
    if(res.success){
      const xm = (res.x/1000).toFixed(2), ym = (res.y/1000).toFixed(2);
      addBubble('bot', `Got it — placing at (${xm}, ${ym}) m.`);
      runExec();   // place using the just-picked location
    } else {
      setStatus(`✗ ${res.error || 'pick cancelled'}`, 'error');
      addBubble('bot', `The pick didn't complete (${res.error || 'cancelled'}). Try again, or give me X, Y in metres.`);
      unlockUI();
    }
  } catch(e){
    setStatus(`✗ ${e.message}`, 'error');
    addBubble('bot', `Couldn't run the pick: ${e.message}`);
    unlockUI();
  }
}
async function streamFrom(url, body){
  lockUI(); showTyping();
  const resp = await fetch(url, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(withPid(body))
  });
  await consumeStream(resp);
}
async function startGreeting(id){
  lockUI(); showTyping();
  const resp = await fetch('/api/start', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({pattern_id:id})
  });
  await consumeStream(resp);
}

/* ── Actions ────────────────────────────────────────────────────── */
async function runExec(){
  if(_executing) return;            // already running — ignore a duplicate fire
  if(!_curId){ setStatus('No pattern selected.', 'error'); return; }
  _busy = false; _executing = true;
  freezeActions();                  // disable Execute/Dismiss synchronously, pre-await
  inp().disabled = false;
  document.getElementById('btn-send').disabled = false;

  const btn = document.getElementById('btn-exec');
  btn.textContent = '⟳ Running…';
  setStatus('⟳ Executing in Revit — self-correcting on errors…', 'info');

  // Agentic, self-healing execution: stream the executor's reasoning + tool calls so the
  // user watches it diagnose and retry (Claude-Code style) instead of one blind shot.
  const handle = (line)=>{
    if(!line.startsWith('data: ')) return;
    let ev; try{ ev = JSON.parse(line.slice(6)); } catch{ return; }
    const k = ev.kind, p = ev.payload;
    if(k === 'reasoning'){ if(p && String(p).trim()) addBubble('bot', p); }
    else if(k === 'tool'){ setStatus(`🔧 ${p.name}…`, 'info'); }
    else if(k === 'result'){
      const ok = p.result && p.result.success;
      setStatus(`${ok?'✓':'⟳'} ${p.name}: ${String((p.result&&p.result.message)||'').slice(0,70)}`, ok?'success':'info');
    }
    else if(k === 'error'){ setStatus(`✗ ${p}`, 'error'); }
    else if(k === 'final'){
      if(p.done){ setStatus(`✓ Routine complete (${p.attempts} step(s))`, 'success'); btn.textContent = '▶ Execute Again'; inp().placeholder = 'Ask a follow-up question…'; }
      else { setStatus(`✗ Stopped: ${String(p.summary||'could not finish').slice(0,90)}`, 'error'); }
    }
  };
  try{
    const resp = await fetch('/api/execute-smart', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(withPid())
    });
    const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf = '';
    while(true){
      const {done, value} = await reader.read(); if(done) break;
      buf += dec.decode(value, {stream:true}); const parts = buf.split('\n'); buf = parts.pop();
      for(const line of parts) handle(line);
    }
    if(buf) handle(buf);
  } catch(e){
    setStatus(`✗ ${e.message}`, 'error');
    addBubble('bot', `Could not reach the server: ${e.message}`);
  }
  unfreezeActions();
  loadPatterns();   // reflect the new "executed" status in the sidebar
}

async function runAction(endpoint, label){
  setStatus(`⟳ ${label}…`, 'info');
  try{
    const r   = await fetch(endpoint, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(withPid())
    });
    const res = await r.json();
    if(res.error){ setStatus(`✗ ${res.error}`, 'error'); }
    else         { setStatus(`✓ ${label} done`, 'success'); }
  } catch(e){ setStatus(`✗ ${e.message}`, 'error'); }
}

function runDismiss(){
  freezeActions();
  setStatus('Shortcut dismissed.', 'info');
  loadPatterns();   // reflect "dismissed" status in the sidebar
}

function clickExec(){ if(_done || _busy || _executing || !_curId) return; runExec(); }
function clickDismiss(){
  if(_done || _busy || _executing || _switching || !_curId) return;
  addBubble('usr','Dismiss');
  streamFrom('/api/chat', {text:'Dismiss'});
}
function send(){
  if(_busy || _executing || _switching) return;
  if(!_curId) return;              // nothing selected yet (empty/just-loaded state)
  const text = inp().value.trim();
  if(!text) return;
  inp().value = '';
  clearStatus();
  addBubble('usr', text);
  streamFrom('/api/chat', {text});
}

/* ── Live polling: pick up newly detected patterns from the watcher ─ */
async function pollPatterns(){
  const data = await loadPatterns();
  _knownIds = new Set((data.patterns||[]).map(p=>p.id));
  if(_busy || _executing || _switching) return;   // never interrupt a live turn/switch
  if(data.active_id && data.active_id !== _curId){
    await switchTo(data.active_id);            // a fresh pattern surfaced — show it
  }
}

/* ── Init ───────────────────────────────────────────────────────── */
async function init(){
  const data = await loadPatterns();
  _knownIds = new Set((data.patterns||[]).map(p=>p.id));
  if(data.active_id){
    await switchTo(data.active_id);
  } else {
    setHeader('Waiting for a pattern', 0, 0);
    addBubble('bot', "No repeated routine detected yet. Keep working in Revit — the moment I notice you repeating a sequence, it'll appear here and in the list on the left.");
    // Disable the composer until a pattern exists. Do NOT set _busy — the 5s poll
    // must stay free to auto-surface the first detection (which re-enables these).
    inp().disabled = true;
    inp().placeholder = 'Waiting for a detected pattern…';
    document.getElementById('btn-send').disabled = true;
    document.getElementById('btn-exec').disabled = true;
    document.getElementById('btn-dis').disabled  = true;
  }
  if(window.innerWidth < 680) document.getElementById('sidebar').classList.add('hidden');
  setInterval(pollPatterns, 5000);
}

init();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return HTMLResponse(
        content=_HTML,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Standalone entry point  (python chatbot/chat_server.py)
# ═══════════════════════════════════════════════════════════════════════════════

_SAMPLE_PATTERN = {
    "label": "Place Door + Set 4 Params + Tag",
    "count": 5,
    "motif": {
        "steps": [
            {"action": "Place",    "family_type": "M_Single-Flush : 900x2100mm"},
            {"action": "SetParam", "param_name": "FireRating",   "param_value": "60"},
            {"action": "SetParam", "param_name": "Mark",         "param_value": "D-101"},
            {"action": "SetParam", "param_name": "Width",        "param_value": "900"},
            {"action": "SetParam", "param_name": "FrameMaterial","param_value": "Aluminium"},
            {"action": "Tag",      "family_type": "Door Tag"},
        ]
    },
    "tool_sequence": [
        {"tool": "place_element",        "arguments": {"family_type": "M_Single-Flush", "location": {"x": 0, "y": 0, "z": 0}}},
        {"tool": "set_parameter",        "arguments": {"element_id": "{{last_element_id}}", "parameter_name": "FireRating",    "value": "60"}},
        {"tool": "set_parameter",        "arguments": {"element_id": "{{last_element_id}}", "parameter_name": "Mark",          "value": "D-101"}},
        {"tool": "set_parameter",        "arguments": {"element_id": "{{last_element_id}}", "parameter_name": "Width",         "value": 900}},
        {"tool": "set_parameter",        "arguments": {"element_id": "{{last_element_id}}", "parameter_name": "FrameMaterial", "value": "Aluminium"}},
        {"tool": "create_annotation_tag","arguments": {"element_id": "{{last_element_id}}", "tag_family": "Door Tag"}},
    ],
}


def _seed_pattern(payload: dict, status: str = "seen") -> None:
    """Insert a pattern record directly (used for --pattern / sample on startup)."""
    global _active_id
    pid = _derive_id(payload)
    if pid not in _patterns:
        _patterns[pid] = {
            "id": pid,
            "label": payload.get("label", "Routine"),
            "count": int(payload.get("count", 0)),
            "motif": payload.get("motif", {}),
            "tool_sequence": payload.get("tool_sequence", []),
            "examples": payload.get("examples", []),
            "detected_at": time.time(),
            "status": status,
            "history": [],
            "last_element_id": None,
            "pending_location": {},
        }
    _active_id = pid


if __name__ == "__main__":
    import argparse

    # Under pythonw.exe (no console) sys.stdout/stderr are None. Both our own prints AND
    # uvicorn's log formatter (sys.stdout.isatty()) dereference them, which would crash the
    # server before it ever binds the port. Give them a real devnull sink so every
    # downstream stdout/stderr use is safe, then normalise encoding for the console case.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="BIM Pattern Chatbot Server")
    parser.add_argument("--port",    type=int, default=PORT,  help="Port (default 5000)")
    parser.add_argument("--pattern", type=str, default=None,  help="Path to a candidate JSON file")
    parser.add_argument("--no-browser", action="store_true",  help="Don't open browser automatically")
    parser.add_argument("--no-watcher", action="store_true",  help="Don't auto-start the pattern watcher")
    parser.add_argument("--fresh", action="store_true",       help="Ignore any saved history this run")
    parser.add_argument("--demo",  action="store_true",       help="Seed the built-in sample pattern (UI testing only)")
    args = parser.parse_args()

    # Restore the saved detection history (so previous patterns are still browsable).
    if not args.fresh:
        _load_history()

    # Pre-load / seed a pattern. The built-in sample is for UI testing ONLY — it is
    # NOT seeded in normal use, where it would masquerade as a real detection and,
    # once the user engaged it, suppress genuine detections from auto-surfacing.
    if args.pattern:
        p = Path(args.pattern)
        if p.exists():
            _seed_pattern(json.loads(p.read_text(encoding="utf-8")))
            print(f"Pattern loaded from {p}")
        else:
            print(f"Warning: {p} not found")
            if args.demo:
                _seed_pattern(_SAMPLE_PATTERN)
    elif args.demo and not _patterns:
        print("Seeding built-in sample pattern (--demo).")
        _seed_pattern(_SAMPLE_PATTERN)

    url = f"http://localhost:{args.port}"
    print(f"\nBIM Pattern Chatbot  →  {url}")
    print(f"History: {len(_patterns)} pattern(s) loaded.")
    if _active():
        print("Active:", _active().get("label", "?"), f"({_active().get('count',0)}×)")
    print("Press Ctrl+C to stop.\n")

    if not args.no_browser:
        asyncio.get_event_loop().call_later(1.2, lambda: webbrowser.open(url))

    # Auto-start the pattern watcher so detected routines appear in the assistant
    # automatically — recreates the retired revit_addin PatternBridge flow for the
    # generalBIMlog architecture. Disable with --no-watcher.
    if not args.no_watcher:
        import subprocess, atexit
        try:
            _watcher = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve().parent.parent / "pattern_watcher.py")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                # Force UTF-8 in the headless child so the "→" in routine labels can't
                # raise UnicodeEncodeError (cp1252) and silently kill every scan.
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            )
            print(f"Pattern watcher started (PID {_watcher.pid}).")
            atexit.register(lambda: _watcher.poll() is None and _watcher.terminate())
        except Exception as exc:
            print(f"Could not start pattern watcher: {exc}")

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
