"""
Standalone AI Chat Server + Copilot CLI Agent Bridge.

The AI agent can list, read and write files in the project root directory,
enabling prompts like "make the background white" to actually edit the files.

Providers:
  Gemini 2.5 Flash/Pro  — via Vertex AI REST (service account auth)
  GPT-4o / GPT-4o mini  — via GitHub Copilot API (OAuth token)

Usage:
  pip install -r requirements.txt
  python server.py   →   http://localhost:7777
"""

from __future__ import annotations

import asyncio
import datetime
import getpass
import json
import math
import os
import socket
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, AsyncGenerator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# ── Load .env from airline_schedule_app ──────────────────────────────────────
_ENV_PATH = Path(__file__).parent.parent.parent / ".env"

try:
    from dotenv import load_dotenv
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH, override=False)
        print(f"[init] Loaded .env from {_ENV_PATH}")
    else:
        print(f"[init] Warning: .env not found at {_ENV_PATH}")
except ImportError:
    if _ENV_PATH.exists():
        for _line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

# ── google-auth (Vertex AI) ───────────────────────────────────────────────────
try:
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    GOOGLE_AUTH_OK = True
except ImportError:
    GOOGLE_AUTH_OK = False
    print("[init] Warning: google-auth not installed. Run: pip install google-auth")

try:
    import requests as _req
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False

# ── Config ────────────────────────────────────────────────────────────────────
def _strip(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] in ('"', "'") and v[-1] == v[0]:
        v = v[1:-1].strip()
    return v

_PROJECT_ID    = _strip(os.environ.get("VERTEX_PROJECT_ID", ""))
_LOCATION      = _strip(os.environ.get("VERTEX_LOCATION",   "us-central1"))
_DEFAULT_MODEL = _strip(os.environ.get("VERTEX_MODEL",      "gemini-2.5-flash"))
_SA_JSON_ENV   = _strip(os.environ.get("VERTEX_SERVICE_ACCOUNT_JSON", ""))
_GH_TOKEN      = _strip(os.environ.get("GITHUB_COPILOT_TOKEN", ""))

_COPILOT_BASE  = "https://api.githubcopilot.com"
_MAX_FILE_SIZE = 150_000   # bytes — cap reads to avoid token bloat
_MAX_TOOL_ROUNDS = 12

# ── Root directory the agent can read/write ───────────────────────────────────
_ROOT = Path(__file__).parent.parent.parent  # airline_schedule_app root

# ── Ignored paths in list_files ───────────────────────────────────────────────
_IGNORE_DIRS  = {".git", "__pycache__", "node_modules", ".pytest_cache", ".venv", "venv"}
_IGNORE_EXTS  = {".pyc", ".pyo", ".db", ".duckdb", ".log", ".lock"}

# ── Per-run file backup registry (enables /api/revert) ───────────────────────
_file_backups: Dict[str, Optional[bytes]] = {}  # rel_path → pre-change bytes; None = newly created

# ── Features registry ─────────────────────────────────────────────────────────
_FEATURES_FILE = _ROOT / "agents_features.json"

_DEFAULT_FEATURES: List[Dict] = [
    {"name": "Multi-model AI chat",       "description": "Chat with Gemini 2.5 Flash/Pro (Vertex AI) or GPT-4o/mini (Copilot API) with full conversation history."},
    {"name": "Copilot CLI agent",         "description": "Autonomous file-editing agent powered by Copilot CLI; reads, writes and creates files in the project root."},
    {"name": "Real-time streaming",       "description": "Agent output streamed token-by-token via SSE so you see responses as they arrive."},
    {"name": "File change review panel",  "description": "After each agent run, lists every changed file with Keep / Discard / Modify actions per file and bulk actions."},
    {"name": "File revert",               "description": "Any file changed this session can be reverted to its pre-session state via /api/revert."},
    {"name": "Markdown rendering",        "description": "Agent replies rendered as rich markdown: headings, code blocks, tables, blockquotes, inline code."},
    {"name": "Airplane animation",        "description": "Canvas-based airplanes orbit the active agent bubble while it is running, with braking, overtaking and cloud puffs."},
    {"name": "Progress indicator",        "description": "Elapsed timer and animated indeterminate progress bar shown while the agent is working."},
    {"name": "Suggestion chips",          "description": "Quick-start prompt chips shown on the empty-state screen for one-click example prompts."},
    {"name": "Message queue",             "description": "Prompts sent while the agent is busy are queued and processed automatically in order once the agent finishes."},
    {"name": "Features dashboard",        "description": "Slide-in panel listing every app feature; supports keep, discard and inline edit — persisted in features.json."},
]


def _load_features() -> List[Dict]:
    if _FEATURES_FILE.exists():
        try:
            raw = json.loads(_FEATURES_FILE.read_text("utf-8"))
            # Support both {"features":[…]} envelope and bare list
            return raw["features"] if isinstance(raw, dict) else raw
        except Exception:
            pass
    return [{"id": str(uuid.uuid4()), **f} for f in _DEFAULT_FEATURES]


def _save_features(features: List[Dict]) -> None:
    _FEATURES_FILE.write_text(
        json.dumps({"features": features}, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _whoami_str() -> str:
    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:
        return "unknown"


_features: List[Dict] = _load_features()
if not _FEATURES_FILE.exists():
    _save_features(_features)


def _snapshot_project() -> Dict[str, Any]:
    """Return {rel: {mtime, content}} for every project file (content capped at _MAX_FILE_SIZE)."""
    snap: Dict[str, Any] = {}
    for f in sorted(_ROOT.rglob("*")):
        if not f.is_file():
            continue
        if any(part in _IGNORE_DIRS for part in f.parts):
            continue
        if f.suffix in _IGNORE_EXTS:
            continue
        rel = str(f.relative_to(_ROOT)).replace("\\", "/")
        stat = f.stat()
        content: Optional[bytes] = None
        if stat.st_size <= _MAX_FILE_SIZE:
            try:
                content = f.read_bytes()
            except Exception:
                pass
        snap[rel] = {"mtime": stat.st_mtime, "content": content}
    return snap


def _find_changed_files(before: Dict[str, Any]) -> List[Dict]:
    """Compare current project tree against *before* snapshot; populate _file_backups for revert."""
    changes: List[Dict] = []
    for f in sorted(_ROOT.rglob("*")):
        if not f.is_file():
            continue
        if any(part in _IGNORE_DIRS for part in f.parts):
            continue
        if f.suffix in _IGNORE_EXTS:
            continue
        rel = str(f.relative_to(_ROOT)).replace("\\", "/")
        try:
            current_mtime = f.stat().st_mtime
        except Exception:
            continue
        if rel not in before:
            action = "created"
            if rel not in _file_backups:
                _file_backups[rel] = None          # new file → revert = delete
        elif current_mtime != before[rel]["mtime"]:
            action = "modified"
            if rel not in _file_backups:
                _file_backups[rel] = before[rel]["content"]  # may be None if was too large
        else:
            continue
        changes.append({"path": rel, "action": action})
    return changes


# ── Available models ──────────────────────────────────────────────────────────
AVAILABLE_MODELS = [
    {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash  -- fast",         "provider": "gemini"},
    {"id": "gemini-2.5-pro",   "label": "Gemini 2.5 Pro  -- best reasoning", "provider": "gemini"},
    {"id": "gpt-4o",           "label": "GPT-4o  -- OpenAI via Copilot",     "provider": "copilot"},
    {"id": "gpt-4o-mini",      "label": "GPT-4o mini  -- fast & cheap",      "provider": "copilot"},
]

# ── Default system prompt (pre-filled in the UI) ─────────────────────────────
DEFAULT_SYSTEM_PROMPT = f"""You are a coding assistant with direct access to the Airline Schedule Intelligence project files.

Project root: {_ROOT}

You have four tools:
- list_files              — see every file in the project
- read_file(path)         — read a file's full content
- write_file(path, content) — create or overwrite a file
- log_feature(action, name, description, old_name?, feature_id?) — record a feature change

Rules:
1. Before editing any file, always call read_file first to get its current content.
2. When writing, provide the COMPLETE updated file content (not diffs or snippets).
3. After making changes, briefly summarise what you changed and why.
4. Only modify files inside the project root — never use absolute paths or '..' traversal.
5. ALWAYS call log_feature after any dashboard change:
   - action "add"               → new feature/component introduced
   - action "remove"            → feature disabled or deleted
   - action "rename"            → feature renamed (set old_name to previous name)
   - action "update_description"→ description or behaviour updated
   Keep feature names short (3–7 words). This powers the Features Registry panel.
""".strip()

# ── Tool definitions — Gemini format ─────────────────────────────────────────
GEMINI_TOOLS = [{
    "functionDeclarations": [
        {
            "name": "list_files",
            "description": "List all source files in the project root directory.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "read_file",
            "description": "Read the complete contents of a project file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path from project root, e.g. 'chat.html'",
                    }
                },
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": "Write (create or fully overwrite) a file. Provide the complete new content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path from project root, e.g. 'chat.html'",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete file content to write.",
                    },
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "log_feature",
            "description": "Record a feature addition, removal, rename, or description update in the Features Registry. Call this after every dashboard change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "rename", "update_description"],
                        "description": "add=new feature, remove=deleted/disabled, rename=name changed, update_description=description updated",
                    },
                    "name": {
                        "type": "string",
                        "description": "Current (new) feature name, 3–7 words.",
                    },
                    "description": {
                        "type": "string",
                        "description": "What the feature does.",
                    },
                    "old_name": {
                        "type": "string",
                        "description": "Previous name (required for rename action).",
                    },
                    "feature_id": {
                        "type": "string",
                        "description": "Existing feature UUID (required for remove/rename/update_description).",
                    },
                },
                "required": ["action", "name"],
            },
        },
    ]
}]

# ── Tool definitions — OpenAI format ─────────────────────────────────────────
OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all source files in the project root directory.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the complete contents of a project file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, e.g. 'chat.html'"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write (create or fully overwrite) a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "Relative path, e.g. 'chat.html'"},
                    "content": {"type": "string", "description": "Complete file content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_feature",
            "description": "Record a feature change in the Features Registry. Call after every dashboard change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action":      {"type": "string", "enum": ["add", "remove", "rename", "update_description"]},
                    "name":        {"type": "string", "description": "Current (new) feature name"},
                    "description": {"type": "string", "description": "What the feature does"},
                    "old_name":    {"type": "string", "description": "Previous name (rename only)"},
                    "feature_id":  {"type": "string", "description": "Existing feature UUID"},
                },
                "required": ["action", "name"],
            },
        },
    },
]


# ── Tool execution ────────────────────────────────────────────────────────────

def _safe_path(rel: str) -> Optional[Path]:
    """Resolve a relative path and verify it stays inside _ROOT."""
    try:
        p = (_ROOT / rel).resolve()
        if _ROOT in p.parents or p == _ROOT:
            return p
    except Exception:
        pass
    return None


def execute_tool(name: str, args: Dict) -> Any:
    if name == "list_files":
        files = []
        for f in sorted(_ROOT.rglob("*")):
            if not f.is_file():
                continue
            if any(part in _IGNORE_DIRS for part in f.parts):
                continue
            if f.suffix in _IGNORE_EXTS:
                continue
            files.append(str(f.relative_to(_ROOT)).replace("\\", "/"))
        return {"files": files, "count": len(files), "root": str(_ROOT)}

    if name == "read_file":
        rel  = args.get("path", "")
        path = _safe_path(rel)
        if path is None:
            return {"error": f"Access denied or invalid path: {rel}"}
        if not path.exists():
            return {"error": f"File not found: {rel}"}
        size = path.stat().st_size
        if size > _MAX_FILE_SIZE:
            return {"error": f"File too large ({size:,} bytes > {_MAX_FILE_SIZE:,}). Read in sections."}
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            return {"path": rel, "content": content, "size": size}
        except Exception as e:
            return {"error": str(e)}

    if name == "write_file":
        rel     = args.get("path", "")
        content = args.get("content", "")
        path    = _safe_path(rel)
        if path is None:
            return {"error": f"Access denied or invalid path: {rel}"}
        try:
            # Backup original before first write so it can be reverted via /api/revert
            if rel not in _file_backups:
                if path.exists() and path.stat().st_size <= _MAX_FILE_SIZE:
                    _file_backups[rel] = path.read_bytes()
                else:
                    _file_backups[rel] = None   # None = newly created file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {"success": True, "path": rel, "bytes_written": len(content.encode("utf-8"))}
        except Exception as e:
            return {"error": str(e)}

    if name == "log_feature":
        action     = args.get("action", "add")
        feat_name  = args.get("name", "").strip()
        desc       = args.get("description", "").strip()
        old_name   = args.get("old_name", "").strip()
        feat_id    = args.get("feature_id", "").strip()
        now        = datetime.datetime.utcnow().isoformat() + "Z"
        by         = _whoami_str()

        try:
            raw = json.loads(_FEATURES_FILE.read_text("utf-8")) if _FEATURES_FILE.exists() else {"features": []}
            features = raw.get("features", []) if isinstance(raw, dict) else raw
        except Exception:
            features = []

        if action == "add":
            features.append({
                "id": str(uuid.uuid4()), "name": feat_name, "description": desc,
                "status": "active", "source": "agent",
                "addedAt": now, "addedBy": by, "localOnly": False,
            })
        else:
            for f in features:
                match = (feat_id and f.get("id") == feat_id) or \
                        (old_name and f.get("name") == old_name) or \
                        (f.get("name") == feat_name)
                if not match:
                    continue
                if action == "remove":
                    f["status"] = "removed"; f["updatedAt"] = now; f["updatedBy"] = by
                elif action == "rename":
                    f["name"] = feat_name; f["updatedAt"] = now; f["updatedBy"] = by
                    if desc:
                        f["description"] = desc
                elif action == "update_description":
                    f["description"] = desc; f["updatedAt"] = now; f["updatedBy"] = by
                break

        _FEATURES_FILE.write_text(
            json.dumps({"features": features}, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return {"success": True, "action": action, "feature": feat_name}

    return {"error": f"Unknown tool: {name}"}


# ── Auth sessions ─────────────────────────────────────────────────────────────
_vertex_session: Optional[Any]  = None
_copilot_session: Optional[Any] = None


def _load_service_account() -> Optional[Dict]:
    raw = _SA_JSON_ENV
    if not raw:
        return None
    if raw.startswith("{"):
        return json.loads(raw)
    p = Path(raw)
    if p.exists():
        return json.loads(p.read_text("utf-8"))
    print(f"[init] Warning: service account JSON not found at: {raw}")
    return None


def _init_sessions() -> None:
    global _vertex_session, _copilot_session

    if GOOGLE_AUTH_OK and REQUESTS_OK and _PROJECT_ID:
        sa = _load_service_account()
        if sa:
            try:
                creds = service_account.Credentials.from_service_account_info(
                    sa, scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
                _vertex_session = AuthorizedSession(creds)
                print(f"[init] Vertex AI ready — project={_PROJECT_ID} model={_DEFAULT_MODEL}")
            except Exception as e:
                print(f"[init] Vertex AI auth failed: {e}")
        else:
            print("[init] Vertex AI: no service account JSON found.")

    if REQUESTS_OK and _GH_TOKEN:
        _copilot_session = _req.Session()
        _copilot_session.headers.update({
            "Authorization":          f"Bearer {_GH_TOKEN}",
            "Content-Type":           "application/json",
            "Accept":                 "application/json",
            "editor-version":         "vscode/1.96.0",
            "editor-plugin-version":  "copilot-chat/0.22.4",
            "copilot-integration-id": "vscode-chat",
        })
        print("[init] GitHub Copilot API ready (GPT-4o available)")
    else:
        print("[init] GitHub Copilot: no token found.")


_init_sessions()


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="AI Chat", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_STATIC = Path(__file__).parent


# ── Schemas ───────────────────────────────────────────────────────────────────
class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[Message]
    model: str = _DEFAULT_MODEL
    system: Optional[str] = None


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(str(_STATIC / "chat.html"))


@app.get("/api/whoami")
async def whoami():
    try:
        return {"username": getpass.getuser(), "hostname": socket.gethostname(),
                "display": _whoami_str()}
    except Exception as e:
        return {"username": "unknown", "hostname": "unknown", "display": "unknown", "error": str(e)}


@app.get("/api/models")
async def list_models():
    result = []
    for m in AVAILABLE_MODELS:
        ok = (
            (m["provider"] == "gemini"  and _vertex_session  is not None) or
            (m["provider"] == "copilot" and _copilot_session is not None)
        )
        result.append({**m, "available": ok})
    return {
        "models":         result,
        "default":        _DEFAULT_MODEL,
        "system_prompt":  DEFAULT_SYSTEM_PROMPT,
        "root":           str(_ROOT),
    }


@app.post("/api/chat")
async def chat(req: ChatRequest):
    model = req.model
    msgs  = [m.model_dump() for m in req.messages]

    if model.startswith("gpt-") and _copilot_session:
        gen = _agent_stream_copilot(msgs, model, req.system)
    elif _vertex_session:
        gen = _agent_stream_gemini(msgs, model, req.system)
    else:
        async def _no_provider():
            yield f"data: {json.dumps({'type':'error','text':'No AI provider configured.'})}\n\n"
            yield "data: [DONE]\n\n"
        gen = _no_provider()

    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Utility ───────────────────────────────────────────────────────────────────
def _clean(obj: Any) -> Any:
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def _sse(event: Dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _result_summary(result: Any) -> str:
    """Short human-readable summary of a tool result for the UI."""
    if isinstance(result, dict):
        if "error" in result:
            return f"Error: {result['error']}"
        if "files" in result:
            return f"{result['count']} files listed"
        if "content" in result:
            lines = result["content"].count("\n") + 1
            return f"{result['size']:,} bytes, {lines} lines"
        if result.get("success"):
            return f"Written {result.get('bytes_written', '?')} bytes to {result.get('path')}"
    return str(result)[:120]


def _auto_log_feature(prompt: str, files: List[str]) -> None:
    """Auto-log a feature entry to features.json whenever the agent changes files."""
    prompt = prompt.strip()
    name   = (prompt[:72] + "…") if len(prompt) > 72 else prompt
    desc   = "Files changed: " + ", ".join(files[:6]) + (" (+more)" if len(files) > 6 else "")
    try:
        raw      = json.loads(_FEATURES_FILE.read_text("utf-8")) if _FEATURES_FILE.exists() else {"features": []}
        features = raw.get("features", []) if isinstance(raw, dict) else raw
        features.append({
            "id":          str(uuid.uuid4()),
            "name":        name,
            "description": desc,
            "status":      "active",
            "source":      "agent",
            "addedAt":     datetime.datetime.utcnow().isoformat() + "Z",
            "addedBy":     _whoami_str(),
            "files":       files,
            "localOnly":   False,
        })
        _FEATURES_FILE.write_text(
            json.dumps({"features": features}, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        print(f"[features] auto-log failed: {e}")


# ── Gemini agentic loop ───────────────────────────────────────────────────────
def _gemini_endpoint(model: str) -> str:
    return (
        f"https://{_LOCATION}-aiplatform.googleapis.com/v1"
        f"/projects/{_PROJECT_ID}/locations/{_LOCATION}"
        f"/publishers/google/models/{model}:generateContent"
    )


async def _agent_stream_gemini(
    messages: List[Dict], model: str, system: Optional[str]
) -> AsyncGenerator[str, None]:

    user_prompt = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    _written_files: List[str] = []   # track files changed this turn

    # Build Gemini-format contents history
    contents: List[Dict] = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})

    sys_text = system or DEFAULT_SYSTEM_PROMPT

    for _round in range(_MAX_TOOL_ROUNDS):
        body = _clean({
            "contents": contents,
            "tools":    GEMINI_TOOLS,
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 65536},
            "systemInstruction": {"parts": [{"text": sys_text}]},
        })

        def _call():
            return _vertex_session.post(
                _gemini_endpoint(model),
                data=json.dumps(body),
                headers={"Content-Type": "application/json"},
                timeout=120,
            )

        resp = await asyncio.to_thread(_call)
        if not resp.ok:
            yield _sse({"type": "error", "text": f"Gemini error ({resp.status_code}): {resp.text[:400]}"})
            yield "data: [DONE]\n\n"
            return

        data      = resp.json()
        candidate = data.get("candidates", [{}])[0]
        parts     = candidate.get("content", {}).get("parts", [])

        fn_calls   = [p["functionCall"] for p in parts if "functionCall" in p]
        text_parts = [p.get("text", "") for p in parts if "text" in p]

        if not fn_calls:
            full_text = "".join(text_parts).strip()
            if full_text:
                yield _sse({"type": "text", "text": full_text})
            # Auto-log feature if files were written (and LM didn't call log_feature itself)
            if _written_files:
                await asyncio.to_thread(_auto_log_feature, user_prompt, _written_files)
                yield _sse({"type": "feature_logged", "files": _written_files})
            yield "data: [DONE]\n\n"
            return

        # Append model turn (may include both text and fn calls)
        contents.append({"role": "model", "parts": parts})

        # Execute each tool call
        result_parts = []
        for fc in fn_calls:
            name = fc["name"]
            args = fc.get("args", {})
            yield _sse({"type": "tool_start", "name": name, "args": args})

            result = await asyncio.to_thread(execute_tool, name, args)

            # Track writes; skip auto-log if LM used log_feature itself
            if name == "write_file" and result.get("success"):
                _written_files.append(args.get("path", "?"))
            if name == "log_feature" and result.get("success"):
                _written_files.clear()  # LM handled it — don't double-log

            yield _sse({"type": "tool_end", "name": name,
                        "summary": _result_summary(result),
                        "result":  json.dumps(result)[:2000]})

            result_parts.append({
                "functionResponse": {
                    "name":     name,
                    "response": result,
                }
            })

        contents.append({"role": "user", "parts": result_parts})

    yield _sse({"type": "error", "text": "Agent reached max tool rounds without a final answer."})
    yield "data: [DONE]\n\n"


# ── Copilot (GPT-4o) agentic loop ────────────────────────────────────────────
async def _agent_stream_copilot(
    messages: List[Dict], model: str, system: Optional[str]
) -> AsyncGenerator[str, None]:

    user_prompt = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    _written_files: List[str] = []

    sys_text = system or DEFAULT_SYSTEM_PROMPT
    oai_messages: List[Dict] = [{"role": "system", "content": sys_text}]
    for m in messages:
        oai_messages.append({"role": m["role"], "content": m["content"]})

    for _round in range(_MAX_TOOL_ROUNDS):
        body = _clean({
            "model":       model,
            "messages":    oai_messages,
            "tools":       OPENAI_TOOLS,
            "tool_choice": "auto",
            "temperature": 0.2,
            "max_tokens":  16384,
        })

        def _call():
            return _copilot_session.post(
                f"{_COPILOT_BASE}/chat/completions",
                data=json.dumps(body),
                timeout=120,
            )

        resp = await asyncio.to_thread(_call)

        if resp.status_code == 401:
            yield _sse({"type": "error", "text": "GitHub Copilot: Unauthorized. Token may be expired."})
            yield "data: [DONE]\n\n"
            return
        if resp.status_code == 429:
            yield _sse({"type": "error", "text": "GitHub Copilot: Rate limited. Switch models or wait."})
            yield "data: [DONE]\n\n"
            return
        if not resp.ok:
            yield _sse({"type": "error", "text": f"Copilot error ({resp.status_code}): {resp.text[:400]}"})
            yield "data: [DONE]\n\n"
            return

        data       = resp.json()
        choice     = data["choices"][0]
        message    = choice["message"]
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            full_text = (message.get("content") or "").strip()
            if full_text:
                yield _sse({"type": "text", "text": full_text})
            # Auto-log if files were changed and LM didn't call log_feature itself
            if _written_files:
                await asyncio.to_thread(_auto_log_feature, user_prompt, _written_files)
                yield _sse({"type": "feature_logged", "files": _written_files})
            yield "data: [DONE]\n\n"
            return

        # Append assistant turn (with tool calls)
        oai_messages.append(message)

        # Execute each tool call
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments", "{}"))
            except Exception:
                args = {}

            yield _sse({"type": "tool_start", "name": name, "args": args})
            result = await asyncio.to_thread(execute_tool, name, args)

            if name == "write_file" and result.get("success"):
                _written_files.append(args.get("path", "?"))
            if name == "log_feature" and result.get("success"):
                _written_files.clear()  # LM handled it — don't double-log

            yield _sse({"type": "tool_end", "name": name,
                        "summary": _result_summary(result),
                        "result":  json.dumps(result)[:2000]})

            oai_messages.append({
                "role":         "tool",
                "tool_call_id": tc["id"],
                "content":      json.dumps(result),
            })

    yield _sse({"type": "error", "text": "Agent reached max tool rounds without a final answer."})
    yield "data: [DONE]\n\n"


# ── Copilot CLI subprocess bridge ────────────────────────────────────────────
_NODE_EXE       = r"C:\Program Files\nodejs\node.exe"
_COPILOT_LOADER = r"C:\Program Files\nodejs\node_modules\@github\copilot\npm-loader.js"


class RunRequest(BaseModel):
    prompt: str


@app.post("/api/run")
async def run_agent(req: RunRequest):
    """Invoke the local GitHub Copilot CLI with the user's prompt and stream its output."""
    return StreamingResponse(
        _run_copilot_stream(req.prompt, str(_ROOT)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _run_copilot_stream(prompt: str, workdir: str) -> AsyncGenerator[str, None]:
    # Snapshot project files BEFORE running so we can detect what changed afterward
    before = await asyncio.to_thread(_snapshot_project)

    cmd = [
        _NODE_EXE, _COPILOT_LOADER,
        "-p",            prompt,
        "--allow-all",
        "--add-dir",     workdir,
        "--silent",
        "--no-ask-user",
        "--autopilot",
        "--no-color",
        "--no-auto-update",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
        )
    except Exception as exc:
        yield _sse({"type": "error", "text": f"Failed to start Copilot CLI: {exc}"})
        yield "data: [DONE]\n\n"
        return

    # Stream stdout in real-time as it arrives
    assert proc.stdout is not None
    while True:
        chunk = await proc.stdout.read(512)
        if not chunk:
            break
        text = chunk.decode("utf-8", errors="replace")
        yield _sse({"type": "text", "text": text})

    # Collect stderr (warnings / errors from the CLI)
    assert proc.stderr is not None
    stderr_raw = await proc.stderr.read()
    await proc.wait()

    if stderr_raw:
        stderr_text = stderr_raw.decode("utf-8", errors="replace").strip()
        # Filter out noisy lines (update nags, ANSI leftovers, etc.)
        meaningful = [l for l in stderr_text.splitlines()
                      if l.strip() and not l.startswith("\x1b")]
        if meaningful:
            yield _sse({"type": "stderr", "text": "\n".join(meaningful)})

    # Detect changed files and emit for the UI review panel
    changes = await asyncio.to_thread(_find_changed_files, before)
    if changes:
        yield _sse({"type": "file_changes", "changes": changes})

    yield _sse({"type": "done", "code": proc.returncode})
    yield "data: [DONE]\n\n"


# ── Changes / Revert API ─────────────────────────────────────────────────────
@app.get("/api/changes")
async def list_changes():
    """List all files modified this session that can be reverted."""
    return {
        "changes": [
            {
                "path":      p,
                "action":    "created" if v is None else "modified",
                "revertable": True,
            }
            for p, v in _file_backups.items()
        ]
    }


class RevertRequest(BaseModel):
    path: str


@app.post("/api/revert")
async def revert_file(req: RevertRequest):
    """Restore a file to its state before this session's first write."""
    rel = req.path
    if rel not in _file_backups:
        raise HTTPException(404, f"No backup found for: {rel}")
    path = _safe_path(rel)
    if path is None:
        raise HTTPException(400, "Invalid or unsafe path")
    backup = _file_backups.pop(rel)
    if backup is None:
        # File was newly created — delete it
        if path.exists():
            path.unlink()
        return {"success": True, "action": "deleted", "path": rel}
    else:
        path.write_bytes(backup)
        return {"success": True, "action": "restored", "path": rel}


# ── Features API ─────────────────────────────────────────────────────────────

class FeatureCreate(BaseModel):
    name: str
    description: str = ""


class FeatureUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


@app.get("/api/features")
async def get_features():
    return {"features": _features}


@app.post("/api/features", status_code=201)
async def create_feature(req: FeatureCreate):
    now = datetime.datetime.utcnow().isoformat() + "Z"
    feature = {
        "id": str(uuid.uuid4()), "name": req.name.strip(), "description": req.description.strip(),
        "status": "active", "source": "user", "addedAt": now, "addedBy": _whoami_str(), "localOnly": False,
    }
    _features.append(feature)
    _save_features(_features)
    return feature


@app.patch("/api/features/{feature_id}")
async def update_feature(feature_id: str, req: FeatureUpdate):
    now = datetime.datetime.utcnow().isoformat() + "Z"
    for f in _features:
        if f["id"] == feature_id:
            if req.name is not None:
                f["name"] = req.name.strip()
            if req.description is not None:
                f["description"] = req.description.strip()
            f["updatedAt"] = now
            f["updatedBy"] = _whoami_str()
            _save_features(_features)
            return f
    raise HTTPException(404, f"Feature not found: {feature_id}")


@app.delete("/api/features/{feature_id}")
async def delete_feature(feature_id: str):
    global _features
    before = len(_features)
    _features = [f for f in _features if f["id"] != feature_id]
    if len(_features) == before:
        raise HTTPException(404, f"Feature not found: {feature_id}")
    _save_features(_features)
    return {"success": True}


# ── Dev entrypoint ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n=== AI Chat Server (with file-editing agent) ===")
    print(f"  Vertex AI     : {'[OK] ready' if _vertex_session  else '[--] not configured'}")
    print(f"  GitHub Copilot: {'[OK] ready' if _copilot_session else '[--] not configured'}")
    print(f"  Project root  : {_ROOT}")
    print(f"  URL           : http://localhost:7777\n")
    uvicorn.run(app, host="0.0.0.0", port=7777, reload=False, log_level="info")
