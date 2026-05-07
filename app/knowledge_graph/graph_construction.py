"""
Knowledge Graph Construction Pipeline — unified build from DuckDB schedule data.

Category: Graph Construction (awesome-knowledge-graph)
Inspired by: Morph-KGC / Ontop R2RML-style pipeline concept

Orchestrates the multi-layer KG build in the correct dependency order:
  DuckDB flights → NetworkX (property graph)
                 → RDFLib (OWL/RDF triples + SPARQL)
                 → Kuzu (embeddable graph database + Cypher)
                 → GraphAnalytics (PageRank + centrality + communities)

All layers derive from the same DuckDB source of truth, ensuring consistency.
Call build_all() from startup; rebuild_all() after re-ingestion.
"""

from __future__ import annotations

import time
from typing import Dict, Any

from loguru import logger


# ─────────────────────────────────────────────────────────────────────────────
# Build pipeline
# ─────────────────────────────────────────────────────────────────────────────

def build_all(force_rebuild: bool = False) -> Dict[str, Any]:
    """
    Build all KG layers in dependency order.

    Returns a summary dict with the status of each layer:
      {
        "networkx": {"ok": True, "time_s": 1.4},
        "rdf":      {"ok": True, "time_s": 8.3},
        "kuzu":     {"ok": True, "time_s": 12.1},
        "analytics": {"ok": True, "time_s": 45.2},
      }
    """
    summary: Dict[str, Any] = {}
    t0_total = time.time()

    # ── Layer 1: NetworkX property graph ──────────────────────────────────────
    t0 = time.time()
    try:
        from app.knowledge_graph.graph_builder import init_graph, rebuild_graph, is_ready as nx_ready
        if force_rebuild:
            ok = rebuild_graph()
        else:
            ok = init_graph() if not nx_ready() else True
        elapsed = round(time.time() - t0, 2)
        summary["networkx"] = {"ok": ok, "time_s": elapsed}
        if ok:
            from app.knowledge_graph.graph_builder import get_graph
            G = get_graph()
            summary["networkx"]["airports"] = G.number_of_nodes() if G else 0
            summary["networkx"]["routes"]   = G.number_of_edges() if G else 0
            logger.info(f"KG layer [NetworkX] ready in {elapsed}s — "
                        f"{summary['networkx']['airports']} airports, "
                        f"{summary['networkx']['routes']} routes")
        else:
            logger.warning("KG layer [NetworkX] failed — dependent layers will be skipped.")
            return summary
    except Exception as exc:
        summary["networkx"] = {"ok": False, "error": str(exc)}
        logger.error(f"KG layer [NetworkX] exception: {exc}")
        return summary

    # ── Layer 1.5: Entity enrichment (LM-powered) — run before RDF/Kuzu ───────
    # Eagerly builds the entity taxonomy (carrier + airport LLM enrichment) and
    # writes the airport strategic_role / description back onto NetworkX nodes so
    # that the RDF and Kuzu layers automatically pick up the enriched attributes.
    t0 = time.time()
    try:
        from app.knowledge_graph.entity_enrichment import build_entity_taxonomy
        G = get_graph()
        if G is not None:
            taxonomy = build_entity_taxonomy(G)
            ap_enrichment = taxonomy.get("airport_enrichment", {}).get("airports", {})
            for ap_code, ap_data in ap_enrichment.items():
                if ap_code in G.nodes:
                    G.nodes[ap_code]["strategic_role"] = ap_data.get("strategic_role", "")
                    G.nodes[ap_code]["description"]    = ap_data.get("description", "")
            elapsed = round(time.time() - t0, 2)
            n_enriched = len(ap_enrichment)
            n_carriers = len(taxonomy.get("carrier_nodes", []))
            summary["entity_enrichment"] = {
                "ok": True, "time_s": elapsed,
                "airports_enriched": n_enriched,
                "carriers_enriched": n_carriers,
            }
            logger.info(
                f"KG layer [EntityEnrichment] ready in {elapsed}s — "
                f"{n_carriers} carriers, {n_enriched} airports LM-enriched"
            )
        else:
            summary["entity_enrichment"] = {"ok": False, "error": "NetworkX graph not available"}
    except Exception as exc:
        summary["entity_enrichment"] = {"ok": False, "error": str(exc)}
        logger.warning(f"KG layer [EntityEnrichment] exception: {exc}")

    # ── Layer 2: RDFLib triple store ──────────────────────────────────────────
    t0 = time.time()
    try:
        from app.knowledge_graph.rdf_store import (
            init_rdf_store, rebuild_rdf_store, is_rdf_ready, get_rdf_graph
        )
        if force_rebuild:
            ok = rebuild_rdf_store()
        else:
            ok = init_rdf_store() if not is_rdf_ready() else True
        elapsed = round(time.time() - t0, 2)
        g = get_rdf_graph()
        summary["rdf"] = {
            "ok": ok, "time_s": elapsed,
            "triples": len(g) if g else 0,
        }
        if ok:
            logger.info(f"KG layer [RDFLib] ready in {elapsed}s — {summary['rdf']['triples']:,} triples")
        else:
            logger.warning("KG layer [RDFLib] build failed.")
    except Exception as exc:
        summary["rdf"] = {"ok": False, "error": str(exc)}
        logger.warning(f"KG layer [RDFLib] exception: {exc}")

    # ── Layer 3: Kuzu graph database ──────────────────────────────────────────
    t0 = time.time()
    try:
        from app.knowledge_graph.kuzu_store import (
            init_kuzu, rebuild_kuzu, is_kuzu_ready
        )
        if force_rebuild:
            ok = rebuild_kuzu()
        else:
            ok = init_kuzu() if not is_kuzu_ready() else True
        elapsed = round(time.time() - t0, 2)
        summary["kuzu"] = {"ok": ok, "time_s": elapsed}
        if ok:
            logger.info(f"KG layer [Kuzu] ready in {elapsed}s")
        else:
            logger.warning("KG layer [Kuzu] build failed.")
    except Exception as exc:
        summary["kuzu"] = {"ok": False, "error": str(exc)}
        logger.warning(f"KG layer [Kuzu] exception: {exc}")

    # ── Layer 4: Graph analytics (PageRank + centrality + communities) ─────────
    t0 = time.time()
    try:
        from app.knowledge_graph.graph_analytics import (
            init_analytics, rebuild_analytics, is_analytics_ready
        )
        if force_rebuild:
            ok = rebuild_analytics()
        else:
            ok = init_analytics() if not is_analytics_ready() else True
        elapsed = round(time.time() - t0, 2)
        summary["analytics"] = {"ok": ok, "time_s": elapsed}
        if ok:
            logger.info(f"KG layer [Analytics] ready in {elapsed}s")
        else:
            logger.warning("KG layer [Analytics] computation failed.")
    except Exception as exc:
        summary["analytics"] = {"ok": False, "error": str(exc)}
        logger.warning(f"KG layer [Analytics] exception: {exc}")

    total_elapsed = round(time.time() - t0_total, 2)
    summary["total_time_s"] = total_elapsed
    layers_ok = sum(1 for v in summary.values() if isinstance(v, dict) and v.get("ok"))
    logger.info(
        f"Knowledge graph construction complete in {total_elapsed}s — "
        f"{layers_ok}/4 layers built successfully."
    )
    return summary


def rebuild_all() -> Dict[str, Any]:
    """Force full rebuild of all KG layers (use after re-ingestion)."""
    logger.info("Rebuilding all knowledge graph layers …")
    return build_all(force_rebuild=True)


def get_build_status() -> Dict[str, Any]:
    """Return the current readiness status of all KG layers."""
    from app.knowledge_graph.graph_builder import is_ready as nx_ready, get_graph
    from app.knowledge_graph.rdf_store import is_rdf_ready, get_rdf_graph
    from app.knowledge_graph.kuzu_store import is_kuzu_ready
    from app.knowledge_graph.graph_analytics import is_analytics_ready, get_analytics_cache
    from app.knowledge_graph.entity_enrichment import _entity_cache

    G = get_graph()
    rdf_g = get_rdf_graph()
    analytics = get_analytics_cache()
    taxonomy  = _entity_cache or {}
    return {
        "networkx": {
            "ready":    nx_ready(),
            "airports": G.number_of_nodes() if G else 0,
            "routes":   G.number_of_edges() if G else 0,
        },
        "entity_enrichment": {
            "ready":             taxonomy != {},
            "carriers_enriched": len(taxonomy.get("carrier_nodes", [])),
            "airports_enriched": len(taxonomy.get("airport_enrichment", {}).get("airports", {})),
        },
        "rdf": {
            "ready":   is_rdf_ready(),
            "triples": len(rdf_g) if rdf_g else 0,
        },
        "kuzu": {
            "ready": is_kuzu_ready(),
        },
        "analytics": {
            "ready":      is_analytics_ready(),
            "communities": len(analytics["communities"]) if analytics else 0,
        },
    }
