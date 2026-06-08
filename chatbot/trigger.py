"""
Helper used by the orchestrator/RevitLogger to push a detected pattern
to the running chatbot server and (optionally) open the browser.

Usage:
    from chatbot.trigger import notify_pattern

    notify_pattern(
        label="Place Door + 4 Params + Tag",
        count=5,
        motif={...},
        tool_sequence=[...],
        open_browser=True,
    )
"""
from __future__ import annotations

import subprocess
import sys
import time
import webbrowser
from pathlib import Path

try:
    import httpx
    _has_httpx = True
except ImportError:
    _has_httpx = False

CHATBOT_URL = "http://localhost:5000"


def _is_server_running() -> bool:
    if not _has_httpx:
        return False
    try:
        r = httpx.get(f"{CHATBOT_URL}/api/pattern", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _start_server() -> None:
    """Start the chatbot server as a background subprocess."""
    server_py = str(Path(__file__).parent / "chat_server.py")
    subprocess.Popen(
        [sys.executable, server_py, "--no-browser"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait until the server is up (max 8 s)
    for _ in range(16):
        time.sleep(0.5)
        if _is_server_running():
            return
    raise RuntimeError("Chatbot server did not start within 8 seconds")


def notify_pattern(
    label: str,
    count: int,
    motif: dict,
    tool_sequence: list,
    examples: list | None = None,
    open_browser: bool = True,
) -> None:
    """
    Push a detected pattern to the chatbot server.
    Starts the server first if it isn't already running.
    """
    if not _has_httpx:
        raise ImportError("httpx is required: pip install httpx")

    if not _is_server_running():
        _start_server()

    payload = {
        "label":         label,
        "count":         count,
        "motif":         motif,
        "tool_sequence": tool_sequence,
        "examples":      examples or [],
    }
    httpx.post(f"{CHATBOT_URL}/api/pattern", json=payload, timeout=5)

    if open_browser:
        webbrowser.open(CHATBOT_URL)
