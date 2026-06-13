# `detector/` — routine detection gate

Detects repeated "Custom Element Instantiation" routines in the BIM action log
and emits `CandidateRoutine` objects for the downstream Pattern Agent. This is a
**detection gate only** — no Revit calls, no model writes, deterministic.

Two versions live behind one interface:

| Version | Class | Role |
|---|---|---|
| **v0.2** (default) | `ClusterDetector` | similarity clustering — tokenize → segment → featurize → cluster → threshold → cooldown |
| **v0.1** (baseline) | `SubstringDetector` | naive char-shape (P/S/T/D) substring matcher, kept **only** for the precision/recall comparison |

## Public interface

```python
from detector import make_detector, DetectorConfig, Detector

det: Detector = make_detector("v2", DetectorConfig())   # default; "v1" for baseline

candidates = det.detect(records, session_id="logs")
#   records:    list[ActionRecord]   (parsed from the C# logger's JSONL)
#   returns:    list[CandidateRoutine]  newly surfaced this call
```

`ClusterDetector` also exposes, for tests and the streaming UI:
`segment(records) -> list[Instance]`, `cluster(instances) -> list[list[Instance]]`,
`active_candidates()` (includes clusters grown while in cooldown), and `reset()`.

### Selecting the detector in the MCP server

`mcp_server.log_reader.list_candidate_routines()` uses **v0.2 by default**. Select
the v0.1 baseline explicitly — never via a buried constant:

```python
list_candidate_routines(detector="v1")        # argument
# or
REVIT_DETECTOR_VERSION=v1                      # environment variable
```

## Parameters (`DetectorConfig`)

| Field | Default | Meaning |
|---|---|---|
| `min_cluster_size` (N) | `3` | min members for a cluster to emit a candidate |
| `theta` | `0.80` | similarity threshold for grouping |
| `cooldown_minutes` (T) | `10.0` | suppress re-emitting the same signature within T (by data time) |
| `min_instance_tokens` | `3` | discard instances shorter than this |
| `idle_gap_minutes` | `5.0` | a larger gap closes open instances and starts a new session |
| `w_set` | `0.6` | weight on feature-set (Jaccard) similarity |
| `w_seq` | `0.4` | weight on sequence (1 − normalized edit distance) similarity |

Similarity between two instances:

```
sim = w_set · Jaccard(featureSetA, featureSetB)
    + w_seq · (1 − normalizedEditDistance(tokenSeqA, tokenSeqB))
```

- **feature set** = `{fam:<family>, param:<name>…, tag:<family>}`
- **token sequence** = ordered `"{action}:{key}"` tokens

## The `key` field

The log schema (`schema_version: "2.0"`) does **not** store a literal `key`, but
it is fully derivable from fields the C# logger already emits, so **no logger
change is required**. Derivation (`_common.derive_key`):

| action_type | key |
|---|---|
| `Place` | `family_name` (family part, before `:`) |
| `SetParam` | `param_name` |
| `Tag` | `tag_family_name` |

## Two ranking axes on every candidate

`CandidateRoutine` carries both signals — rank by either or both:

- **`support`** (= `count`) — cluster size, the **frequency** signal.
- **`confidence`** — the **tightness** of the grouping, but the meaning differs
  by detector and the two are **not comparable**:
  - v0.2: mean pairwise intra-cluster similarity (0–1).
  - v0.1: legacy frequency-based `min(1, count/5)`.

Because the confidence axes differ, the **v0.1-vs-v0.2 evaluation compares
detection precision and recall against the labeled session**, not the confidence
value.

## Testing

```bash
pytest tests/test_detector.py -v
```

Covers the five required scenarios — param/family separation, interleaved
repeats, 3-vs-4 param variation merging, idle-gap session splitting, cooldown
suppression+growth — plus baseline-contrast checks. Synthetic logs are generated
in `detector/synthetic.py` (run `python -m detector.synthetic` to dump a sample
session as JSONL).
