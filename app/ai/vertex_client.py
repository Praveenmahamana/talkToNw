"""
Vertex AI client — direct REST API with service account auth.

Pattern mirrors the simulationLocal reference project (server/index.js):
  VERTEX_SERVICE_ACCOUNT_JSON  →  google-auth Credentials  →  Bearer token
  →  POST https://{location}-aiplatform.googleapis.com/v1/projects/{project}/
         locations/{location}/publishers/google/models/{model}:generateContent

Environment variables (same names as reference project):
  VERTEX_PROJECT_ID            GCP project id
  VERTEX_LOCATION              e.g. us-central1  (default)
  VERTEX_MODEL                 e.g. gemini-2.5-flash (default)
  VERTEX_SERVICE_ACCOUNT_JSON  path to service-account JSON file  -OR-  raw JSON string
  VERTEX_ACCESS_TOKEN          pre-minted bearer token (short-lived alternative)
  GOOGLE_APPLICATION_CREDENTIALS  fallback ADC path
"""

import os
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger

# ── Load .env ────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent.parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
        logger.info(f"Loaded .env from {_env_path}")
except ImportError:
    pass

# ── google-auth ───────────────────────────────────────────────────────────────
try:
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    import requests as _requests
    GOOGLE_AUTH_AVAILABLE = True
except ImportError:
    GOOGLE_AUTH_AVAILABLE = False
    logger.warning("google-auth / requests not available. Run: pip install google-auth requests")

# ── Module-level state ────────────────────────────────────────────────────────
VERTEX_AVAILABLE = False          # legacy bool kept for backwards compat
_session: Optional[Any]  = None  # AuthorizedSession or requests.Session
_project_id: str         = ""
_location:   str         = "us-central1"
_model_name: str         = "gemini-2.5-flash"
GEMINI_AVAILABLE         = True  # always True — we use REST directly


# ─────────────────────────────────────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────────────────────────────────────

def _strip_quotes(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] in ('"', "'") and v[-1] == v[0]:
        v = v[1:-1].strip()
    return v


def _load_service_account() -> Optional[Dict]:
    """Load service account JSON from env var (path or inline) or ADC."""
    raw = _strip_quotes(os.environ.get("VERTEX_SERVICE_ACCOUNT_JSON", ""))
    if not raw:
        raw = _strip_quotes(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""))
    if not raw:
        return None
    if raw.startswith("{"):
        return json.loads(raw)
    p = Path(raw)
    if p.exists():
        return json.loads(p.read_text("utf-8"))
    logger.warning(f"Service account JSON not found: {raw}")
    return None


def is_available() -> bool:
    """Return True if Vertex AI is ready to use."""
    return VERTEX_AVAILABLE


def init_vertex() -> bool:
    """
    Initialise the Vertex AI REST session.
    Reads VERTEX_PROJECT_ID + credentials from environment.
    Returns True on success.
    """
    global VERTEX_AVAILABLE, _session, _project_id, _location, _model_name

    if not GOOGLE_AUTH_AVAILABLE:
        logger.error("google-auth not installed — cannot use Vertex AI.")
        return False

    _project_id = _strip_quotes(os.environ.get("VERTEX_PROJECT_ID", ""))
    if not _project_id:
        logger.warning("VERTEX_PROJECT_ID not set — AI features disabled.")
        return False

    _location   = _strip_quotes(os.environ.get("VERTEX_LOCATION",   "us-central1"))
    _model_name = _strip_quotes(os.environ.get("VERTEX_MODEL",      "gemini-2.5-flash"))

    # Option A: pre-minted access token
    access_token = _strip_quotes(os.environ.get("VERTEX_ACCESS_TOKEN", ""))
    if access_token:
        import requests as req
        sess = req.Session()
        sess.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Content-Type":  "application/json",
        })
        _session        = sess
        VERTEX_AVAILABLE = True
        logger.info(f"Vertex AI ready (static token) — project={_project_id} model={_model_name}")
        return True

    # Option B: service account JSON
    sa = _load_service_account()
    if sa:
        try:
            creds = service_account.Credentials.from_service_account_info(
                sa,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            _session        = AuthorizedSession(creds)
            VERTEX_AVAILABLE = True
            logger.info(f"Vertex AI ready (service account {sa.get('client_email','')}) "
                        f"— project={_project_id} model={_model_name}")
            return True
        except Exception as exc:
            logger.error(f"Failed to init Vertex AI credentials: {exc}")
            return False

    logger.warning(
        "No Vertex AI credentials found. "
        "Set VERTEX_SERVICE_ACCOUNT_JSON (or VERTEX_ACCESS_TOKEN) in .env"
    )
    return False


# ─────────────────────────────────────────────────────────────────────────────
# REST helpers
# ─────────────────────────────────────────────────────────────────────────────

def _endpoint() -> str:
    return (
        f"https://{_location}-aiplatform.googleapis.com/v1"
        f"/projects/{_project_id}/locations/{_location}"
        f"/publishers/google/models/{_model_name}:generateContent"
    )


def _clean_body(obj: Any) -> Any:
    """Recursively replace NaN/Inf floats (invalid JSON) with None."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _clean_body(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_body(v) for v in obj]
    return obj


def generate_content(
    contents: List[Dict],
    tools: Optional[List[Dict]] = None,
    system_instruction: Optional[str] = None,
    temperature: float = 0.0,
) -> Optional[Dict]:
    """
    Call Vertex AI generateContent REST endpoint.
    Returns the raw JSON response dict, or None on error.
    """
    if not VERTEX_AVAILABLE or _session is None:
        return None

    body: Dict[str, Any] = {
        "generationConfig": {"temperature": temperature},
        "contents": contents,
    }
    if system_instruction:
        body["systemInstruction"] = {"parts": [{"text": system_instruction}]}
    if tools:
        body["tools"] = tools

    try:
        body_str = json.dumps(_clean_body(body), default=str)
        resp = _session.post(
            _endpoint(),
            data=body_str,
            headers={"Content-Type": "application/json"},
            timeout=90,
        )
        if resp.status_code == 429:
            raise RuntimeError("Vertex AI rate-limited (429). Retry in a few seconds.")
        if not resp.ok:
            snippet = resp.text[:700] if resp.text else "(no body)"
            raise RuntimeError(f"Vertex AI error ({resp.status_code}): {snippet}")
        return resp.json()
    except RuntimeError:
        raise
    except Exception as exc:
        logger.error(f"Vertex AI request failed: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Response parsing
# ─────────────────────────────────────────────────────────────────────────────

def extract_function_call(response: Optional[Dict]) -> Optional[Dict[str, Any]]:
    """Extract the first function call from a generateContent response."""
    if not response:
        return None
    try:
        parts = response["candidates"][0]["content"]["parts"]
        for part in parts:
            if "functionCall" in part:
                fc = part["functionCall"]
                return {"name": fc["name"], "args": fc.get("args", {})}
    except (KeyError, IndexError, TypeError):
        pass
    return None


def extract_text(response: Optional[Dict]) -> str:
    """Extract concatenated text from a generateContent response."""
    if not response:
        return ""
    try:
        parts = response["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, TypeError):
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Tool building
# ─────────────────────────────────────────────────────────────────────────────

def build_tools(tool_defs: List[Dict]) -> List[Dict]:
    """
    Convert OpenAPI-style tool definition dicts to the Vertex AI
    tools payload format:  [{"functionDeclarations": [...]}]
    """
    declarations = []
    for td in tool_defs:
        declarations.append({
            "name":        td["name"],
            "description": td.get("description", ""),
            "parameters":  td.get("parameters", {"type": "object", "properties": {}}),
        })
    return [{"functionDeclarations": declarations}]


def make_function_response_message(tool_name: str, result: Dict) -> Dict:
    """
    Build a function-response turn to feed back to the model.
    Vertex AI REST requires role="user" with functionResponse parts
    (not role="function" — that's OpenAI style).
    """
    return {
        "role": "user",
        "parts": [{
            "functionResponse": {
                "name":     tool_name,
                "response": result,
            }
        }],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Backwards-compat stubs (agent.py uses these names)
# ─────────────────────────────────────────────────────────────────────────────

def get_model(model_name: Optional[str] = None) -> None:
    """No-op stub — model is selected via env var."""
    return None


def start_chat(*args, **kwargs) -> None:
    return None


def send_message(*args, **kwargs) -> None:
    return None


def make_function_response_part(tool_name: str, result: Dict) -> Dict:
    """Alias for make_function_response_message (used by old agent code)."""
    return make_function_response_message(tool_name, result)
