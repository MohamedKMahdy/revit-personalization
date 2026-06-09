"""
BIM Pattern Chatbot Server
==========================
Serves a streaming chat UI at http://localhost:5000

The flow:
  1. RevitLogger / orchestrator detects a repeated pattern
  2. It calls POST /api/pattern with the motif + tool_sequence
  3. The chat UI opens in the browser
  4. Claude greets the user, presents the pattern in plain language
  5. User can ask questions, change parameters, then confirm or dismiss
  6. On confirmation  → POST /api/execute → revit_bridge.execute_shortcut()
  7. Result shown in the chat

Run standalone (with sample data for testing):
  python chatbot/chat_server.py

Trigger programmatically:
  from chatbot.trigger import notify_pattern
  notify_pattern(label="...", count=5, motif={...}, tool_sequence=[...])
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import webbrowser
from pathlib import Path
from typing import AsyncIterator

import anthropic
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))
from mcp_server.revit_bridge import execute_shortcut

# ── Config ────────────────────────────────────────────────────────────────────
PORT  = int(os.environ.get("CHATBOT_PORT", "5000"))
MODEL = "claude-opus-4-8"

# ── App + Anthropic client ────────────────────────────────────────────────────
app     = FastAPI(title="BIM Pattern Assistant")
_client = anthropic.AsyncAnthropic()

# ── In-memory session state (one conversation at a time) ─────────────────────
_pattern: dict       = {}
_history: list[dict] = []   # Anthropic messages (alternating user/assistant)

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
- When the user confirms (yes / go / execute / do it / confirm / sure), output exactly:
    ##EXECUTE##
  as its own line, then a brief "Running now..." message.
- When the user declines (no / dismiss / cancel / not now / skip), output exactly:
    ##DISMISS##
  as its own line, then a brief acknowledgment.
- If the user wants to change a parameter (e.g. "change the mark to D-201"), acknowledge
  the change in your reply. The execution engine will apply it.
- Be friendly and concise. You are saving a BIM professional time.
"""


def _build_system() -> str:
    p = _pattern
    label = p.get("label", "Unnamed Routine")
    count = p.get("count", 0)
    motif = p.get("motif", {})
    seq   = p.get("tool_sequence", [])

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
    return _SYSTEM_TEMPLATE.format(summary=summary, steps=steps_str)


# ═══════════════════════════════════════════════════════════════════════════════
# SSE streaming helpers
# ═══════════════════════════════════════════════════════════════════════════════

async def _stream(messages: list[dict], append_to_history: bool = True) -> AsyncIterator[str]:
    """Call Claude with the current history + given messages, stream SSE chunks."""
    full = ""
    async with _client.messages.stream(
        model=MODEL,
        max_tokens=512,
        system=_build_system(),
        messages=messages,
    ) as stream:
        async for chunk in stream.text_stream:
            full += chunk
            yield f"data: {json.dumps({'t': chunk})}\n\n"

    if append_to_history:
        _history.append({"role": "assistant", "content": full})

    # Signal completion + any action token
    action = None
    if "##EXECUTE##" in full:
        action = "execute"
    elif "##DISMISS##" in full:
        action = "dismiss"

    yield f"data: {json.dumps({'done': True, 'action': action})}\n\n"


# ═══════════════════════════════════════════════════════════════════════════════
# API routes
# ═══════════════════════════════════════════════════════════════════════════════

class PatternIn(BaseModel):
    label: str        = "Repeated Workflow"
    count: int        = 0
    motif: dict       = {}
    tool_sequence: list = []
    examples: list    = []

class MessageIn(BaseModel):
    text: str = ""


@app.post("/api/pattern")
async def api_set_pattern(payload: PatternIn):
    """Load a new detected pattern (resets conversation)."""
    global _pattern, _history
    _pattern = payload.model_dump()
    _history  = []
    return {"ok": True}


@app.get("/api/pattern")
async def api_get_pattern():
    return _pattern


@app.post("/api/start")
async def api_start():
    """
    Generate the opening greeting from Claude.
    The user message is hidden from the UI — only the assistant reply is shown.
    """
    init_msg = {"role": "user", "content": "__INIT__"}
    _history.append(init_msg)

    return StreamingResponse(
        _stream(_history),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat")
async def api_chat(msg: MessageIn):
    """Send a user message and stream Claude's reply."""
    _history.append({"role": "user", "content": msg.text})
    return StreamingResponse(
        _stream(_history),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/execute")
async def api_execute():
    """Run the shortcut in Revit."""
    seq = _pattern.get("tool_sequence", [])
    if not seq:
        return {"error": "No tool sequence loaded", "success": False}
    result = execute_shortcut("<chatbot>", tool_sequence=seq)
    return result


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
     background:#f0f2f5;height:100vh;display:flex;flex-direction:column;overflow:hidden}

/* ── Header ── */
.hdr{background:#0696d7;color:#fff;padding:14px 20px;
     display:flex;align-items:center;gap:14px;flex-shrink:0;
     box-shadow:0 2px 8px rgba(0,0,0,.18)}
.hdr-icon{font-size:22px;line-height:1}
.hdr-text h1{font-size:15px;font-weight:600;letter-spacing:.01em}
.hdr-text p{font-size:11px;opacity:.8;margin-top:3px}
.hdr-actions{margin-left:auto;display:flex;gap:8px}
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

/* Spin animation for the ⟳ character */
@keyframes spin{from{display:inline-block;transform:rotate(0deg)}to{transform:rotate(360deg)}}

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

<div class="hdr">
  <div class="hdr-icon">🔍</div>
  <div class="hdr-text">
    <h1 id="p-label">Pattern Detected</h1>
    <p id="p-meta">Loading…</p>
  </div>
  <div class="hdr-actions">
    <button class="btn btn-exec"    id="btn-exec"    onclick="clickExec()">▶ Execute</button>
    <button class="btn btn-dismiss" id="btn-dis"     onclick="clickDismiss()">✕ Dismiss</button>
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

<script>
"use strict";
let _busy   = false;
let _done   = false;

/* ── Helpers ────────────────────────────────────────────────────── */
const chat   = () => document.getElementById('chat');
const status = () => document.getElementById('status');
const inp    = () => document.getElementById('inp');

function esc(s){
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\n/g,'<br>');
}

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

function setStatus(msg, type){
  const s = status();
  s.className = `status ${type}`;
  s.textContent = msg;
}
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
  inp().disabled = true;
  document.getElementById('btn-send').disabled = true;
}

/* ── SSE streaming ──────────────────────────────────────────────── */
async function streamFrom(url, body){
  lockUI();
  showTyping();

  const resp = await fetch(url,{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: body ? JSON.stringify(body) : '{}'
  });

  hideTyping();
  const bubble = addBubble('bot','');
  let full = '';

  const reader  = resp.body.getReader();
  const decoder = new TextDecoder();

  while(true){
    const {done, value} = await reader.read();
    if(done) break;
    const raw = decoder.decode(value, {stream:true});
    for(const line of raw.split('\n')){
      if(!line.startsWith('data: ')) continue;
      let ev;
      try{ ev = JSON.parse(line.slice(6)); } catch{ continue; }

      if(ev.t !== undefined){
        full += ev.t;
        // Strip control tokens from display
        const disp = full.replace(/##EXECUTE##|##DISMISS##/g,'').trim();
        bubble.innerHTML = esc(disp);
        chat().scrollTop = 99999;
      }
      if(ev.done){
        if(ev.action === 'execute') runExec();
        else if(ev.action === 'dismiss') runDismiss();
        else unlockUI();
      }
    }
  }
}

/* ── Actions ────────────────────────────────────────────────────── */
async function runExec(){
  freezeActions();

  // Immediately show spinner on the button itself
  const btn = document.getElementById('btn-exec');
  btn.textContent = '⟳ Running…';

  setStatus('⟳ Executing in Revit — please wait…', 'info');

  try{
    const r   = await fetch('/api/execute',{method:'POST'});
    const res = await r.json();
    if(res.success || (res.steps_executed != null && res.steps_executed > 0)){
      setStatus(`✓ Done — ${res.steps_executed} step(s) executed in Revit`, 'success');
      addBubble('bot', `Done! I applied ${res.steps_executed} step(s) to your Revit model.`);
    } else {
      const err = res.error || JSON.stringify(res);
      setStatus(`✗ ${err}`, 'error');
      addBubble('bot', `Something went wrong: ${err}\n\nMake sure Revit is open with a project loaded.`);
    }
  } catch(e){
    setStatus(`✗ Network error: ${e.message}`, 'error');
    addBubble('bot', `Could not reach Revit: ${e.message}`);
  }
}

function runDismiss(){
  freezeActions();
  setStatus('Shortcut dismissed.','info');
}

function clickExec(){
  if(_done || _busy) return;
  runExec();
}

function clickDismiss(){
  if(_done || _busy) return;
  addBubble('usr','Dismiss');
  streamFrom('/api/chat',{text:'Dismiss'});
}

function send(){
  if(_busy || _done) return;
  const text = inp().value.trim();
  if(!text) return;
  inp().value = '';
  clearStatus();
  addBubble('usr', text);
  streamFrom('/api/chat',{text});
}

/* ── Init ───────────────────────────────────────────────────────── */
async function init(){
  // Load pattern metadata for header
  try{
    const r = await fetch('/api/pattern');
    const p = await r.json();
    if(p && p.label){
      document.getElementById('p-label').textContent = p.label;
      const steps = (p.tool_sequence||[]).length;
      document.getElementById('p-meta').textContent =
        `Detected ${p.count||'?'} time(s) · ${steps} step(s)`;
    }
  } catch(e){ /* no pattern loaded yet — chatbot will explain */ }

  // Kick off the opening greeting from Claude
  lockUI();
  showTyping();
  const resp = await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  hideTyping();
  const bubble = addBubble('bot','');
  let full = '';

  const reader  = resp.body.getReader();
  const decoder = new TextDecoder();
  while(true){
    const {done,value} = await reader.read();
    if(done) break;
    for(const line of decoder.decode(value,{stream:true}).split('\n')){
      if(!line.startsWith('data: ')) continue;
      let ev; try{ ev=JSON.parse(line.slice(6)); }catch{ continue; }
      if(ev.t !== undefined){
        full += ev.t;
        bubble.innerHTML = esc(full.replace(/##EXECUTE##|##DISMISS##/g,'').trim());
        chat().scrollTop = 99999;
      }
      if(ev.done){
        if(ev.action === 'execute') runExec();
        else if(ev.action === 'dismiss') runDismiss();
        else unlockUI();
      }
    }
  }
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

if __name__ == "__main__":
    import argparse

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="BIM Pattern Chatbot Server")
    parser.add_argument("--port",    type=int, default=PORT,  help="Port (default 5000)")
    parser.add_argument("--pattern", type=str, default=None,  help="Path to a candidate JSON file")
    parser.add_argument("--no-browser", action="store_true",  help="Don't open browser automatically")
    args = parser.parse_args()

    # Pre-load pattern
    if args.pattern:
        p = Path(args.pattern)
        if p.exists():
            _pattern = json.loads(p.read_text(encoding="utf-8"))
            print(f"Pattern loaded from {p}")
        else:
            print(f"Warning: {p} not found, using sample pattern")
            _pattern = _SAMPLE_PATTERN
    else:
        print("No --pattern file given. Loading sample pattern for demo.")
        _pattern = _SAMPLE_PATTERN

    url = f"http://localhost:{args.port}"
    print(f"\nBIM Pattern Chatbot  →  {url}")
    print("Pattern:", _pattern.get("label", "?"), f"({_pattern.get('count',0)}×)")
    print("Press Ctrl+C to stop.\n")

    if not args.no_browser:
        # Open browser after a short delay so the server is ready
        asyncio.get_event_loop().call_later(1.2, lambda: webbrowser.open(url))

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
