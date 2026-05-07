"""
RDF Triple Store — airline route ontology + SPARQL query API.

Category: Triple Stores (awesome-knowledge-graph)
Library:  RDFLib (https://rdflib.dev)

Builds an OWL/RDF knowledge graph from the NetworkX property graph:
  Classes:     Airport subtypes (MegaHub … PointToPoint),
               Airline subtypes (FullServiceCarrier, LowCostCarrier …)
  Route model: each O×D×Airline tuple → a typed Route resource
  Properties:  fromAirport, toAirport, operatedBy, memberOf, hubScore, …

Exposes SPARQL SELECT queries for semantic reasoning that SQL cannot express:
  - Which FSC carriers operate DXB→LHR?
  - Which airports are Mega-hubs?
  - Which airlines are in Star Alliance AND operate from DXB?
  - What alliance does EK belong to?
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from loguru import logger

try:
    from rdflib import Graph as RDFGraph, Namespace, RDF, OWL, Literal, URIRef
    from rdflib.namespace import RDFS, XSD
    RDFLIB_AVAILABLE = True
except ImportError:
    RDFLIB_AVAILABLE = False
    logger.warning("rdflib not installed — RDF triple store disabled.")

# ─────────────────────────────────────────────────────────────────────────────
# Namespaces
# ─────────────────────────────────────────────────────────────────────────────

if RDFLIB_AVAILABLE:
    ONT    = Namespace("http://schedai.sabre.com/ontology/airline#")
    AP_NS  = Namespace("http://schedai.sabre.com/data/airport/")
    AL_NS  = Namespace("http://schedai.sabre.com/data/airline/")
    RT_NS  = Namespace("http://schedai.sabre.com/data/route/")
    ALN_NS = Namespace("http://schedai.sabre.com/data/alliance/")
else:
    ONT = AP_NS = AL_NS = RT_NS = ALN_NS = None  # type: ignore

# ─────────────────────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────────────────────

_rdf_graph: Optional[Any] = None
_rdf_lock = threading.Lock()
_rdf_built = False

# ─────────────────────────────────────────────────────────────────────────────
# Static mappings
# ─────────────────────────────────────────────────────────────────────────────

ALLIANCE_MEMBERS: Dict[str, List[str]] = {
    "Star_Alliance": ["LH", "UA", "SQ", "NH", "TK", "CA", "ET", "OS", "TG", "MS", "AI", "SN", "SA"],
    "oneworld":      ["BA", "AA", "QF", "CX", "IB", "AY", "JL", "MH", "RJ", "UL", "AT", "S7"],
    "SkyTeam":       ["DL", "AF", "KL", "KE", "MU", "CI", "GA", "SV", "AM", "AZ", "ME", "OK", "VN"],
    "Gulf_Trio":     ["EK", "EY", "QR"],
}
ALLIANCE_MAP: Dict[str, str] = {
    member: alliance
    for alliance, members in ALLIANCE_MEMBERS.items()
    for member in members
}

_HUB_TIER_CLASS = {
    "Mega-hub":       "MegaHub",
    "Major hub":      "MajorHub",
    "Secondary hub":  "SecondaryHub",
    "Regional hub":   "RegionalHub",
    "Point-to-point": "PointToPoint",
}

_CARRIER_CLASS = {
    "Full-service": "FullServiceCarrier",
    "Low-cost":     "LowCostCarrier",
    "Regional":     "RegionalCarrier",
    "Charter":      "CharterCarrier",
}


# ─────────────────────────────────────────────────────────────────────────────
# Ontology definition
# ─────────────────────────────────────────────────────────────────────────────

def _define_ontology(g: "RDFGraph") -> None:
    """Add OWL class & property definitions to the graph."""
    # ── Classes ──────────────────────────────────────────────────────────────
    for cls in ["Airport", "MegaHub", "MajorHub", "SecondaryHub", "RegionalHub", "PointToPoint",
                "Airline", "FullServiceCarrier", "LowCostCarrier", "RegionalCarrier", "CharterCarrier",
                "Alliance", "Route", "CodeshareRoute"]:
        uri = ONT[cls]
        g.add((uri, RDF.type, OWL.Class))
        g.add((uri, RDFS.label, Literal(cls)))

    # ── Sub-class hierarchies ────────────────────────────────────────────────
    for sub in ["MegaHub", "MajorHub", "SecondaryHub", "RegionalHub", "PointToPoint"]:
        g.add((ONT[sub], RDFS.subClassOf, ONT.Airport))
    for sub in ["FullServiceCarrier", "LowCostCarrier", "RegionalCarrier", "CharterCarrier"]:
        g.add((ONT[sub], RDFS.subClassOf, ONT.Airline))
    # CodeshareRoute is a Route where marketing ≠ operating carrier
    g.add((ONT.CodeshareRoute, RDFS.subClassOf, ONT.Route))

    # ── Object properties ────────────────────────────────────────────────────
    for prop, domain, range_ in [
        ("fromAirport",      "Route",   "Airport"),
        ("toAirport",        "Route",   "Airport"),
        ("operatedBy",       "Route",   "Airline"),   # physical operator
        ("marketedBy",       "Route",   "Airline"),   # marketing/selling carrier
        ("hasRoute",         "Airport", "Route"),
        ("connectsTo",       "Airport", "Airport"),
        ("servesAirport",    "Airline", "Airport"),
        ("memberOf",         "Airline", "Alliance"),
        ("codeSharesWith",   "Airline", "Airline"),   # marketing ↔ operating carrier link
    ]:
        p = ONT[prop]
        g.add((p, RDF.type, OWL.ObjectProperty))
        g.add((p, RDFS.domain, ONT[domain]))
        g.add((p, RDFS.range,  ONT[range_]))

    # ── Datatype properties ──────────────────────────────────────────────────
    for prop, domain, dtype in [
        ("hubScore",           "Airport", XSD.decimal),
        ("weeklyFrequency",    "Airport", XSD.integer),
        ("destinationsServed", "Airport", XSD.integer),
        ("airlinesOperating",  "Airport", XSD.integer),
        ("city",               "Airport", XSD.string),
        ("country",            "Airport", XSD.string),
        ("utcOffset",          "Airport", XSD.string),
        ("carrierType",        "Airline", XSD.string),
        ("allianceName",       "Alliance",XSD.string),
        ("weeklyFlights",      "Route",   XSD.integer),
        ("avgBlockMin",        "Route",   XSD.integer),
        ("isCodeshare",        "Route",   XSD.boolean),
        ("marketingAirline",   "Route",   XSD.string),
        ("operatingAirline",   "Route",   XSD.string),
    ]:
        p = ONT[prop]
        g.add((p, RDF.type,       OWL.DatatypeProperty))
        g.add((p, RDFS.domain,    ONT[domain]))
        g.add((p, RDFS.range,     dtype))


# ─────────────────────────────────────────────────────────────────────────────
# Builder
# ─────────────────────────────────────────────────────────────────────────────

def build_rdf_graph() -> Optional["RDFGraph"]:
    """Build the RDF graph from the in-memory NetworkX KG."""
    if not RDFLIB_AVAILABLE:
        return None

    from app.knowledge_graph.graph_builder import get_graph
    nx_graph = get_graph()
    if nx_graph is None:
        logger.warning("RDF build skipped — NetworkX graph not ready.")
        return None

    try:
        from app.services.workset_service import AIRPORT_INFO, AIRLINE_NAMES, CARRIER_TYPE

        g = RDFGraph()
        g.bind("ont",     ONT)
        g.bind("airport", AP_NS)
        g.bind("airline", AL_NS)
        g.bind("route",   RT_NS)
        g.bind("alliance",ALN_NS)
        g.bind("owl",     OWL)
        g.bind("rdfs",    RDFS)
        _define_ontology(g)

        # ── Airport nodes ─────────────────────────────────────────────────────
        for ap, data in nx_graph.nodes(data=True):
            ap_uri = AP_NS[ap]
            tier   = data.get("hub_tier", "Point-to-point")
            cls    = _HUB_TIER_CLASS.get(tier, "Airport")
            g.add((ap_uri, RDF.type, ONT[cls]))
            g.add((ap_uri, RDF.type, ONT.Airport))
            info = AIRPORT_INFO.get(ap, {})
            if info.get("city"):
                g.add((ap_uri, ONT.city,    Literal(info["city"], datatype=XSD.string)))
            if info.get("country"):
                g.add((ap_uri, ONT.country, Literal(info["country"], datatype=XSD.string)))
            if info.get("utc"):
                g.add((ap_uri, ONT.utcOffset, Literal(info["utc"], datatype=XSD.string)))
            if data.get("hub_score"):
                g.add((ap_uri, ONT.hubScore, Literal(float(data["hub_score"]), datatype=XSD.decimal)))
            if data.get("dest_count"):
                g.add((ap_uri, ONT.destinationsServed, Literal(int(data["dest_count"]), datatype=XSD.integer)))
            if data.get("airline_count"):
                g.add((ap_uri, ONT.airlinesOperating, Literal(int(data["airline_count"]), datatype=XSD.integer)))
            if data.get("out_freq"):
                g.add((ap_uri, ONT.weeklyFrequency, Literal(int(data["out_freq"]), datatype=XSD.integer)))

        # ── Airline nodes ─────────────────────────────────────────────────────
        airlines_seen = {d.get("airline") for _, _, d in nx_graph.edges(data=True) if d.get("airline")}
        for al in airlines_seen:
            al_uri   = AL_NS[al]
            ctype    = CARRIER_TYPE.get(al, "Full-service")
            cls      = _CARRIER_CLASS.get(ctype, "Airline")
            name     = AIRLINE_NAMES.get(al, al)
            alliance = ALLIANCE_MAP.get(al)
            g.add((al_uri, RDF.type,        ONT[cls]))
            g.add((al_uri, RDF.type,        ONT.Airline))
            g.add((al_uri, RDFS.label,      Literal(name, datatype=XSD.string)))
            g.add((al_uri, ONT.carrierType, Literal(ctype, datatype=XSD.string)))
            if alliance:
                aln_uri = ALN_NS[alliance]
                g.add((aln_uri, RDF.type,        ONT.Alliance))
                g.add((aln_uri, ONT.allianceName, Literal(alliance.replace("_", " "), datatype=XSD.string)))
                g.add((al_uri,  ONT.memberOf,     aln_uri))

        # ── Route resources (n-ary: origin + dest + airline) ──────────────────
        airports_served: Dict[str, set] = {}  # airline → set of airports
        codeshare_pairs = nx_graph.graph.get("codeshare_pairs", [])

        for origin, dest, data in nx_graph.edges(data=True):
            al = data.get("airline", "")
            if not al:
                continue
            is_cs     = bool(data.get("is_codeshare", False))
            op_al     = data.get("operating_airline", al)
            mkt_al    = data.get("marketing_airline", al)

            route_uri = RT_NS[f"{origin}_{dest}_{al}"]
            route_cls = ONT.CodeshareRoute if is_cs else ONT.Route
            g.add((route_uri, RDF.type,             route_cls))
            g.add((route_uri, RDF.type,             ONT.Route))
            g.add((route_uri, ONT.fromAirport,      AP_NS[origin]))
            g.add((route_uri, ONT.toAirport,        AP_NS[dest]))
            g.add((route_uri, ONT.operatedBy,       AL_NS[op_al]))
            g.add((route_uri, ONT.marketedBy,       AL_NS[mkt_al]))
            g.add((route_uri, ONT.isCodeshare,      Literal(is_cs, datatype=XSD.boolean)))
            g.add((route_uri, ONT.marketingAirline, Literal(mkt_al, datatype=XSD.string)))
            g.add((route_uri, ONT.operatingAirline, Literal(op_al,  datatype=XSD.string)))
            if data.get("unique_flights"):
                g.add((route_uri, ONT.weeklyFlights, Literal(int(data["unique_flights"]), datatype=XSD.integer)))
            if data.get("avg_block_min"):
                g.add((route_uri, ONT.avgBlockMin, Literal(int(data["avg_block_min"]), datatype=XSD.integer)))
            # Shortcut: airport connects directly to airport
            g.add((AP_NS[origin], ONT.connectsTo, AP_NS[dest]))
            g.add((AP_NS[origin], ONT.hasRoute,   route_uri))
            # Airline serves both airports
            airports_served.setdefault(al, set()).update([origin, dest])
            if op_al != al:
                airports_served.setdefault(op_al, set()).update([origin, dest])

        for al, aps in airports_served.items():
            al_uri = AL_NS[al]
            for ap in aps:
                g.add((al_uri, ONT.servesAirport, AP_NS[ap]))

        # ── Codeshare relationships between airlines ──────────────────────────
        for mkt_al, op_al in codeshare_pairs:
            g.add((AL_NS[mkt_al], ONT.codeSharesWith, AL_NS[op_al]))
            g.add((AL_NS[op_al],  ONT.codeSharesWith, AL_NS[mkt_al]))

        triple_count = len(g)
        logger.info(f"RDF triple store built: {triple_count:,} triples")
        return g

    except Exception as exc:
        logger.error(f"RDF graph build failed: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def init_rdf_store() -> bool:
    global _rdf_graph, _rdf_built
    if _rdf_built:
        return _rdf_graph is not None
    with _rdf_lock:
        if _rdf_built:
            return _rdf_graph is not None
        logger.info("Building RDF triple store …")
        _rdf_graph = build_rdf_graph()
        _rdf_built = True
    return _rdf_graph is not None


def get_rdf_graph() -> Optional[Any]:
    return _rdf_graph


def is_rdf_ready() -> bool:
    return _rdf_built and _rdf_graph is not None


def rebuild_rdf_store() -> bool:
    global _rdf_graph, _rdf_built
    with _rdf_lock:
        _rdf_built = False
        _rdf_graph = None
    return init_rdf_store()


def sparql_query(query: str) -> List[Dict[str, Any]]:
    """
    Execute a SPARQL SELECT query.
    Returns list of dicts {varname: value}. Strings only — no URIRef/Literal wrappers.
    """
    if not is_rdf_ready():
        return [{"error": "RDF triple store not ready."}]
    if not RDFLIB_AVAILABLE:
        return [{"error": "rdflib not installed."}]
    try:
        from rdflib import Literal as _Lit, URIRef as _URI
        results = _rdf_graph.query(query)
        rows: List[Dict[str, Any]] = []
        for row in results:
            d: Dict[str, Any] = {}
            for var in results.vars:
                val = row[var]
                if val is None:
                    d[str(var)] = None
                elif isinstance(val, _Lit):
                    d[str(var)] = val.toPython()
                elif isinstance(val, _URI):
                    # strip to local name
                    s = str(val)
                    d[str(var)] = s.split("/")[-1].split("#")[-1]
                else:
                    d[str(var)] = str(val)
            rows.append(d)
        return rows
    except Exception as exc:
        return [{"error": str(exc), "query_fragment": query[:200]}]


# ─────────────────────────────────────────────────────────────────────────────
# Pre-built semantic queries (used by LM tool & graph analytics)
# ─────────────────────────────────────────────────────────────────────────────

PREFIXES = """
    PREFIX ont: <http://schedai.sabre.com/ontology/airline#>
    PREFIX ap:  <http://schedai.sabre.com/data/airport/>
    PREFIX al:  <http://schedai.sabre.com/data/airline/>
    PREFIX rt:  <http://schedai.sabre.com/data/route/>
    PREFIX aln: <http://schedai.sabre.com/data/alliance/>
"""


def query_airlines_on_route(origin: str, dest: str) -> List[Dict[str, Any]]:
    """Return airlines (code + carrier_type) with direct service origin→dest."""
    return sparql_query(PREFIXES + f"""
        SELECT DISTINCT ?al_code ?ctype WHERE {{
            ?r ont:fromAirport ap:{origin} ;
               ont:toAirport   ap:{dest} ;
               ont:operatedBy  ?al .
            ?al ont:carrierType ?ctype .
            BIND(STRAFTER(STR(?al), "airline/") AS ?al_code)
        }} ORDER BY ?al_code
    """)


def query_airports_by_tier(tier: str, limit: int = 30) -> List[Dict[str, Any]]:
    """Return airports of a given hub tier with their hub score."""
    cls = _HUB_TIER_CLASS.get(tier, "Airport")
    return sparql_query(PREFIXES + f"""
        SELECT ?ap_code ?city ?country ?score ?dests WHERE {{
            ?ap a ont:{cls} .
            OPTIONAL {{ ?ap ont:city               ?city . }}
            OPTIONAL {{ ?ap ont:country            ?country . }}
            OPTIONAL {{ ?ap ont:hubScore           ?score . }}
            OPTIONAL {{ ?ap ont:destinationsServed ?dests . }}
            BIND(STRAFTER(STR(?ap), "airport/") AS ?ap_code)
        }} ORDER BY DESC(?score) LIMIT {limit}
    """)


def query_alliance_carriers(alliance: str) -> List[Dict[str, Any]]:
    """Return airlines in a named alliance (e.g. 'Star Alliance')."""
    aln_key = alliance.replace(" ", "_")
    return sparql_query(PREFIXES + f"""
        SELECT ?al_code ?label WHERE {{
            ?al ont:memberOf aln:{aln_key} .
            OPTIONAL {{ ?al rdfs:label ?label . }}
            BIND(STRAFTER(STR(?al), "airline/") AS ?al_code)
        }} ORDER BY ?al_code
    """)


def query_carriers_at_airport(airport: str, carrier_class: str = "Airline") -> List[Dict[str, Any]]:
    """Return carriers of a specific class operating at an airport."""
    cls = _CARRIER_CLASS.get(carrier_class, "Airline") if carrier_class in _CARRIER_CLASS else carrier_class
    return sparql_query(PREFIXES + f"""
        SELECT DISTINCT ?al_code ?ctype WHERE {{
            ?al a ont:{cls} ;
                ont:servesAirport ap:{airport} ;
                ont:carrierType   ?ctype .
            BIND(STRAFTER(STR(?al), "airline/") AS ?al_code)
        }} ORDER BY ?al_code
    """)


def query_airline_alliance(airline: str) -> List[Dict[str, Any]]:
    """Return the alliance an airline belongs to."""
    return sparql_query(PREFIXES + f"""
        SELECT ?alliance_name WHERE {{
            al:{airline} ont:memberOf ?aln .
            ?aln ont:allianceName ?alliance_name .
        }}
    """)


def query_common_hubs(origin: str, dest: str) -> List[Dict[str, Any]]:
    """Return airports reachable from both origin and dest (potential 1-stop hubs)."""
    return sparql_query(PREFIXES + f"""
        SELECT DISTINCT ?hub_code WHERE {{
            ap:{origin} ont:connectsTo ?hub .
            ap:{dest}   ont:connectsTo ?hub .
            BIND(STRAFTER(STR(?hub), "airport/") AS ?hub_code)
        }} LIMIT 20
    """)


def query_fsc_vs_lcc(origin: str, dest: str) -> Dict[str, List[str]]:
    """Return dict with 'fsc' and 'lcc' airline code lists for a route."""
    rows = query_airlines_on_route(origin, dest)
    fsc = [r["al_code"] for r in rows if "Full" in str(r.get("ctype", ""))]
    lcc = [r["al_code"] for r in rows if "Low" in str(r.get("ctype", ""))]
    return {"fsc": fsc, "lcc": lcc, "all": [r["al_code"] for r in rows]}


def query_codeshare_partners(airline: str) -> List[Dict[str, Any]]:
    """Return airlines that codeshare with the given airline (bidirectional)."""
    return sparql_query(PREFIXES + f"""
        SELECT DISTINCT ?partner_code WHERE {{
            {{
                al:{airline} ont:codeSharesWith ?partner .
            }} UNION {{
                ?partner ont:codeSharesWith al:{airline} .
            }}
            BIND(STRAFTER(STR(?partner), "airline/") AS ?partner_code)
            FILTER(?partner_code != "{airline}")
        }} ORDER BY ?partner_code
    """)


def query_codeshare_routes(airline: str) -> List[Dict[str, Any]]:
    """Return all codeshare routes where this airline is the marketing or operating carrier."""
    return sparql_query(PREFIXES + f"""
        SELECT ?origin ?dest ?mkt ?op WHERE {{
            ?r a ont:CodeshareRoute ;
               ont:fromAirport    ?orig_ap ;
               ont:toAirport      ?dest_ap ;
               ont:marketingAirline ?mkt ;
               ont:operatingAirline ?op .
            BIND(STRAFTER(STR(?orig_ap), "airport/") AS ?origin)
            BIND(STRAFTER(STR(?dest_ap), "airport/") AS ?dest)
            FILTER(?mkt = "{airline}" || ?op = "{airline}")
        }} ORDER BY ?origin ?dest LIMIT 50
    """)
