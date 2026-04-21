"""
Enhanced Graph Analytics — NetworkX algorithms for airline route network.

Category: Graph Computing Frameworks (awesome-knowledge-graph)
Library:  NetworkX (https://networkx.org) + Python-louvain community detection

Computes and caches:
  • PageRank           — airport importance by network position
  • Betweenness centrality — airports critical for connecting flows (approximate)
  • Degree centrality  — airport connectivity level
  • Community detection — geographic/operational clusters of airports
  • Weighted shortest paths — minimum block-time route O→D
  • Network-level statistics

All results are cached after the first computation and reused for LM tool calls.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger


# ─────────────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────────────

_analytics_cache: Optional[Dict[str, Any]] = None
_analytics_lock = threading.Lock()
_analytics_built = False


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_undirected(G: Any) -> Any:
    """Convert MultiDiGraph to a simple undirected Graph, summing edge weights."""
    import networkx as nx
    UG = nx.Graph()
    UG.add_nodes_from(G.nodes(data=True))
    for u, v, data in G.edges(data=True):
        w = int(data.get("unique_flights", 1)) or 1
        if UG.has_edge(u, v):
            UG[u][v]["weight"] += w
        else:
            UG.add_edge(u, v, weight=w)
    return UG


def _make_block_time_graph(G: Any) -> Any:
    """
    Create a DiGraph where edge weight = minimum block time across airlines.
    Used for shortest-path queries (min travel time).
    """
    import networkx as nx
    DG = nx.DiGraph()
    DG.add_nodes_from(G.nodes(data=True))
    for u, v, data in G.edges(data=True):
        blk = int(data.get("avg_block_min", 0)) or 1
        if DG.has_edge(u, v):
            if blk < DG[u][v]["weight"]:
                DG[u][v]["weight"] = blk
                DG[u][v]["airline"] = data.get("airline", "")
        else:
            DG.add_edge(u, v, weight=blk, airline=data.get("airline", ""))
    return DG


# ─────────────────────────────────────────────────────────────────────────────
# Core computation
# ─────────────────────────────────────────────────────────────────────────────

def _compute_analytics(G: Any) -> Dict[str, Any]:
    """Run all graph analytics. Returns a rich cache dict."""
    import networkx as nx

    logger.info("Computing graph analytics (PageRank + centrality + communities) …")

    # ── Weighted PageRank ─────────────────────────────────────────────────────
    # Weight = total weekly flights on edge (sum over parallel edges)
    try:
        weighted_G = nx.DiGraph()
        for u, v, data in G.edges(data=True):
            w = int(data.get("unique_flights", 1)) or 1
            if weighted_G.has_edge(u, v):
                weighted_G[u][v]["weight"] += w
            else:
                weighted_G.add_edge(u, v, weight=w)
        pagerank: Dict[str, float] = nx.pagerank(weighted_G, weight="weight", max_iter=200)
        pr_max = max(pagerank.values()) if pagerank else 1.0
        pagerank_norm = {ap: round(score / pr_max * 100, 2) for ap, score in pagerank.items()}
    except Exception as e:
        logger.warning(f"PageRank failed: {e}")
        pagerank_norm = {}

    # ── Approximate betweenness centrality ────────────────────────────────────
    # k=300 means sample 300 source nodes — good balance of speed vs accuracy
    try:
        UG = _make_undirected(G)
        betweenness: Dict[str, float] = nx.betweenness_centrality(
            UG, k=min(300, G.number_of_nodes()), weight="weight", normalized=True
        )
        bc_max = max(betweenness.values()) if betweenness else 1.0
        betweenness_norm = {
            ap: round(score / bc_max * 100, 2) for ap, score in betweenness.items()
        }
    except Exception as e:
        logger.warning(f"Betweenness centrality failed: {e}")
        betweenness_norm = {}

    # ── Degree centrality ─────────────────────────────────────────────────────
    try:
        out_degree = dict(weighted_G.out_degree(weight="weight"))
        in_degree  = dict(weighted_G.in_degree(weight="weight"))
    except Exception:
        out_degree = in_degree = {}

    # ── Community detection (greedy modularity on undirected) ─────────────────
    communities: List[List[str]] = []
    airport_community: Dict[str, int] = {}
    try:
        UG_comm = _make_undirected(G)
        comms = list(nx.community.greedy_modularity_communities(UG_comm, weight="weight"))
        # Sort communities by size (largest first)
        comms.sort(key=len, reverse=True)
        for idx, community_set in enumerate(comms):
            members = sorted(community_set)
            communities.append(members)
            for ap in members:
                airport_community[ap] = idx
        logger.info(f"Detected {len(communities)} airport communities")
    except Exception as e:
        logger.warning(f"Community detection failed: {e}")

    # ── Top airports by metric ────────────────────────────────────────────────
    def _top_n(score_dict: Dict[str, float], n: int = 50) -> List[Dict[str, Any]]:
        from app.services.workset_service import AIRPORT_INFO
        return [
            {
                "airport": ap,
                "city": AIRPORT_INFO.get(ap, {}).get("city", ap),
                "score": score,
                "hub_tier": G.nodes[ap].get("hub_tier", "unknown") if ap in G else "unknown",
            }
            for ap, score in sorted(score_dict.items(), key=lambda x: x[1], reverse=True)[:n]
        ]

    # ── Community summary (top airports per community) ────────────────────────
    community_summaries: List[Dict[str, Any]] = []
    for idx, members in enumerate(communities[:15]):
        top_aps = sorted(
            members,
            key=lambda ap: pagerank_norm.get(ap, 0.0),
            reverse=True,
        )[:8]
        from app.services.workset_service import AIRPORT_INFO
        community_summaries.append({
            "community_id": idx,
            "size": len(members),
            "top_airports": [
                {
                    "code": ap,
                    "city": AIRPORT_INFO.get(ap, {}).get("city", ap),
                    "pagerank": pagerank_norm.get(ap, 0.0),
                }
                for ap in top_aps
            ],
        })

    cache = {
        "pagerank":           pagerank_norm,
        "betweenness":        betweenness_norm,
        "out_degree":         out_degree,
        "in_degree":          in_degree,
        "airport_community":  airport_community,
        "communities":        communities,
        "community_summaries": community_summaries,
        "top_by_pagerank":    _top_n(pagerank_norm, 50),
        "top_by_betweenness": _top_n(betweenness_norm, 50),
        "total_airports":     G.number_of_nodes(),
        "total_routes":       G.number_of_edges(),
    }
    logger.info("Graph analytics computed and cached.")
    return cache


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def init_analytics() -> bool:
    """Compute graph analytics. Idempotent — safe to call multiple times."""
    global _analytics_cache, _analytics_built
    if _analytics_built:
        return _analytics_cache is not None
    with _analytics_lock:
        if _analytics_built:
            return _analytics_cache is not None
        from app.knowledge_graph.graph_builder import get_graph
        G = get_graph()
        if G is None:
            logger.warning("Analytics: NetworkX graph not ready.")
            _analytics_built = True
            return False
        try:
            _analytics_cache = _compute_analytics(G)
        except Exception as exc:
            logger.error(f"Analytics computation failed: {exc}")
            _analytics_cache = None
        _analytics_built = True
    return _analytics_cache is not None


def get_analytics_cache() -> Optional[Dict[str, Any]]:
    return _analytics_cache


def is_analytics_ready() -> bool:
    return _analytics_built and _analytics_cache is not None


def rebuild_analytics() -> bool:
    global _analytics_cache, _analytics_built
    with _analytics_lock:
        _analytics_built = False
        _analytics_cache = None
    return init_analytics()


# ─────────────────────────────────────────────────────────────────────────────
# Query functions (called by LM tools)
# ─────────────────────────────────────────────────────────────────────────────

def get_airport_analytics(airport: str) -> Dict[str, Any]:
    """
    Return all graph analytics for a single airport:
    PageRank rank, betweenness rank, community membership, co-community top hubs.
    """
    if not is_analytics_ready():
        return {"airport": airport, "found": False, "note": "Analytics not yet computed."}

    ap = airport.upper().strip()
    cache = _analytics_cache
    from app.services.workset_service import AIRPORT_INFO, AIRLINE_NAMES
    from app.knowledge_graph.graph_builder import get_graph

    G = get_graph()

    pr_score  = cache["pagerank"].get(ap, 0.0)
    bc_score  = cache["betweenness"].get(ap, 0.0)
    comm_id   = cache["airport_community"].get(ap)

    # Rank within network
    pr_rank = sorted(cache["pagerank"].items(), key=lambda x: x[1], reverse=True)
    pr_rank_pos = next((i + 1 for i, (k, _) in enumerate(pr_rank) if k == ap), None)

    # Community peers (same community, sorted by PageRank)
    community_peers: List[Dict[str, Any]] = []
    if comm_id is not None and comm_id < len(cache["communities"]):
        peers = [
            p for p in cache["communities"][comm_id]
            if p != ap
        ]
        peers_sorted = sorted(peers, key=lambda p: cache["pagerank"].get(p, 0.0), reverse=True)[:10]
        community_peers = [
            {
                "code": p,
                "city": AIRPORT_INFO.get(p, {}).get("city", p),
                "pagerank": cache["pagerank"].get(p, 0.0),
                "hub_tier": G.nodes[p].get("hub_tier", "unknown") if G and p in G else "unknown",
            }
            for p in peers_sorted
        ]

    info = AIRPORT_INFO.get(ap, {})
    return {
        "airport":           ap,
        "city":              info.get("city", ap),
        "country":           info.get("country", ""),
        "found":             ap in cache["pagerank"] or ap in cache["betweenness"],
        "pagerank_score":    pr_score,
        "pagerank_rank":     pr_rank_pos,
        "betweenness_score": bc_score,
        "community_id":      comm_id,
        "community_peers":   community_peers,
        "interpretation": (
            f"{ap} has PageRank score {pr_score:.1f}/100 "
            f"(ranked #{pr_rank_pos} in the network by network importance). "
            f"Betweenness centrality {bc_score:.1f}/100 "
            f"({'high' if bc_score > 30 else 'medium' if bc_score > 10 else 'low'} "
            f"flow control importance). "
            + (f"Member of community #{comm_id} with {len(community_peers)} peer airports." if comm_id is not None else "")
        ),
    }


def get_network_analytics_summary() -> Dict[str, Any]:
    """Return the top-level network analytics: top PageRank hubs, top betweenness nodes, community count."""
    if not is_analytics_ready():
        return {"found": False, "note": "Analytics not yet computed."}
    cache = _analytics_cache
    return {
        "found": True,
        "total_airports": cache["total_airports"],
        "total_routes":   cache["total_routes"],
        "community_count": len(cache["communities"]),
        "top_by_pagerank": cache["top_by_pagerank"][:20],
        "top_by_betweenness": cache["top_by_betweenness"][:20],
        "community_summaries": cache["community_summaries"][:10],
    }


def find_shortest_path(origin: str, dest: str) -> Dict[str, Any]:
    """
    Find minimum block-time routing options between two airports.

    Returns up to 3 options: direct (0-stop), best 1-stop, best 2-stop.
    Uses fast O(n) neighbour enumeration — no slow path generators.
    """
    from app.knowledge_graph.graph_builder import get_graph
    from app.services.workset_service import AIRPORT_INFO, AIRLINE_NAMES
    import networkx as nx

    G = get_graph()
    o, d = origin.upper().strip(), dest.upper().strip()

    if G is None:
        return {"origin": o, "destination": d, "found": False, "note": "Graph not ready."}
    if o not in G:
        return {"origin": o, "destination": d, "found": False, "note": f"{o} not in network."}
    if d not in G:
        return {"origin": o, "destination": d, "found": False, "note": f"{d} not in network."}

    BT_G = _make_block_time_graph(G)
    paths: List[Dict[str, Any]] = []

    def _leg(src: str, tgt: str) -> Dict:
        blk = BT_G[src][tgt]["weight"]
        al  = BT_G[src][tgt].get("airline", "")
        return {
            "from": src, "from_city": AIRPORT_INFO.get(src, {}).get("city", src),
            "to":   tgt, "to_city":   AIRPORT_INFO.get(tgt, {}).get("city", tgt),
            "airline": al, "airline_name": AIRLINE_NAMES.get(al, al),
            "block_min": blk,
        }

    # ── 0-stop (direct) ───────────────────────────────────────────────────────
    if BT_G.has_edge(o, d):
        blk = BT_G[o][d]["weight"]
        paths.append({
            "stops": 0, "total_block_min": blk,
            "path": [o, d], "legs": [_leg(o, d)],
        })

    # ── 1-stop: best intermediate via common o→via→d ─────────────────────────
    o_succs = set(BT_G.successors(o))
    d_preds = set(BT_G.predecessors(d))
    via_nodes = (o_succs & d_preds) - {o, d}
    if via_nodes:
        best_cost, best_via = None, None
        for via in via_nodes:
            cost = BT_G[o][via]["weight"] + BT_G[via][d]["weight"]
            if best_cost is None or cost < best_cost:
                best_cost, best_via = cost, via
        if best_via:
            paths.append({
                "stops": 1, "total_block_min": best_cost,
                "path": [o, best_via, d],
                "legs": [_leg(o, best_via), _leg(best_via, d)],
            })

    # ── 2-stop: Dijkstra overall shortest path (may be 2+ hops) ─────────────
    try:
        dijk_path = nx.dijkstra_path(BT_G, o, d, weight="weight")
        hops = len(dijk_path) - 1
        if hops >= 3:  # 2+ stops not already covered
            total_blk = sum(BT_G[dijk_path[i]][dijk_path[i+1]]["weight"]
                            for i in range(hops))
            legs = [_leg(dijk_path[i], dijk_path[i+1]) for i in range(hops)]
            paths.append({
                "stops": hops - 1, "total_block_min": total_blk,
                "path": dijk_path, "legs": legs,
            })
        elif hops == 2 and len(paths) < 2:
            # Dijkstra found a 1-stop we hadn't captured (shouldn't happen, but guard)
            total_blk = sum(BT_G[dijk_path[i]][dijk_path[i+1]]["weight"]
                            for i in range(hops))
            legs = [_leg(dijk_path[i], dijk_path[i+1]) for i in range(hops)]
            paths.append({
                "stops": 1, "total_block_min": total_blk,
                "path": dijk_path, "legs": legs,
            })
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        pass
    except Exception as e:
        logger.debug(f"Dijkstra error {o}→{d}: {e}")

    return {
        "origin": o, "destination": d,
        "origin_city": AIRPORT_INFO.get(o, {}).get("city", o),
        "dest_city":   AIRPORT_INFO.get(d, {}).get("city", d),
        "found": len(paths) > 0,
        "paths": paths,
        "note": f"{len(paths)} routing option(s) found from {o} to {d}."
                if paths else f"No path found from {o} to {d}.",
    }
