"""
In-memory conversation session store.

Each session holds:
  - `contents`     : full Vertex AI contents history (for context continuity)
  - `chat_history` : display-friendly [{role, text, tools, ts}] list for the UI
  - `turn_count`   : number of completed user→assistant turns
  - `last_active`  : Unix timestamp for TTL eviction
"""

import time
import uuid
from typing import Any, Dict, List, Optional

# ── Configuration ─────────────────────────────────────────────────────────────
_MAX_CONTENTS_TURNS = 20   # keep last N Vertex AI content objects (user+model pairs)
_SESSION_TTL        = 7200  # seconds (2 hours)

# ── Storage ───────────────────────────────────────────────────────────────────
_sessions: Dict[str, Dict[str, Any]] = {}


def create_session() -> str:
    """Create a new session and return its ID."""
    sid = str(uuid.uuid4())
    _sessions[sid] = {
        "contents":     [],   # Vertex AI format history
        "chat_history": [],   # display format: [{role, text, tools, confidence, ts}]
        "turn_count":   0,
        "last_active":  time.time(),
    }
    return sid


def session_exists(session_id: str) -> bool:
    return session_id in _sessions


def get_contents(session_id: str) -> List[Dict]:
    """Return the Vertex AI contents history for a session."""
    s = _sessions.get(session_id)
    if not s:
        return []
    s["last_active"] = time.time()
    return list(s["contents"])


def get_chat_history(session_id: str) -> List[Dict]:
    """Return display-friendly chat history."""
    s = _sessions.get(session_id)
    return list(s["chat_history"]) if s else []


def save_turn(
    session_id: str,
    new_contents: List[Dict],
    user_text: str,
    assistant_text: str,
    tools_used: List[str],
    confidence: str,
) -> int:
    """
    Persist a completed turn.
    Returns the new turn count.
    """
    if session_id not in _sessions:
        _sessions[session_id] = {
            "contents":     [],
            "chat_history": [],
            "turn_count":   0,
            "last_active":  time.time(),
        }
    s = _sessions[session_id]

    # Trim Vertex AI contents to last N turns (each turn = 1 user + 1 model message minimum)
    s["contents"] = new_contents[-(  _MAX_CONTENTS_TURNS * 3):]

    # Append display entries
    ts = time.strftime("%H:%M")
    s["chat_history"].append({
        "role":       "user",
        "text":       user_text,
        "ts":         ts,
    })
    s["chat_history"].append({
        "role":       "assistant",
        "text":       assistant_text,
        "tools":      tools_used,
        "confidence": confidence,
        "ts":         ts,
    })

    s["turn_count"]  += 1
    s["last_active"]  = time.time()
    return s["turn_count"]


def delete_session(session_id: str) -> None:
    _sessions.pop(session_id, None)


def get_turn_count(session_id: str) -> int:
    s = _sessions.get(session_id)
    return s["turn_count"] if s else 0


def cleanup_expired() -> int:
    """Remove sessions older than TTL. Returns count removed."""
    now     = time.time()
    expired = [sid for sid, s in _sessions.items()
               if now - s["last_active"] > _SESSION_TTL]
    for sid in expired:
        del _sessions[sid]
    return len(expired)
