"""Small deterministic admission rules; external text never grants authority."""
from __future__ import annotations

import copy


def checked_rules(rules):
    if not isinstance(rules, list) or len(rules) > 100:
        raise ValueError("Task rules must be a list with at most 100 items")
    result, seen = [], set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) - {"id", "enabled", "contains"}:
            raise ValueError("Task rule fields are invalid")
        identity = rule.get("id")
        terms = rule.get("contains")
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 128 or identity in seen:
            raise ValueError("Task rule IDs must be unique nonempty text")
        if type(rule.get("enabled")) is not bool:
            raise ValueError("Task rule enabled must be boolean")
        if (not isinstance(terms, list) or not 1 <= len(terms) <= 20
                or any(not isinstance(term, str) or not term.strip() or len(term) > 500 for term in terms)):
            raise ValueError("Each task rule needs one to twenty nonempty text terms")
        result.append({"id": identity, "enabled": rule["enabled"], "contains": [term.strip() for term in terms]})
        seen.add(identity)
    return copy.deepcopy(result)


def evaluate(text, mode, rules):
    matches = [rule["id"] for rule in rules if rule["enabled"]
               and all(term.casefold() in text.casefold() for term in rule["contains"])]
    if mode == "off":
        return "ignored", "Proactive task admission is off", matches
    if not matches:
        return "ignored", "No enabled task rule matched", matches
    if mode == "observe":
        return "observed", "Would accept under the matched rules; observation starts no worker", matches
    return "accepted", "Matched an enabled proactive task rule", matches
