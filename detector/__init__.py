"""
Routine-detection package.

Public surface:
    DetectorConfig   — tunable parameters (dataclass)
    Detector         — protocol both versions satisfy
    ClusterDetector  — v0.2, the default
    SubstringDetector — v0.1 baseline (precision/recall comparison only)
    make_detector(name, config) — factory; name in {"v2","v1", aliases}

The default version is v2. Selecting v1 is explicit — pass make_detector("v1")
or set REVIT_DETECTOR_VERSION=v1 (never a buried constant).
"""
from __future__ import annotations

from .base import Detector, DetectorConfig
from .v1_substring import SubstringDetector
from .v1_5_episode import EpisodeGroupingDetector
from .v2_cluster import ClusterDetector, Instance
from .v3_compound import CompoundDetector

DEFAULT_VERSION = "v2"

_ALIASES = {
    "v3": "v3", "compound": "v3", "v0.3": "v3", "multi-element": "v3", "multielement": "v3",
    "v2": "v2", "cluster": "v2", "v0.2": "v2", "default": "v2",
    "v1.5": "v1.5", "v15": "v1.5", "episode": "v1.5", "episode-grouping": "v1.5",
    "v1": "v1", "substring": "v1", "baseline": "v1", "v0.1": "v1",
}


def make_detector(name: str | None = None, config: DetectorConfig | None = None) -> Detector:
    """Construct a detector by name. Defaults to v2."""
    key = _ALIASES.get((name or DEFAULT_VERSION).lower())
    if key == "v3":
        return CompoundDetector(config)
    if key == "v2":
        return ClusterDetector(config)
    if key == "v1.5":
        return EpisodeGroupingDetector(config)
    if key == "v1":
        return SubstringDetector(config)
    raise ValueError(
        f"Unknown detector '{name}'. Use one of: "
        f"{sorted(set(_ALIASES))}"
    )


__all__ = [
    "Detector",
    "DetectorConfig",
    "ClusterDetector",
    "SubstringDetector",
    "EpisodeGroupingDetector",
    "CompoundDetector",
    "Instance",
    "make_detector",
    "DEFAULT_VERSION",
]
