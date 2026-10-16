"""Version-pinned Graphify bridge executed only in its external tool environment."""

from __future__ import annotations

import ctypes
import errno
import importlib.metadata
import json
import os
from pathlib import Path
import resource
import sys

VERSION = "0.9.91"


def deny_network() -> None:
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(0x7FFF0000)
    if not ctx:
        raise RuntimeError("Network guard initialization failed.")
    try:
        for name in ("socket", "connect", "bind", "listen", "accept", "accept4", "io_uring_setup"):
            syscall = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
            if syscall < 0 or lib.seccomp_rule_add(ctx, 0x00050000 | errno.EPERM, syscall, 0):
                raise RuntimeError(f"Network guard rule failed: {name}")
        if lib.seccomp_load(ctx):
            raise RuntimeError("Network guard installation failed.")
    finally:
        lib.seccomp_release(ctx)


def node_view(graph, node_id: str) -> dict:
    data = graph.nodes[node_id]
    return {
        "id": node_id, "label": data.get("label"),
        "kind": data.get("type"), "source_file": data.get("source_file"),
        "source_location": data.get("source_location"),
    }


def edge_view(source: str, target: str, data: dict) -> dict:
    src, tgt = data.get("_src", source), data.get("_tgt", target)
    if {src, tgt} != {source, target}:
        raise ValueError("Invalid stored edge orientation.")
    return {
        "source": src, "target": tgt, "direction": "source_to_target",
        "relation": data.get("relation"), "confidence": data.get("confidence"),
        "source_file": data.get("source_file"), "source_location": data.get("source_location"),
    }


def query(graph, arguments: dict) -> dict:
    from graphify import serve
    from graphify.affected import affected_nodes
    from graphify.build import edge_datas

    operation = arguments["operation"]
    result = {"outcome": "OK", "nodes": [], "relationships": [], "warnings": []}
    if operation == "stats":
        result["stats"] = {"nodes": graph.number_of_nodes(), "relationships": graph.number_of_edges()}
        return result

    def resolve(value: str) -> list[str]:
        node, refusal = serve._resolve_single_node(graph, value)
        if node is not None:
            return [node]
        if not serve._is_ambiguity_refusal(refusal):
            return []
        for tier in serve._find_node_tiers(graph, value):
            if tier:
                return sorted(set(tier))
        raise ValueError("Native ambiguous resolution omitted candidates.")

    if operation == "context":
        terms = serve._query_terms(arguments["query"])
        scores = serve._score_query(graph, terms, collect_per_term_seeds=True)
        seeds = serve._pick_seeds(scores.ranked, G=graph)
        if not seeds:
            result["outcome"] = "NOT_FOUND"
            return result
        traversal = serve._traversal_view(graph)
        nodes, pairs = serve._bfs(traversal, seeds, arguments["depth"])
        ordered_nodes = seeds + sorted(set(nodes) - set(seeds))
        raw_edges = []
        for source, target in pairs:
            for data in edge_datas(traversal, source, target):
                raw_edges.append(edge_view(source, target, data))
        result["warnings"].append("Lexical seeds and structural proximity are not runtime or relevance guarantees.")
    else:
        hits = resolve(arguments["query"])
        if not hits:
            result["outcome"] = "NOT_FOUND"
            return result
        if len(hits) > 1:
            result["outcome"] = "AMBIGUOUS"
            ordered_nodes, raw_edges = hits, []
        elif operation == "lookup":
            ordered_nodes, raw_edges = hits, []
        elif operation == "path":
            targets = resolve(arguments["target"])
            if not targets:
                result["outcome"] = "NOT_FOUND"
                return result
            if len(targets) != 1:
                result["outcome"] = "AMBIGUOUS"
                ordered_nodes, raw_edges = targets, []
            elif hits[0] == targets[0]:
                ordered_nodes, raw_edges = hits, []
                result["warnings"].append("Both endpoints identify the same node.")
            else:
                text = serve._shortest_path_text(
                    graph, {
                        "source": hits[0], "target": targets[0],
                        "max_hops": min(arguments["depth"], arguments["max_nodes"] - 1, arguments["max_edges"]),
                    },
                )
                if text.startswith("No "):
                    result["outcome"] = "INCONCLUSIVE"
                elif text.startswith("Path exceeds max_hops="):
                    result["outcome"] = "TRUNCATED"
                elif not text.startswith("Shortest path ("):
                    raise ValueError("Unrecognized native path outcome.")
                result["native_text"] = text
                result["warnings"].append(
                    "Generic relationship path, NOT a call chain. Native path interior is text; "
                    "structured counts below cover endpoints only. No path is not proof of no runtime relationship."
                )
                ordered_nodes, raw_edges = hits + targets, []
        elif operation in {"impact", "tests"}:
            seed = hits[0]
            affected = affected_nodes(graph, seed, relations=arguments["relations"], depth=arguments["depth"])
            if operation == "tests":
                affected = [
                    hit for hit in affected
                    if any(part in {"test", "tests", "__tests__"} or part.startswith("test_")
                           or part.endswith(("_test.py", ".test.js", ".spec.ts"))
                           for part in str(graph.nodes[hit.node_id].get("source_file") or "").split("/"))
                ]
            ordered_nodes = [seed] + [hit.node_id for hit in affected]
            raw_edges = []
            result["impact"] = [
                {"id": hit.node_id, "depth": hit.depth, "relation": hit.via_relation,
                 "source_file": hit.via_file, "source_location": hit.via_location}
                for hit in affected[:min(arguments["max_nodes"] - 1, arguments["max_edges"])]
            ]
            result["omitted_impact"] = len(affected) - len(result["impact"])
            result["warnings"].append(
                "Likely reverse-impact/test candidates only, not complete coverage or runtime impact."
            )
        else:
            seed = hits[0]
            raw_edges = []
            for source, target, data in graph.edges(data=True):
                edge = edge_view(source, target, data)
                direction = arguments["direction"]
                if (direction == "incoming" and edge["target"] != seed
                    or direction == "outgoing" and edge["source"] != seed
                    or direction == "both" and seed not in {edge["source"], edge["target"]}):
                    continue
                if arguments["relations"] and edge["relation"] not in arguments["relations"]:
                    continue
                raw_edges.append(edge)
            raw_edges.sort(key=lambda e: (e["source"], e["target"], str(e["relation"])))
            ordered_nodes = [seed] + sorted(
                {endpoint for edge in raw_edges for endpoint in (edge["source"], edge["target"])} - {seed}
            )
    # Preserve seed order, eliminate duplicate candidates, and only return edges with visible endpoints.
    ordered_nodes = list(dict.fromkeys(ordered_nodes))
    kept = ordered_nodes[:arguments["max_nodes"]]
    visible = set(kept)
    candidate_edges = [
        edge for edge in raw_edges if edge["source"] in visible and edge["target"] in visible
    ]
    result["nodes"] = [node_view(graph, node_id) for node_id in kept]
    result["relationships"] = candidate_edges[:arguments["max_edges"]]
    if "impact" in result:
        previous = len(result["impact"])
        result["impact"] = [hit for hit in result["impact"] if hit["id"] in visible]
        result["omitted_impact"] += previous - len(result["impact"])
    result["omitted_nodes"] = len(ordered_nodes) - len(kept)
    result["omitted_relationships"] = len(raw_edges) - len(result["relationships"])
    return result


def main() -> None:
    # Isolation precedes Graphify imports, including imports with optional network integrations.
    deny_network()
    resource.setrlimit(resource.RLIMIT_AS, (1_500_000_000, 1_500_000_000))
    resource.setrlimit(resource.RLIMIT_CPU, (90, 90))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
    if importlib.metadata.version("graphifyy") != VERSION:
        raise ValueError("Graphify runtime version is incompatible.")
    request = json.loads(sys.stdin.buffer.read(8193))
    if request["action"] == "build":
        from graphify.__main__ import main as graphify_main
        sys.argv = ["graphify", "extract", request["source"], "--code-only", "--max-workers", "2"]
        graphify_main()
        return
    from graphify.serve import _read_graph
    result = query(_read_graph(request["graph"]), request["arguments"])
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
