"""
Vertex AI client — direct REST API with service account auth.

Supports:
  • Google Gemini models  (publishers/google)   — default
  • Anthropic Claude models (publishers/anthropic) — set VERTEX_MODEL=claude-*
  • GitHub Copilot API   (gpt-4o / gpt-4o-mini)  — set VERTEX_MODEL=gpt-*

Environment variables:
  VERTEX_PROJECT_ID            GCP project id
  VERTEX_LOCATION              e.g. us-central1  (Gemini default)
  VERTEX_CLAUDE_LOCATION       e.g. us-east5     (Claude default — Model Garden region)
  VERTEX_MODEL                 Gemini: gemini-2.5-flash / gemini-2.5-pro
                               Claude: claude-3-5-sonnet@20241022 / claude-3-opus@20240229
                               Copilot: gpt-4o / gpt-4o-mini
  VERTEX_SERVICE_ACCOUNT_JSON  path to service-account JSON  -OR-  raw JSON string
  VERTEX_ACCESS_TOKEN          pre-minted bearer token (short-lived alternative)
  GOOGLE_APPLICATION_CREDENTIALS  fallback ADC path
  GITHUB_COPILOT_TOKEN         GitHub OAuth token for Copilot API (GPT-4o)
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
VERTEX_AVAILABLE = False
_session: Optional[Any]  = None
_project_id: str         = ""
_location:   str         = "us-central1"
_claude_location: str    = "us-east5"   # Claude Model Garden region
_model_name: str         = "gemini-2.5-flash"
GEMINI_AVAILABLE         = True  # always True — we use REST directly

_CLAUDE_PREFIXES  = ("claude-",)
_COPILOT_PREFIXES = ("gpt-",)

# Copilot-specific state (plain requests.Session with GitHub token)
_copilot_session: Optional[Any] = None
COPILOT_AVAILABLE: bool         = False
_COPILOT_API_BASE               = "https://api.githubcopilot.com"

# Models the UI can offer — (id, display_label, provider)
AVAILABLE_MODELS = [
    ("gemini-2.5-flash",   "Gemini 2.5 Flash  — fast",              "gemini"),
    ("gemini-2.5-pro",     "Gemini 2.5 Pro  — better reasoning",     "gemini"),
    ("gpt-4o",             "GPT-4o  — ⭐ OpenAI via Copilot",        "copilot"),
    ("gpt-4o-mini",        "GPT-4o mini  — fast + cheap",            "copilot"),
    ("claude-sonnet-4-5",  "Claude Sonnet 4.5  — best reasoning",    "claude"),
    ("claude-haiku-4-5",   "Claude Haiku 4.5  — fast + smart",       "claude"),
    ("claude-opus-4-5",    "Claude Opus 4.5  — most powerful",        "claude"),
]


def _is_claude() -> bool:
    return any(_model_name.startswith(p) for p in _CLAUDE_PREFIXES)


def _is_copilot() -> bool:
    return any(_model_name.startswith(p) for p in _COPILOT_PREFIXES)


def set_model(name: str) -> None:
    """Hot-swap the active model at runtime — no restart needed."""
    global _model_name
    _model_name = name
    logger.info(f"Model switched to: {name}")


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
    global VERTEX_AVAILABLE, _session, _project_id, _location, _claude_location, _model_name
    global _copilot_session, COPILOT_AVAILABLE

    if not GOOGLE_AUTH_AVAILABLE:
        logger.error("google-auth not installed — cannot use Vertex AI.")
        return False

    _project_id     = _strip_quotes(os.environ.get("VERTEX_PROJECT_ID", ""))
    if not _project_id:
        logger.warning("VERTEX_PROJECT_ID not set — AI features disabled.")
        return False

    _location        = _strip_quotes(os.environ.get("VERTEX_LOCATION",        "us-central1"))
    _claude_location = _strip_quotes(os.environ.get("VERTEX_CLAUDE_LOCATION", "global"))
    _model_name      = _strip_quotes(os.environ.get("VERTEX_MODEL",           "gemini-2.5-flash"))

    # ── GitHub Copilot session (GPT-4o) ─────────────────────────────────────
    gh_token = _strip_quotes(os.environ.get("GITHUB_COPILOT_TOKEN", ""))
    if gh_token:
        import requests as req
        _copilot_session = req.Session()
        _copilot_session.headers.update({
            "Authorization":          f"Bearer {gh_token}",
            "Content-Type":           "application/json",
            "Accept":                 "application/json",
            "editor-version":         "vscode/1.96.0",
            "editor-plugin-version":  "copilot-chat/0.22.4",
            "copilot-integration-id": "vscode-chat",
        })
        COPILOT_AVAILABLE = True
        logger.info("GitHub Copilot API ready (GPT-4o available)")

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
    if _is_claude():
        # Claude is available via global endpoint (recommended) or specific regions
        if _claude_location == "global":
            return (
                f"https://aiplatform.googleapis.com/v1"
                f"/projects/{_project_id}/locations/global"
                f"/publishers/anthropic/models/{_model_name}:rawPredict"
            )
        return (
            f"https://{_claude_location}-aiplatform.googleapis.com/v1"
            f"/projects/{_project_id}/locations/{_claude_location}"
            f"/publishers/anthropic/models/{_model_name}:rawPredict"
        )
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


# ─────────────────────────────────────────────────────────────────────────────
# Claude ↔ Gemini format translation
# ─────────────────────────────────────────────────────────────────────────────

def _gemini_contents_to_anthropic(contents: List[Dict]) -> List[Dict]:
    """
    Convert Gemini-format contents history to Anthropic Messages API format.

    Gemini model turn:  role=model, parts=[{functionCall: {name, args}}, {text}]
    Anthropic:          role=assistant, content=[{type:tool_use, id, name, input}, {type:text}]

    Gemini user turn:   role=user, parts=[{functionResponse: {name, response}}, {text}]
    Anthropic:          role=user, content=[{type:tool_result, tool_use_id, content}, {type:text}]

    Tool IDs are assigned sequentially and matched positionally (model turn → next user turn)
    so duplicate tool names within a single turn are handled correctly.
    """
    # Pre-assign IDs to every functionCall across all model turns, in order
    counter = 0
    turn_ids: List[Optional[List[str]]] = []   # parallel to contents; None for user turns
    for c in contents:
        if c.get("role") == "model":
            ids = []
            for p in c.get("parts", []):
                if "functionCall" in p:
                    ids.append(f"toolu_{counter}")
                    counter += 1
            turn_ids.append(ids)
        else:
            turn_ids.append(None)

    messages: List[Dict] = []
    pending_ids: List[str] = []   # tool IDs from the immediately preceding model turn

    for i, c in enumerate(contents):
        role  = c.get("role", "")
        parts = c.get("parts", [])

        if role == "model":
            pending_ids = turn_ids[i] or []
            blocks = []
            tool_pos = 0
            for p in parts:
                if "text" in p and p["text"].strip():
                    blocks.append({"type": "text", "text": p["text"]})
                elif "functionCall" in p:
                    fc  = p["functionCall"]
                    tid = pending_ids[tool_pos] if tool_pos < len(pending_ids) else f"toolu_x{tool_pos}"
                    tool_pos += 1
                    blocks.append({
                        "type":  "tool_use",
                        "id":    tid,
                        "name":  fc["name"],
                        "input": fc.get("args", {}),
                    })
            if blocks:
                messages.append({"role": "assistant", "content": blocks})

        elif role == "user":
            blocks = []
            resp_pos = 0
            for p in parts:
                if "text" in p and p["text"].strip():
                    blocks.append({"type": "text", "text": p["text"]})
                elif "functionResponse" in p:
                    fr  = p["functionResponse"]
                    tid = pending_ids[resp_pos] if resp_pos < len(pending_ids) else f"toolu_r{resp_pos}"
                    resp_pos += 1
                    result_str = json.dumps(fr.get("response", {}), default=str)
                    if len(result_str) > 12_000:
                        result_str = result_str[:12_000] + "…[truncated]"
                    blocks.append({
                        "type":        "tool_result",
                        "tool_use_id": tid,
                        "content":     result_str,
                    })
            if blocks:
                messages.append({"role": "user", "content": blocks})
            if resp_pos > 0:
                pending_ids = []   # consumed; reset so next user turn doesn't reuse

    return messages


def _anthropic_to_gemini_response(resp: Dict) -> Dict:
    """Convert Anthropic rawPredict response → Gemini generateContent format."""
    parts = []
    for block in resp.get("content", []):
        btype = block.get("type", "")
        if btype == "text":
            text = block.get("text", "").strip()
            if text:
                parts.append({"text": text})
        elif btype == "tool_use":
            parts.append({
                "functionCall": {
                    "name": block["name"],
                    "args": block.get("input", {}),
                }
            })
    stop_reason   = resp.get("stop_reason", "end_turn")
    finish_reason = "STOP" if stop_reason == "end_turn" else "OTHER"
    return {
        "candidates": [{
            "content":      {"role": "model", "parts": parts},
            "finishReason": finish_reason,
        }]
    }


def _gemini_tools_to_anthropic(tools: List[Dict]) -> List[Dict]:
    """Convert Gemini functionDeclarations → Anthropic tools format."""
    result = []
    for group in (tools or []):
        for decl in group.get("functionDeclarations", []):
            params = decl.get("parameters", {"type": "object", "properties": {}})
            result.append({
                "name":         decl["name"],
                "description":  decl.get("description", ""),
                "input_schema": params,
            })
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Main generate_content — routes to Gemini or Claude transparently
# ─────────────────────────────────────────────────────────────────────────────

def generate_content(
    contents: List[Dict],
    tools: Optional[List[Dict]] = None,
    system_instruction: Optional[str] = None,
    temperature: float = 0.0,
) -> Optional[Dict]:
    """
    Call the active AI model.  Returns a Gemini-format response dict regardless
    of backend (Gemini, Claude, or GitHub Copilot GPT-4o).
    Agent.py sees the same interface for all providers.
    """
    if _is_copilot():
        if not COPILOT_AVAILABLE or _copilot_session is None:
            raise RuntimeError(
                "GitHub Copilot API not configured. "
                "Set GITHUB_COPILOT_TOKEN in .env and restart the server."
            )
        return _generate_content_copilot(contents, tools, system_instruction, temperature)
    if not VERTEX_AVAILABLE or _session is None:
        return None
    if _is_claude():
        return _generate_content_claude(contents, tools, system_instruction, temperature)
    return _generate_content_gemini(contents, tools, system_instruction, temperature)


def _generate_content_gemini(
    contents: List[Dict],
    tools: Optional[List[Dict]],
    system_instruction: Optional[str],
    temperature: float,
) -> Optional[Dict]:
    """Gemini generateContent via Vertex AI REST."""
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


def _generate_content_claude(
    contents: List[Dict],
    tools: Optional[List[Dict]],
    system_instruction: Optional[str],
    temperature: float,
) -> Optional[Dict]:
    """Claude rawPredict via Vertex AI Model Garden — returns Gemini-format dict."""
    messages = _gemini_contents_to_anthropic(contents)

    body: Dict[str, Any] = {
        "anthropic_version": "vertex-2023-10-16",
        "max_tokens":        8096,
        "temperature":       temperature,
        "messages":          messages,
    }
    if system_instruction:
        body["system"] = system_instruction
    if tools:
        body["tools"] = _gemini_tools_to_anthropic(tools)

    try:
        body_str = json.dumps(_clean_body(body), default=str)
        logger.debug(f"Claude request → {_endpoint()} | messages={len(messages)}")
        resp = _session.post(
            _endpoint(),
            data=body_str,
            headers={"Content-Type": "application/json"},
            timeout=120,
        )
        if resp.status_code == 429:
            raise RuntimeError("Claude rate-limited (429). Retry in a few seconds.")
        if resp.status_code == 404:
            raise RuntimeError(
                f"Claude model '{_model_name}' is not enabled for this project.\n\n"
                "**To enable Claude on Vertex AI:**\n"
                "1. Go to Google Cloud Console → Vertex AI → Model Garden\n"
                "2. Search for 'Claude' and click on the model\n"
                "3. Click **Enable** and accept Anthropic's terms\n"
                "4. Wait ~2 minutes, then try again\n\n"
                "Alternatively, switch to a Gemini model using the model selector above."
            )
        if not resp.ok:
            snippet = resp.text[:700] if resp.text else "(no body)"
            raise RuntimeError(f"Claude error ({resp.status_code}): {snippet}")
        anthropic_resp = resp.json()
        gemini_fmt = _anthropic_to_gemini_response(anthropic_resp)
        logger.debug(f"Claude response parsed OK — stop_reason={anthropic_resp.get('stop_reason')}")
        return gemini_fmt
    except RuntimeError:
        raise
    except Exception as exc:
        logger.error(f"Claude request failed: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Gemini ↔ OpenAI format translation  (for GitHub Copilot / GPT-4o)
# ─────────────────────────────────────────────────────────────────────────────

def _gemini_contents_to_openai(
    contents: List[Dict],
    system_instruction: Optional[str],
) -> List[Dict]:
    """Convert Gemini-format contents + system prompt → OpenAI messages list."""
    messages: List[Dict] = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})

    # Pre-assign tool_call IDs to every functionCall across model turns
    counter = 0
    turn_ids: List[Optional[List[str]]] = []
    for c in contents:
        if c.get("role") == "model":
            ids = [f"call_{counter + k}" for k, p in enumerate(c.get("parts", [])) if "functionCall" in p]
            counter += len(ids)
            turn_ids.append(ids)
        else:
            turn_ids.append(None)

    pending_ids: List[str] = []

    for i, c in enumerate(contents):
        role  = c.get("role", "")
        parts = c.get("parts", [])

        if role == "model":
            pending_ids = turn_ids[i] or []
            text_parts  = [p["text"] for p in parts if "text" in p and p["text"].strip()]
            tool_calls  = []
            tool_pos    = 0
            for p in parts:
                if "functionCall" in p:
                    fc  = p["functionCall"]
                    tid = pending_ids[tool_pos] if tool_pos < len(pending_ids) else f"call_x{tool_pos}"
                    tool_pos += 1
                    tool_calls.append({
                        "id":   tid,
                        "type": "function",
                        "function": {
                            "name":      fc["name"],
                            "arguments": json.dumps(fc.get("args", {}), default=str),
                        },
                    })
            msg: Dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)

        elif role == "user":
            # Split functionResponse parts from plain text parts
            tool_results = [p for p in parts if "functionResponse" in p]
            text_parts   = [p["text"].strip() for p in parts if "text" in p and p["text"].strip()]

            if tool_results:
                for k, p in enumerate(tool_results):
                    fr  = p["functionResponse"]
                    tid = pending_ids[k] if k < len(pending_ids) else f"call_r{k}"
                    result_str = json.dumps(fr.get("response", {}), default=str)
                    if len(result_str) > 12_000:
                        result_str = result_str[:12_000] + "…[truncated]"
                    messages.append({
                        "role":         "tool",
                        "tool_call_id": tid,
                        "content":      result_str,
                    })
                pending_ids = []
            if text_parts:
                messages.append({"role": "user", "content": "\n".join(text_parts)})

    return messages


def _gemini_tools_to_openai(tools: List[Dict]) -> List[Dict]:
    """Convert Gemini functionDeclarations → OpenAI tools format."""
    result = []
    for group in (tools or []):
        for decl in group.get("functionDeclarations", []):
            params = decl.get("parameters", {"type": "object", "properties": {}})
            result.append({
                "type": "function",
                "function": {
                    "name":        decl["name"],
                    "description": decl.get("description", ""),
                    "parameters":  params,
                },
            })
    return result


def _openai_to_gemini_response(resp: Dict) -> Dict:
    """Convert OpenAI chat/completions response → Gemini generateContent format."""
    choice  = resp.get("choices", [{}])[0]
    message = choice.get("message", {})
    parts: List[Dict] = []

    content = message.get("content") or ""
    if content.strip():
        parts.append({"text": content})

    for tc in message.get("tool_calls", []):
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments", "{}"))
        except Exception:
            args = {}
        parts.append({
            "functionCall": {
                "name": fn.get("name", ""),
                "args": args,
            }
        })

    finish = choice.get("finish_reason", "stop")
    finish_reason = "STOP" if finish in ("stop", "end_turn") else "OTHER"
    return {
        "candidates": [{
            "content":      {"role": "model", "parts": parts},
            "finishReason": finish_reason,
        }]
    }


def _generate_content_copilot(
    contents: List[Dict],
    tools: Optional[List[Dict]],
    system_instruction: Optional[str],
    temperature: float,
) -> Optional[Dict]:
    """Call GitHub Copilot API (GPT-4o) — returns Gemini-format dict."""
    messages = _gemini_contents_to_openai(contents, system_instruction)

    body: Dict[str, Any] = {
        "model":       _model_name,
        "messages":    messages,
        "temperature": temperature,
        "max_tokens":  8096,
        "stream":      False,
    }
    if tools:
        body["tools"]       = _gemini_tools_to_openai(tools)
        body["tool_choice"] = "auto"

    try:
        body_str = json.dumps(_clean_body(body), default=str)
        logger.debug(f"Copilot request → {_COPILOT_API_BASE}/chat/completions | model={_model_name} msgs={len(messages)}")
        resp = _copilot_session.post(
            f"{_COPILOT_API_BASE}/chat/completions",
            data=body_str,
            timeout=120,
        )
        if resp.status_code == 401:
            raise RuntimeError(
                "GitHub Copilot API: Unauthorized (401). "
                "Check GITHUB_COPILOT_TOKEN in .env — token may have expired."
            )
        if resp.status_code == 429:
            body_snippet = resp.text[:500] if resp.text else "(no body)"
            headers_info = {k: v for k, v in resp.headers.items() if k.lower().startswith(("retry", "x-rate", "x-ratelimit", "content"))}
            logger.error(f"Copilot 429 — headers={headers_info} body={body_snippet}")
            # Distinguish quota exhaustion (long Retry-After) from transient rate-limit
            retry_after = resp.headers.get("Retry-After") or resp.headers.get("x-ratelimit-user-retry-after", "0")
            quota_exceeded = resp.headers.get("x-ratelimit-exceeded", "") == "quota_exceeded"
            if quota_exceeded or (retry_after and int(retry_after) > 3600):
                hours = int(retry_after) // 3600 if retry_after else "unknown"
                raise RuntimeError(
                    f"GitHub Copilot GPT-4o quota exhausted — resets in ~{hours}h. "
                    "Switch to Gemini or Claude in the model selector."
                )
            raise RuntimeError("GitHub Copilot API rate-limited (429). Retry in a few seconds.")
        if not resp.ok:
            snippet = resp.text[:700] if resp.text else "(no body)"
            raise RuntimeError(f"GitHub Copilot API error ({resp.status_code}): {snippet}")
        openai_resp = resp.json()
        gemini_fmt  = _openai_to_gemini_response(openai_resp)
        logger.debug(f"Copilot response OK — finish={openai_resp.get('choices',[{}])[0].get('finish_reason')}")
        return gemini_fmt
    except RuntimeError:
        raise
    except Exception as exc:
        logger.error(f"GitHub Copilot request failed: {exc}")
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
