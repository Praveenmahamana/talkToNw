"""
Pydantic request and response schemas for the FastAPI layer.
"""

from datetime import datetime, date
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────────────────────────────────────
# Shared / base
# ─────────────────────────────────────────────────────────────────────────────

class ScheduleResponse(BaseModel):
    """Standard structured response for all schedule intelligence endpoints."""
    verdict:             str              = ""
    facts:               List[str]        = Field(default_factory=list)
    constraints_checked: List[str]        = Field(default_factory=list)
    violations:          List[str]        = Field(default_factory=list)
    warnings:            List[str]        = Field(default_factory=list)
    risks:               List[str]        = Field(default_factory=list)
    alternatives:        List[str]        = Field(default_factory=list)
    confidence:          str              = "Low"
    metadata:            Optional[Dict]   = None


class HealthResponse(BaseModel):
    status:        str
    db_flight_count: int
    vertex_ai:     bool
    model:         str = ""
    version:       str = "1.0.0"


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion
# ─────────────────────────────────────────────────────────────────────────────

class IngestRequest(BaseModel):
    folder_path: str  = Field(..., description="Absolute path to folder containing schedule files")
    clear:       bool = Field(default=False, description="If true, delete ALL existing flights before ingesting (clean workset switch)")


class IngestResponse(BaseModel):
    status:        str
    rows_inserted: int
    rows_skipped:  int
    skip_reasons:  List[str] = Field(default_factory=list)
    report:        Optional[Dict] = None


# ─────────────────────────────────────────────────────────────────────────────
# Query (natural language)
# ─────────────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    query:         str = Field(..., description="Natural language query about the schedule")
    max_results:   int = Field(default=20, ge=1, le=200)
    session_id:    Optional[str] = Field(None, description="Session ID to continue a conversation thread. Omit to start a new thread.")
    persona:       Optional[str] = Field(None, description="Active user persona key (route|network|ops|revenue|alliance) to tailor response style.")
    panel_context: Optional[str] = Field(None, description="Pre-aggregated panel KPI summary string from the dashboard to help LM cross-validate answers.")


class QueryResponse(BaseModel):
    answer:          str
    session_id:      str              = ""
    turn:            int              = 0
    chat_history:    List[Dict]       = Field(default_factory=list)
    tools_used:      List[str]        = Field(default_factory=list)
    tool_results:    List[Dict]       = Field(default_factory=list)
    visualizations:  List[Dict]       = Field(default_factory=list)
    confidence:      str              = "Low"
    response_time:   Optional[float]  = None


class SuggestRequest(BaseModel):
    query:           str            = Field(..., description="The user's last question")
    answer:          str            = Field(..., description="The AI's answer (will be truncated server-side)")
    persona:         Optional[str]  = Field(None, description="Active persona key")
    entities:        List[str]      = Field(default_factory=list, description="IATA codes or entity names mentioned")
    workset_context: Optional[str]  = Field(None, description="Brief workset context (host airline, top airports) for question relevance")


class SuggestResponse(BaseModel):
    questions: List[str] = Field(default_factory=list)


class ChartSuggestRequest(BaseModel):
    query:   str        = Field(..., description="The user's natural language query")
    answer:  str        = Field(..., description="The AI answer text (summary context)")
    columns: List[str]  = Field(default_factory=list, description="Column names from the result data")
    rows:    List[Dict] = Field(default_factory=list, description="Data rows (up to 40)")


class ChartSuggestResponse(BaseModel):
    spec: Optional[Dict] = None   # chart spec chosen by AI
    error: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Simulation — Add Flight
# ─────────────────────────────────────────────────────────────────────────────

class AddFlightRequest(BaseModel):
    origin:          str  = Field(..., description="IATA origin airport code, e.g. DXB")
    destination:     str  = Field(..., description="IATA destination airport code, e.g. LHR")
    departure_local: str  = Field(..., description="Proposed departure (local) — ISO format e.g. 2024-03-15 08:00")
    arrival_local:   Optional[str]  = Field(None, description="Proposed arrival (local)")
    aircraft_type:   Optional[str]  = Field(None, description="IATA aircraft type code, e.g. B777")
    airline:         Optional[str]  = Field(None, description="2-letter IATA airline code")
    block_time:      Optional[int]  = Field(None, description="Block time in minutes (used if arrival not given)")
    hub:             Optional[str]  = Field(None, description="Hub airport for connectivity analysis")

    @field_validator("origin", "destination")
    @classmethod
    def uppercase_iata(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("airline")
    @classmethod
    def uppercase_airline(cls, v: Optional[str]) -> Optional[str]:
        return v.upper().strip() if v else v


class AddFlightResponse(ScheduleResponse):
    feasible:             bool = False
    feasibility_score:    int  = 0
    network_value_score:  int  = 0
    best_window:          List[Dict] = Field(default_factory=list)
    why_not:              Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Simulation — Retime Flight
# ─────────────────────────────────────────────────────────────────────────────

class RetimeFlightRequest(BaseModel):
    flight_number:       str  = Field(..., description="Existing flight number, e.g. EK001")
    new_departure_local: str  = Field(..., description="Proposed new departure local datetime")
    hub:                 Optional[str] = Field(None, description="Hub airport for connectivity delta")


class RetimeFlightResponse(ScheduleResponse):
    feasible:             bool             = False
    feasibility_score:    int              = 0
    network_value_score:  int              = 0
    current_timing:       Optional[Dict]   = None
    proposed_timing:      Optional[Dict]   = None
    delta:                Optional[Dict]   = None
    connectivity_change:  Optional[Dict]   = None


# ─────────────────────────────────────────────────────────────────────────────
# Route / Schedule search
# ─────────────────────────────────────────────────────────────────────────────

class RouteSearchRequest(BaseModel):
    origin:      Optional[str] = None
    destination: Optional[str] = None
    airline:     Optional[str] = None


class FlightRecord(BaseModel):
    """Single flight record for display."""
    flight_number: Optional[str]
    airline:       Optional[str]
    origin:        Optional[str]
    destination:   Optional[str]
    departure_local: Optional[str]
    arrival_local:   Optional[str]
    aircraft_type:   Optional[str]
    block_time:      Optional[int]
    day_of_operation: Optional[int]
