"""Evidence-bearing label paths and explicit-type neighborhoods over scoped facts."""
from __future__ import annotations

import math


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ValueError(name + " requires bounded nonempty text")
    return value


def _relation(value):
    _text(value, "relation")
    inverse = value.endswith("^-1")
    base = value[:-3] if inverse else value
    if not base or "^" in base:
        raise ValueError("Relations allow only a terminal ^-1 inverse suffix")
    return base, inverse


def _hop(fact, inverse):
    return {"from": fact["value"] if inverse else fact["entity"],
            "to": fact["entity"] if inverse else fact["value"],
            "relation": fact["relation"], "inverse": inverse, "fact_id": fact["id"],
            "evidence": fact["evidence"], "scope": fact["scope"],
            "entity_type": fact.get("target_type") if inverse else fact.get("entity_type"),
            "target_type": fact.get("entity_type") if inverse else fact.get("target_type")}


def query(memory, *, mode="direct_relation", entity=None, relation=None, path=None, candidate_type=None,
          aggregate=None, scopes=None, at=None, limit=50, scan_limit=2000):
    if mode not in {"direct_relation", "path_query", "typed_neighborhood", "aggregate"}:
        raise ValueError("Unknown relation query mode")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Query limit must be between 1 and 200")
    if entity is not None:
        _text(entity, "entity")
    if mode == "path_query":
        _text(entity, "entity")
        if relation is not None or candidate_type is not None or aggregate is not None:
            raise ValueError("path_query accepts path rather than relation, type or aggregation")
        if not isinstance(path, list) or not 1 <= len(path) <= 32:
            raise ValueError("path_query requires 1-32 explicit relation hops")
        hops = [_relation(item) for item in path]
    else:
        if path is not None:
            raise ValueError("Only path_query accepts multiple hops")
        base, inverse = _relation(relation)
        hops = [(base, inverse)]
        if mode == "typed_neighborhood":
            _text(entity, "entity")
            _text(candidate_type, "candidate_type")
        elif candidate_type is not None:
            raise ValueError("candidate_type applies only to typed_neighborhood")
        if mode == "aggregate":
            if inverse or aggregate not in {"count", "sum"}:
                raise ValueError("Aggregation supports direct_relation count or sum only")
        elif aggregate is not None:
            raise ValueError("Aggregation requires aggregate mode")
    source = memory.candidates(scopes=scopes, at=at, scan_limit=scan_limit)
    facts = source["facts"]
    selected, paths, expanded_truncated = [], [], False
    if mode in {"direct_relation", "aggregate"} and not hops[0][1]:
        selected = [fact for fact in facts if fact["relation"] == hops[0][0] and (entity is None or fact["entity"] == entity)]
    else:
        frontier = [{"entity": entity, "observed_path": [], "type": None}]
        adjacency, relation_edges = {}, {}
        for fact in facts:
            if not isinstance(fact["value"], str):
                continue
            for inverse in (False, True):
                hop = _hop(fact, inverse)
                adjacency.setdefault((fact["relation"], inverse, hop["from"]), []).append(hop)
                relation_edges.setdefault((fact["relation"], inverse), []).append(hop)
        for base, inverse in hops:
            following, layer_truncated = [], False
            for current in frontier:
                edges = (relation_edges.get((base, inverse), []) if current["entity"] is None else
                         adjacency.get((base, inverse, current["entity"]), []))
                for hop in edges:
                    if current["type"] and hop["entity_type"] and current["type"] != hop["entity_type"]:
                        continue  # Explicit incompatible source types never join.
                    if mode == "typed_neighborhood" and hop["target_type"] != candidate_type:
                        continue
                    following.append({"entity": hop["to"], "observed_path": [*current["observed_path"], hop],
                                      "type": hop["target_type"]})
                    if len(following) > scan_limit:
                        layer_truncated = True
                        break
                if layer_truncated:
                    break
            expanded_truncated = expanded_truncated or layer_truncated
            frontier = following[:scan_limit]
            if not frontier:
                break
        paths = [{"entity": item["entity"], "entity_type": item["type"], "observed_path": item["observed_path"]}
                 for item in frontier]
        ids = {hop["fact_id"] for item in paths for hop in item["observed_path"]}
        selected = [fact for fact in facts if fact["id"] in ids]
    selected_ids = {fact["id"] for fact in selected}
    conflicts = [item for item in source["conflicts"] if selected_ids.intersection(item["fact_ids"])]
    aggregation = None
    if mode == "aggregate":
        reason = "candidate_scan_truncated" if source["truncated"] else "conflicting_facts" if conflicts else None
        if aggregate == "sum" and any(type(fact["value"]) not in {int, float} or
                                       not math.isfinite(fact["value"]) for fact in selected):
            reason = reason or "nonnumeric_facts"
        values = {(fact["scope"], fact["agent_id"], fact["project_id"], fact["entity"], fact["relation"], str(fact["value"])):
                  fact["value"] for fact in selected}
        aggregation = {"operation": aggregate, "available": reason is None,
                       "value": (len(values) if aggregate == "count" else sum(values.values())) if reason is None else None,
                       "reason": reason}
    truncated = source["truncated"] or expanded_truncated or len(paths if paths else selected) > limit
    return {"mode": mode, "facts": selected[:limit], "conflicts": conflicts, "paths": paths[:limit],
            "aggregation": aggregation, "truncated": truncated, "scan_truncated": source["truncated"],
            "at": source["at"], "no_data": not selected and not paths, "retrieval": "scoped_sourced_relations",
            "candidate_count": len(facts), "scan_limit": scan_limit,
            "limits": "Raise scan_limit explicitly for larger scopes; truncated results cannot supply complete aggregations",
            "aggregation_basis": "Distinct effective (scope, agent, project, entity, relation, value) assertions; revisions and duplicate evidence are not extra values"}
