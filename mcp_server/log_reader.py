"""
Routine detector entry point. Reads real authoring logs as an ActionRecord
stream and runs the selected detector over them.

PRIMARY SOURCE: generalBIMlog RevitLogger output (a ProjectSchema JSON per
project), converted to ActionRecords by `generalbimlog_reader` — see
load_real_action_records().

LEGACY (retired add-in): the old in-repo `revit_addin/` plugin wrote one JSON
object per line to %LOCALAPPDATA%\\RevitPersonalization\\logs\\session_*.jsonl,
with three record types:
  • record_type == "session_start"  — session metadata (SessionInfo)
  • (no record_type field)          — ActionRecord (Place / SetParam / Tag)
  • record_type == "session_end"    — closing marker
That reader (_load_action_records / LOG_DIR) is kept for backward compatibility
but is no longer the default source.

Routine detection follows the episode-grouping approach from:
  Jang & Lee (2023) arXiv:2305.18032 — enhanced BIM logging for reproducibility
  Jang et al. (2023) AEI 57, 102079 — lexicon-based BIM log analysis

Algorithm:
  1. Parse all ActionRecords from all .jsonl files.
  2. Group records by element_id — each element accumulates a sequence of
     [Place → SetParam* → Tag?] actions, forming one "routine episode."
  3. Compute a structural signature per episode:
       (element_category, family_name, tuple of action_type/param_name)
     Two episodes with the same signature are instances of the same routine.
  4. Groups with ≥ 2 episodes become CandidateRoutine objects for the agents.
"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from shared.schemas import ActionRecord, CandidateRoutine, RoutineExample
from detector import Detector, DetectorConfig, make_detector

# Legacy log directory written by the retired revit_addin/ plugin (JSONL).
# Kept for backward compatibility; generalBIMlog is now the default source.
LOG_DIR = Path(os.environ.get(
    "REVIT_PERSONALIZATION_LOG_DIR",
    Path.home() / "AppData" / "Local" / "RevitPersonalization" / "logs",
))

# Synthetic .json files kept for offline testing without Revit
SYNTHETIC_DIR = Path(__file__).parent.parent / "tests" / "synthetic_logs"


# ── JSONL parsing ─────────────────────────────────────────────────────────────

def _iter_records(path: Path) -> Iterator[dict]:
    """Yield raw dicts from a .jsonl file, skipping session_start/end markers."""
    # utf-8-sig handles files written with a UTF-8 BOM (older LogWriter versions)
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("record_type") in ("session_start", "session_end"):
                continue
            # Skip internal error lines written by LogWriter's catch block
            if "_error" in obj:
                continue
            yield obj


def _load_action_records(path: Path) -> list[ActionRecord]:
    """Parse a .jsonl session file into a list of ActionRecord objects."""
    records: list[ActionRecord] = []
    for obj in _iter_records(path):
        try:
            records.append(ActionRecord(**obj))
        except Exception as e:
            # Log bad records to stderr for debugging but keep processing
            import sys
            print(f"  [log_reader] skipping bad record in {path.name}: {e}", file=sys.stderr)
    return records


# ── Old-format .json reader (synthetic test data) ─────────────────────────────

def _load_synthetic_routine(path: Path) -> CandidateRoutine | None:
    """
    Read a synthetic test file written in the CandidateRoutine JSON format.
    These are only used for testing without a live Revit installation.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        examples = [
            RoutineExample(
                example_id=ex["example_id"],
                session_id=ex["session_id"],
                recorded_at=ex["recorded_at"],
                actions=[ActionRecord(**a) for a in ex["actions"]],
            )
            for ex in data.get("examples", [])
        ]
        return CandidateRoutine(
            id=data["id"],
            label=data["label"],
            action_signature=data["action_signature"],
            count=data["count"],
            support=data.get("support", data["count"]),
            confidence=data.get("confidence", 0.0),
            examples=examples,
        )
    except Exception:
        return None


# ── Episode grouping (v1.5) ───────────────────────────────────────────────────
# The historical episode-grouping logic that used to live here now lives in
# detector/v1_5_episode.py as EpisodeGroupingDetector, reachable via
# make_detector("v1.5"). Keeping it in one place means there is a single code
# path for all three detector versions.


# ── Detector selection ────────────────────────────────────────────────────────

def _resolve_detector(
    detector: Detector | str | None,
    config: DetectorConfig | None,
    min_repeats: int | None,
) -> Detector:
    """
    Resolve the detector to use. v0.2 ('v2') is the default; v0.1 ('v1') is
    reachable explicitly via the `detector` argument or the REVIT_DETECTOR_VERSION
    environment variable — never a buried constant.
    """
    if detector is not None and hasattr(detector, "detect"):
        return detector  # already an instance
    name = detector if isinstance(detector, str) else os.environ.get("REVIT_DETECTOR_VERSION")
    cfg = config or DetectorConfig()
    if min_repeats is not None:
        cfg = replace(cfg, min_cluster_size=min_repeats)
    return make_detector(name, cfg)


# ── Public API ────────────────────────────────────────────────────────────────

def load_real_action_records() -> list[ActionRecord]:
    """
    Real authoring logs as one ActionRecord stream.

    PRIMARY: generalBIMlog RevitLogger output, converted by generalbimlog_reader.
    Set GENERALBIMLOG_DIR to point at a custom logs folder; otherwise every
    installed Revit version's eventlog dir is read.

    The legacy revit_addin/ JSONL format (_load_action_records / LOG_DIR) is no
    longer read by default — that add-in is retired.
    """
    from mcp_server.generalbimlog_reader import load_action_records
    return load_action_records()


def list_candidate_routines(
    include_synthetic: bool = True,
    min_repeats: int | None = None,
    *,
    detector: Detector | str | None = None,
    config: DetectorConfig | None = None,
) -> list[CandidateRoutine]:
    """
    Return all detected candidate routines from both real and synthetic logs.

    Real logs (.jsonl from the C# add-in) are run through the selected detector.
    The default is v0.2 (similarity clustering); pass detector="v1" (or set
    REVIT_DETECTOR_VERSION=v1) to use the v0.1 substring baseline for comparison.

    Synthetic .json test files are pre-grouped CandidateRoutines and are loaded
    directly (merged in, deduplicated by id).

    `min_repeats`, when given, overrides the detector's min_cluster_size.
    Results are sorted by support (cluster size / frequency) descending.
    """
    det = _resolve_detector(detector, config, min_repeats)
    routines: dict[str, CandidateRoutine] = {}

    # ── Real logs (generalBIMlog RevitLogger output): gather records, detect once ──
    all_records = load_real_action_records()
    if all_records:
        try:
            for r in det.detect(all_records, session_id="logs"):
                if r.id not in routines or r.support > routines[r.id].support:
                    routines[r.id] = r
        except Exception as e:
            import sys
            print(f"  [log_reader] detector error: {e}", file=sys.stderr)

    # ── Synthetic test data (.json) ──
    if include_synthetic and SYNTHETIC_DIR.exists():
        for json_file in SYNTHETIC_DIR.glob("*.json"):
            try:
                r = _load_synthetic_routine(json_file)
                if r and (r.id not in routines or r.support > routines[r.id].support):
                    routines[r.id] = r
            except Exception:
                pass

    return sorted(routines.values(), key=lambda r: r.support, reverse=True)


def get_routine_examples(routine_id: str, k: int = 5) -> CandidateRoutine | None:
    """Return a CandidateRoutine trimmed to at most k examples, or None if not found."""
    for r in list_candidate_routines():
        if r.id == routine_id:
            return r.model_copy(update={"examples": r.examples[:k]})
    return None
