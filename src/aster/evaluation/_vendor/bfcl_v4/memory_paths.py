"""Minimal memory path predicates; no global output directory or environment."""
import re
from pathlib import PurePosixPath

_MEMORY_ID = re.compile(r"memory_(kv|rec_sum|vector)(?:_prereq)?_[A-Za-z0-9_-]+\Z")


def get_directory_structure_by_id(test_id: str) -> str:
    """Match the upstream memory grouping for allowlisted backend IDs only."""
    match = _MEMORY_ID.fullmatch(test_id) if isinstance(test_id, str) else None
    if match is None:
        raise ValueError("Invalid isolated BFCL memory entry ID")
    return str(PurePosixPath("agentic", "memory", match[1]))


def is_first_memory_prereq_entry(test_entry_id):
    return "prereq" in test_entry_id and test_entry_id.endswith("-0")


def is_memory_prereq(test_category):
    return "prereq" in test_category
