"""
Vertex AI Gemini agent — multi-turn REST conversation loop.

Pattern mirrors simulationLocal reference project:
  1. User query + system prompt + tools  →  POST /generateContent
  2. If Gemini returns a functionCall  →  execute Python tool
  3. Append model turn + functionResponse to history
  4. Repeat until Gemini returns plain text
  5. Return structured response
"""

import re
import time
from typing import Any, Dict, List, Optional
from loguru import logger

from app.ai.vertex_client import (
    is_available, generate_content,
    build_tools, extract_function_call, extract_text,
    make_function_response_message,
)
from app.ai.tool_registry import TOOL_DEFINITIONS, execute_tool
from app.ai.prompts import build_system_prompt
from app.ai import session_store
from app.database.queries import log_query


_MAX_TOOL_ROUNDS = 15

# Cache the resolved schedule name (set once at startup via init_schedule_name())
_schedule_name: str = "the loaded schedule"
_host_airline: str = ""


def _prune_tool_data_from_history(contents: List[Dict]) -> List[Dict]:
    """
    Strip raw functionResponse payloads from prior session history.

    The LM only needs the TEXT turns from previous rounds to maintain
    conversational context (so follow-up questions work).  Feeding it
    raw tool-result JSON from prior turns causes it to blend stale data
    with fresh data and produce contradictory answers.

    Kept:    role=user text turns, role=model text turns, functionCall declarations
    Stripped: functionResponse data bodies  →  replaced with a lightweight stub
    Also:    persona-specific Takeaway lines removed from old model text turns
             so the LM doesn't mimic a stale persona style.
    """
    # Matches "**<Persona Name> Takeaway:**" and everything after it on that line
    _takeaway_re = re.compile(
        r'\*\*(?:Revenue Manager|Route Analyst|Network Strategist|Ops Manager|Alliance Director) Takeaway:?\*\*.*',
        re.IGNORECASE,
    )

    pruned: List[Dict] = []
    for turn in contents:
        role  = turn.get("role", "")
        parts = turn.get("parts", [])
        new_parts: List[Dict] = []
        for p in parts:
            if "text" in p:
                if role == "model":
                    # Strip persona Takeaway lines so stale persona style doesn't leak
                    cleaned = "\n".join(
                        line for line in p["text"].split("\n")
                        if not _takeaway_re.match(line.strip())
                    )
                    new_parts.append({"text": cleaned})
                else:
                    new_parts.append(p)
            elif "functionCall" in p:
                # Keep the call declaration so the LM knows which tool was invoked
                new_parts.append(p)
            elif "functionResponse" in p:
                # Replace payload with a minimal stub that preserves the role structure
                name = p.get("functionResponse", {}).get("name", "tool")
                new_parts.append({
                    "functionResponse": {
                        "name":     name,
                        "response": {
                            "_pruned": True,
                            "_note":   (
                                f"Data from {name} was retrieved and used in the "
                                "assistant's text reply for that turn. "
                                "Do NOT use it for the current question — call fresh tools."
                            ),
                        },
                    }
                })
        if new_parts:
            pruned.append({"role": role, "parts": new_parts})
    return pruned


def init_schedule_name() -> None:
    """
    Read the loaded schedule file name(s) from the DB and cache them
    so the system prompt can reference the actual data source.
    Also reads the host airline from workset profile.
    Called once during app startup after ingestion.
    """
    global _schedule_name, _host_airline
    try:
        from app.database.db import get_connection
        conn = get_connection()
        rows = conn.execute(
            "SELECT DISTINCT source_file FROM flights WHERE source_file IS NOT NULL AND source_file != '' LIMIT 5"
        ).fetchall()
        if rows:
            names = [r[0] for r in rows]
            _schedule_name = ", ".join(names)
    except Exception:
        pass  # keep default

    # Read host airline from workset profile
    try:
        from app.services.workset_service import get_dashboard_profile
        prof = get_dashboard_profile()
        if prof and prof.get("host_airline"):
            _host_airline = prof["host_airline"]
    except Exception:
        pass


def _get_system_prompt(persona: Optional[str] = None) -> str:
    host = _host_airline
    if not host:
        # Workset may not have been loaded when init_schedule_name() first ran — read dynamically
        try:
            from app.services.workset_service import get_dashboard_profile
            prof = get_dashboard_profile()
            if prof and prof.get("host_airline"):
                host = prof["host_airline"]
        except Exception:
            pass
    return build_system_prompt(_schedule_name, persona=persona, host_airline=host or None)


class ScheduleAgent:
    """Vertex AI Gemini agent for airline schedule intelligence queries."""

    def __init__(self, model_name: Optional[str] = None):
        self.model_name = model_name  # overrides VERTEX_MODEL env var if set
        self._tools_payload: Optional[List[Dict]] = None

    def _get_tools(self) -> List[Dict]:
        if self._tools_payload is None:
            self._tools_payload = build_tools(TOOL_DEFINITIONS)
        return self._tools_payload

    # ─────────────────────────────────────────────────────────────────────────
    # Public entry point
    # ─────────────────────────────────────────────────────────────────────────

    def query(self, user_query: str, session_id: Optional[str] = None, persona: Optional[str] = None, panel_context: Optional[str] = None) -> Dict[str, Any]:
        """
        Process a natural-language query through the Gemini agent loop.

        If session_id is provided (and exists), the prior conversation history is
        included so Gemini has full context of the thread.
        If session_id is None or not found, a new session is created.
        If persona is provided, the system prompt is augmented with persona-specific lens.
        If panel_context is provided, it is prepended to the user query so the LM can
        cross-validate its answer against the pre-aggregated dashboard numbers.

        Falls back to deterministic-only mode when Vertex AI is not configured.
        """
        start_time = time.time()

        # ── Session management ────────────────────────────────────────────────
        if not session_id or not session_store.session_exists(session_id):
            session_id = session_store.create_session()
        prior_contents = session_store.get_contents(session_id)

        # Strip raw tool-result payloads from prior turns so the LM bases its
        # answer solely on tools called in THIS turn, not stale data from earlier
        # turns that could cause contradictory or blended responses.
        prior_contents = _prune_tool_data_from_history(prior_contents)

        # If the dashboard has pre-aggregated KPI context, prepend it so the LM
        # can cross-validate its answer (e.g. "flights = 234" must match panel).
        effective_query = user_query
        if panel_context:
            effective_query = f"{panel_context}\n\n{user_query}"

        if not is_available():
            return self._deterministic_fallback(user_query, start_time, session_id)

        tools_used:   List[str]  = []
        tool_results: List[Dict] = []
        final_answer: str        = ""
        confidence:   str        = "Low"

        # Build history: prior session context + new user message (with panel context prepended if available)
        history: List[Dict] = prior_contents + [
            {"role": "user", "parts": [{"text": effective_query}]}
        ]

        try:
            for _round in range(_MAX_TOOL_ROUNDS):
                response = generate_content(
                    contents=history,
                    tools=self._get_tools(),
                    system_instruction=_get_system_prompt(persona=persona),
                    temperature=0.0,
                )

                if response is None:
                    logger.error("No response from Vertex AI.")
                    break

                # Collect ALL function calls from this model turn
                try:
                    model_content = response["candidates"][0]["content"]
                    fc_parts = [
                        p for p in model_content.get("parts", [])
                        if "functionCall" in p
                    ]
                except (KeyError, IndexError):
                    fc_parts = []

                if fc_parts:
                    # Build exactly one functionResponse per functionCall (Vertex AI requirement)
                    fn_response_parts = []
                    for part in fc_parts:
                        fc = part["functionCall"]
                        name = fc.get("name", "")
                        args = fc.get("args", {})
                        logger.info(f"Gemini -> tool: {name}  args={list(args.keys())}")
                        tools_used.append(name)

                        tool_result = execute_tool(name, args)
                        tool_results.append(tool_result)

                        fn_response_parts.append({
                            "functionResponse": {
                                "name":     name,
                                "response": _safe_json(tool_result),
                            }
                        })

                    # Append model fc turn, then all responses in a single user turn
                    history.append({"role": "model", "parts": fc_parts})
                    history.append({"role": "user",  "parts": fn_response_parts})
                    continue

                # ── Plain-text final answer ───────────────────────────────
                final_answer = extract_text(response)
                break

        except Exception as exc:
            logger.exception(f"Agent loop error: {exc}")
            final_answer = f"Agent error: {exc}"

        # ── Synthesis fallback: if tool rounds exhausted without text ─────────
        if not final_answer and tool_results:
            logger.warning("Tool rounds exhausted without plain-text answer — requesting synthesis.")
            try:
                synth_response = generate_content(
                    contents=history + [{
                        "role": "user",
                        "parts": [{"text": (
                            "You have gathered the relevant data above. "
                            "Please write your final analysis and directly answer the user's question. "
                            "Be thorough but concise. Include all relevant numbers from the tool results."
                        )}],
                    }],
                    tools=None,          # No tools — force a plain-text response
                    system_instruction=_get_system_prompt(persona=persona),
                    temperature=0.1,
                )
                if synth_response:
                    final_answer = extract_text(synth_response)
                    if final_answer:
                        logger.info("Synthesis call produced an answer.")
            except Exception as synth_exc:
                logger.error(f"Synthesis call failed: {synth_exc}")

        # Pull confidence: prefer Gemini's stated confidence in the answer,
        # fall back to last tool result
        import re as _re
        m = _re.search(r"confidence[:\s]+\**(High|Medium|Low)\**", final_answer, _re.IGNORECASE)
        if m:
            confidence = m.group(1).capitalize()
        else:
            for tr in reversed(tool_results):
                c = tr.get("confidence")
                if c in ("High", "Medium", "Low"):
                    confidence = c
                    break

        elapsed = round(time.time() - start_time, 2)
        log_query(user_query, "natural_language", ",".join(tools_used), elapsed)

        # Persist session turn
        turn = session_store.save_turn(
            session_id,
            new_contents=history,
            user_text=user_query,
            assistant_text=final_answer or "No answer generated.",
            tools_used=tools_used,
            confidence=confidence,
        )

        return {
            "answer":        final_answer or "No answer generated.",
            "session_id":    session_id,
            "turn":          turn,
            "chat_history":  session_store.get_chat_history(session_id),
            "tools_used":    tools_used,
            "tool_results":  tool_results,
            "confidence":    confidence,
            "response_time": elapsed,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Deterministic fallback (Vertex AI not configured)
    # ─────────────────────────────────────────────────────────────────────────

    def _deterministic_fallback(self, user_query: str, start_time: float, session_id: str) -> Dict[str, Any]:
        logger.warning(
            "Vertex AI not configured. Running deterministic-only mode. "
            "Set VERTEX_PROJECT_ID + VERTEX_SERVICE_ACCOUNT_JSON in .env to enable AI."
        )

        tools_used:   List[str]  = []
        tool_results: List[Dict] = []
        answer:       str        = ""
        q = user_query.lower()

        if any(k in q for k in ["add flight", "new flight", "can we add", "propose flight"]):
            r = execute_tool("simulate_add_flight", _extract_flight_args(user_query))
            tools_used.append("simulate_add_flight"); tool_results.append(r)
            answer = _fmt(r)

        elif any(k in q for k in ["retime", "move flight", "change departure", "reschedule"]):
            r = execute_tool("simulate_retime_flight", _extract_retime_args(user_query))
            tools_used.append("simulate_retime_flight"); tool_results.append(r)
            answer = _fmt(r)

        elif any(k in q for k in ["route", "summary", "how many flights", "frequency"]):
            r = execute_tool("get_route_summary", _extract_search_args(user_query))
            tools_used.append("get_route_summary"); tool_results.append(r)
            count = r.get("total_flights") or len(r.get("routes", []))
            answer = f"Route summary: {count} records found. Set VERTEX_PROJECT_ID in .env for AI analysis."

        elif any(k in q for k in ["search", "flights from", "flights to", "show", "list"]):
            r = execute_tool("search_schedule", _extract_search_args(user_query))
            tools_used.append("search_schedule"); tool_results.append(r)
            answer = (
                f"Found {r.get('count', 0)} flight(s). "
                "Set VERTEX_PROJECT_ID + VERTEX_SERVICE_ACCOUNT_JSON in .env for AI analysis."
            )

        else:
            answer = (
                "Vertex AI is not configured.\n\n"
                "To enable AI-powered queries, add these to airline_schedule_app/.env:\n"
                "  VERTEX_PROJECT_ID=sab-dev-calibteam-3619\n"
                "  VERTEX_LOCATION=us-central1\n"
                "  VERTEX_MODEL=gemini-2.5-flash\n"
                "  VERTEX_SERVICE_ACCOUNT_JSON=<path-to-service-account.json>\n\n"
                "You can still use REST endpoints directly:\n"
                "  POST /api/v1/simulate/add-flight\n"
                "  POST /api/v1/simulate/retime-flight\n"
                "  GET  /api/v1/schedule/search?origin=DXB"
            )

        elapsed = round(time.time() - start_time, 2)
        conf = tool_results[-1].get("confidence", "Low") if tool_results else "Low"
        turn = session_store.save_turn(
            session_id,
            new_contents=[{"role": "user", "parts": [{"text": user_query}]}],
            user_text=user_query,
            assistant_text=answer,
            tools_used=tools_used,
            confidence=conf,
        )
        return {
            "answer":        answer,
            "session_id":    session_id,
            "turn":          turn,
            "chat_history":  session_store.get_chat_history(session_id),
            "tools_used":    tools_used,
            "tool_results":  tool_results,
            "confidence":    conf,
            "response_time": elapsed,
            "mode":          "deterministic",
        }


# ─────────────────────────────────────────────────────────────────────────────
# Minimal keyword-based argument extractors for fallback mode
# ─────────────────────────────────────────────────────────────────────────────

def _extract_flight_args(query: str) -> Dict[str, Any]:
    import re
    from datetime import date
    airports = re.findall(r"\b([A-Z]{3})\b", query.upper())
    times    = re.findall(r"\b(\d{1,2}:\d{2})\b", query)
    today    = date.today().strftime("%Y-%m-%d")
    return {
        "origin":          airports[0] if airports else "DXB",
        "destination":     airports[1] if len(airports) > 1 else "",
        "departure_local": f"{today} {times[0].zfill(5)}" if times else f"{today} 08:00",
    }


def _extract_retime_args(query: str) -> Dict[str, Any]:
    import re
    from datetime import date
    flt   = re.search(r"\b([A-Z]{2}\d{3,4})\b", query.upper())
    times = re.findall(r"\b(\d{1,2}:\d{2})\b", query)
    today = date.today().strftime("%Y-%m-%d")
    return {
        "flight_number":       flt.group(1) if flt else "",
        "new_departure_local": f"{today} {times[0].zfill(5)}" if times else f"{today} 10:00",
    }


def _extract_search_args(query: str) -> Dict[str, Any]:
    import re
    airports = re.findall(r"\b([A-Z]{3})\b", query.upper())
    return {
        "origin":      airports[0] if airports else None,
        "destination": airports[1] if len(airports) > 1 else None,
    }


def _fmt(result: Dict[str, Any]) -> str:
    lines = [result.get("verdict", "No verdict.")]
    if (fs := result.get("feasibility_score")) is not None:
        lines.append(f"Feasibility score : {fs}/100")
    if (nv := result.get("network_value_score")) is not None:
        lines.append(f"Network value     : {nv}/100")
    if viols := result.get("violations", []):
        lines.append("Violations: " + " | ".join(viols[:3]))
    if alts := result.get("alternatives", []):
        lines.append(f"Best alternative  : {alts[0]}")
    return "\n".join(lines)


def _safe_json(obj: Any) -> Any:
    """
    Return a JSON-safe version of obj.
    - Replaces NaN/Inf floats with None (invalid JSON)
    - Truncates large lists to 30 items
    - Converts non-serializable types to strings
    """
    import json, math

    def _clean(v: Any) -> Any:
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        if isinstance(v, list):
            # Allow larger lists for SQL result rows; truncate very large ones
            limit = 300 if len(v) > 50 else len(v)
            return [_clean(i) for i in v[:limit]]
        if isinstance(v, dict):
            return {k: _clean(vv) for k, vv in v.items()}
        return v

    try:
        return json.loads(json.dumps(_clean(obj), default=str))
    except Exception:
        return {"error": str(obj)[:500]}
