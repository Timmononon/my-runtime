"""Aster reverses wire aliases before scoring, so names need no provider rewrite."""
from dataclasses import dataclass


@dataclass(frozen=True)
class _CanonicalNames:
    underscore_to_dot: bool = False


# This replaces the upstream provider registry, whose imports can load model SDKs.
# All argument and function matching remains in the unchanged official checker.
MODEL_CONFIG_MAPPING = {"aster": _CanonicalNames()}
