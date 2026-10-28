"""Deterministic BM25 search over the existing registry, no second tool registry."""

from __future__ import annotations

import math
import re
from collections import Counter

from aster.domain import ToolContext, ToolResult, ToolSpec


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", text.lower().replace("_", " "))


def search_tools(specs: list[ToolSpec], query: str, limit: int = 8) -> list[dict]:
    if not query.strip() or limit < 1:
        return []
    specs = sorted(specs, key=lambda spec: spec.name)
    if query.startswith("select:"):
        names = set(re.split(r"[\s,]+", query[7:].strip()))
        selected = [spec for spec in specs if spec.name in names]
        return [
            {"name": spec.name, "description": spec.description, "parameters": spec.parameters, "score": 1.0}
            for spec in selected[:limit]
        ]
    documents = [Counter(_tokens(spec.name + " " + spec.description)) for spec in specs]
    if not documents:
        return []
    lengths = [sum(doc.values()) for doc in documents]
    average = sum(lengths) / len(lengths) or 1
    terms = set(_tokens(query))
    frequencies = {term: sum(term in doc for doc in documents) for term in terms}
    scores = []
    for spec, doc, length in zip(specs, documents, lengths):
        score = 0.0
        for term in terms:
            freq = doc.get(term, 0)
            inverse = math.log(1 + (len(specs) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            score += inverse * freq * 2.2 / (freq + 1.2 * (0.25 + 0.75 * length / average))
        if query.casefold() == spec.name.casefold():
            score += 10
        if score > 0:
            scores.append((score, spec))
    return [
        {"name": spec.name, "description": spec.description, "parameters": spec.parameters, "score": score}
        for score, spec in sorted(scores, key=lambda pair: (-pair[0], pair[1].name))[:limit]
    ]


class ToolSearchTool:
    spec = ToolSpec(
        "tool_search",
        "Find available tools and load their parameter schemas",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        risk="read",
        replay_safe=True,
    )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        import json

        registry = context.services.get("registry")
        if registry is None:
            return ToolResult(
                "Tool registry service is unavailable", is_error=True, error_category="configuration"
            )
        results = search_tools(registry.specs(), arguments.get("query", ""), arguments.get("limit", 8))
        scope = context.services.get("skill_scope")
        results = [result for result in results if scope is None or scope.check_tool(result["name"])]
        active = context.services.get("active_tools")
        if active is not None:
            active.update(result["name"] for result in results)
        return ToolResult(json.dumps(results, ensure_ascii=False), data={"tools": results})
