"""
Entity Enrichment — LM-powered entity classification for the KG Brain.

Derives typed entity nodes (Airport, Carrier, AircraftType) and semantic
relation types from the NetworkX graph. Uses Gemini to enrich entity
classifications (carrier FSC/LCC/ULCC, alliances, hub roles).

Entity types:  Airport | Carrier | AircraftType | Alliance
Relation types: connects | hubs_at | served_by | operated_by | uses_aircraft
                codeshares_with | member_of
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

from loguru import logger

# ── Entity type → visual color (matches kg-viz reference NODE_COLORS) ─────────
ENTITY_COLORS: Dict[str, str] = {
    "Airport":      "#38bdf8",   # sky blue
    "Carrier":      "#f472b6",   # hot pink
    "AircraftType": "#a78bfa",   # violet
    "Alliance":     "#60a5fa",   # blue
    "Route":        "#34d399",   # emerald
    "Unknown":      "#cbd5e1",   # light grey
}

# ── Relation type → visual color (matches kg-viz reference EDGE_COLORS) ───────
RELATION_COLORS: Dict[str, str] = {
    "connects":        "#4ade80",   # lime green  (airport ↔ airport)
    "hubs_at":         "#fb923c",   # orange      (carrier → airport hub)
    "served_by":       "#60a5fa",   # blue        (airport ← carrier)
    "operated_by":     "#f472b6",   # hot pink    (flight ← carrier)
    "uses_aircraft":   "#a78bfa",   # violet      (carrier → aircraft type)
    "codeshares_with": "#fbbf24",   # amber       (carrier ↔ carrier)
    "member_of":       "#e879f9",   # fuchsia     (carrier → alliance)
    "default":         "#94a3b8",   # slate
}

# ── Singleton cache ────────────────────────────────────────────────────────────
_entity_cache: Optional[Dict] = None
_cache_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Internal derivation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _derive_carrier_entities(
    G,
    max_carriers: int = 40,
    max_hubs_per_carrier: int = 3,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Derive Carrier nodes and hubs_at edges from NetworkX graph.
    Returns (carrier_nodes, carrier_edges).
    """
    from app.services.workset_service import AIRLINE_NAMES, CARRIER_TYPE

    carrier_data: Dict[str, Dict[str, Any]] = {}
    carrier_hub_freq: Dict[str, Dict[str, int]] = {}
    carrier_aircraft: Dict[str, Set[str]] = {}

    for _u, _v, data in G.edges(data=True):
        al = data.get("airline", "")
        if not al:
            continue
        freq = int(data.get("unique_flights", 0))
        if al not in carrier_data:
            carrier_data[al] = {"routes": 0, "total_freq": 0, "airports": set()}
            carrier_hub_freq[al] = {}
            carrier_aircraft[al] = set()

        carrier_data[al]["routes"]     += 1
        carrier_data[al]["total_freq"] += freq
        carrier_data[al]["airports"].add(_u)
        carrier_data[al]["airports"].add(_v)
        carrier_hub_freq[al][_u]  = carrier_hub_freq[al].get(_u, 0) + freq
        carrier_hub_freq[al][_v]  = carrier_hub_freq[al].get(_v, 0) + freq
        for ac in data.get("aircraft_types", []):
            if ac:
                carrier_aircraft[al].add(ac)

    sorted_carriers = sorted(
        carrier_data.items(), key=lambda x: x[1]["total_freq"], reverse=True
    )[:max_carriers]

    carrier_nodes: List[Dict] = []
    carrier_edges: List[Dict] = []

    for al, cdata in sorted_carriers:
        node_id = f"C_{al}"
        carrier_nodes.append({
            "id":            node_id,
            "label":         al,
            "name":          AIRLINE_NAMES.get(al, al),
            "entity_type":   "Carrier",
            "carrier_type":  CARRIER_TYPE.get(al, "Full-service"),
            "routes":        cdata["routes"],
            "total_freq":    cdata["total_freq"],
            "airports_count": len(cdata["airports"]),
            "aircraft_types": sorted(carrier_aircraft[al])[:8],
        })

        # hubs_at edges: top N airports by frequency for this carrier
        top_hubs = sorted(
            carrier_hub_freq[al].items(), key=lambda x: x[1], reverse=True
        )[:max_hubs_per_carrier]
        max_freq = max((f for _, f in top_hubs), default=1)
        for airport, freq in top_hubs:
            if airport in G:
                carrier_edges.append({
                    "id":       f"{node_id}--{airport}",
                    "source":   node_id,
                    "target":   airport,
                    "relation": "hubs_at",
                    "weight":   round(0.5 + (freq / max_freq) * 2.5, 2),
                })

    return carrier_nodes, carrier_edges


def _derive_aircraft_entities(
    G,
    carrier_node_ids: Set[str],
    max_aircraft: int = 15,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Derive AircraftType nodes and uses_aircraft edges from NetworkX graph.
    Returns (aircraft_nodes, aircraft_edges).
    Only connects aircraft to carrier nodes already in carrier_node_ids.
    """
    ac_data: Dict[str, Dict[str, Any]] = {}
    ac_carrier_freq: Dict[str, Dict[str, int]] = {}

    for _u, _v, data in G.edges(data=True):
        al = data.get("airline", "")
        freq = int(data.get("unique_flights", 0))
        for ac in data.get("aircraft_types", []):
            if not ac:
                continue
            if ac not in ac_data:
                ac_data[ac] = {"carriers": set(), "routes": 0, "total_freq": 0}
                ac_carrier_freq[ac] = {}
            ac_data[ac]["carriers"].add(al)
            ac_data[ac]["routes"]     += 1
            ac_data[ac]["total_freq"] += freq
            ac_carrier_freq[ac][al] = ac_carrier_freq[ac].get(al, 0) + freq

    sorted_ac = sorted(
        ac_data.items(), key=lambda x: x[1]["total_freq"], reverse=True
    )[:max_aircraft]

    aircraft_nodes: List[Dict] = []
    aircraft_edges: List[Dict] = []

    for ac_type, adata in sorted_ac:
        node_id = f"AC_{ac_type}"
        aircraft_nodes.append({
            "id":          node_id,
            "label":       ac_type,
            "entity_type": "AircraftType",
            "operators":   len(adata["carriers"]),
            "routes":      adata["routes"],
            "total_freq":  adata["total_freq"],
        })

        # uses_aircraft edges: top 2 carrier nodes for this aircraft
        top_carriers = sorted(
            ac_carrier_freq[ac_type].items(), key=lambda x: x[1], reverse=True
        )[:2]
        for al, _freq in top_carriers:
            c_id = f"C_{al}"
            if c_id in carrier_node_ids:
                aircraft_edges.append({
                    "id":       f"{node_id}--{c_id}",
                    "source":   c_id,
                    "target":   node_id,
                    "relation": "uses_aircraft",
                    "weight":   1.0,
                })

    return aircraft_nodes, aircraft_edges


def _lm_enrich_carriers(carrier_nodes: List[Dict]) -> Dict[str, Any]:
    """
    Use Gemini to classify carrier types (FSC/LCC/ULCC/Regional/Cargo)
    and identify alliances. Degrades gracefully to empty dict on any error.
    """
    from app.ai.vertex_client import generate_content, extract_text, VERTEX_AVAILABLE

    if not VERTEX_AVAILABLE:
        return {}

    # Build concise carrier summary for LLM
    lines = []
    for c in carrier_nodes[:25]:
        lines.append(
            f"- {c['label']} ({c['name']}): {c['routes']} routes, "
            f"{c['airports_count']} airports, "
            f"fleet=[{','.join((c.get('aircraft_types') or [])[:3])}]"
        )

    prompt = (
        "You are an airline industry expert. Classify these airlines.\n"
        "Airlines:\n" + "\n".join(lines) + "\n\n"
        "Return ONLY valid JSON with this exact structure (no markdown):\n"
        '{"carriers":{"<IATA_code>":{"type":"FSC|LCC|ULCC|Regional|Cargo|Charter",'
        '"alliance":"Star|SkyTeam|Oneworld|None","description":"3-4 word label"}}}'
    )

    try:
        resp = generate_content(
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            temperature=0.0,
        )
        text = extract_text(resp)
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as exc:
        logger.warning(f"LM carrier enrichment failed: {exc}")
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def build_entity_taxonomy(G) -> Dict[str, Any]:
    """
    Build the full entity taxonomy from the NetworkX graph.
    Results are cached after the first call.

    Returns:
        carrier_nodes  : list of Carrier node dicts
        aircraft_nodes : list of AircraftType node dicts
        carrier_edges  : list of hubs_at edge dicts
        aircraft_edges : list of uses_aircraft edge dicts
        lm_enrichment  : Gemini-enriched carrier metadata (may be {})
        entity_colors  : color map by entity_type
        relation_colors: color map by relation type
    """
    global _entity_cache

    if _entity_cache is not None:
        return _entity_cache

    with _cache_lock:
        if _entity_cache is not None:
            return _entity_cache

        logger.info("Building KG entity taxonomy…")

        carrier_nodes, carrier_edges = _derive_carrier_entities(G)
        carrier_node_ids = {c["id"] for c in carrier_nodes}

        aircraft_nodes, aircraft_edges = _derive_aircraft_entities(
            G, carrier_node_ids
        )

        # LM enrichment (non-blocking fallback)
        lm_data: Dict[str, Any] = {}
        try:
            lm_data = _lm_enrich_carriers(carrier_nodes)
        except Exception as exc:
            logger.warning(f"LM enrichment skipped: {exc}")

        # Apply LM enrichment to carrier nodes in-place
        lm_carriers = lm_data.get("carriers", {})
        for cn in carrier_nodes:
            code = cn["label"]
            if code in lm_carriers:
                info = lm_carriers[code]
                cn["carrier_subtype"] = info.get("type", cn.get("carrier_type", ""))
                cn["alliance"]        = info.get("alliance", "None")
                cn["description"]     = info.get("description", "")

        _entity_cache = {
            "carrier_nodes":   carrier_nodes,
            "aircraft_nodes":  aircraft_nodes,
            "carrier_edges":   carrier_edges,
            "aircraft_edges":  aircraft_edges,
            "lm_enrichment":   lm_data,
            "entity_colors":   ENTITY_COLORS,
            "relation_colors": RELATION_COLORS,
        }

        logger.info(
            f"Entity taxonomy ready: {len(carrier_nodes)} carriers, "
            f"{len(aircraft_nodes)} aircraft types, "
            f"{len(carrier_edges) + len(aircraft_edges)} typed edges"
        )
        return _entity_cache


def invalidate_cache() -> None:
    """Invalidate entity cache (call after graph rebuild)."""
    global _entity_cache
    with _cache_lock:
        _entity_cache = None
