"""
Detection evaluation — v0.2 vs. two baselines (v1.5 strong, v0.1 weak).

Produces the comparison the thesis needs, in two modes:

  LABELED (scored)  — a synthetic session with ground-truth routine labels.
    Reports, per detector:
      • routine-level precision/recall/F1 at two cluster-purity thresholds
        (≥0.80 headline, ≥0.50 lenient)
      • clustering-quality: pairwise precision/recall/F1 over instance groupings
        and Adjusted Rand Index (hand-rolled, std-lib only)
    Plus a theta sensitivity sweep for v0.2.

  REAL (descriptive, NOT scored) — runs all three detectors over the real
    %LOCALAPPDATA%\\RevitPersonalization\\logs\\session_*.jsonl and reports only
    unsupervised signals (routines surfaced, cluster sizes, mean intra-cluster
    similarity). No precision/recall — there is no ground truth.

Deterministic: fixed synthetic timestamps, no RNG, stable sorts and documented
tie-breaks throughout. `python eval/detection_eval.py --check-deterministic`
asserts two runs produce byte-identical CSV. No LLM, no Revit calls.

INTERPRETING THE SCORES — read this before citing any number
  The labeled synthetic session is CONSTRUCTED to contain the specific failure
  modes the detectors are designed around (param/family separation, interleaving,
  a 3-vs-4 parameter variant, and below-threshold distractors). v0.2 scoring 1.0
  on it is therefore "by construction" — it confirms v0.2 handles the cases it
  was built for; it is NOT an unbiased measure of real-world accuracy. The value
  of the labeled run is COMPARATIVE: on the same constructed cases, v1.5 splits
  the parameter variant (pairwise/ARI < 1) and v0.1 collapses under the stricter
  purity threshold — that contrast is the point. The generalization evidence is
  the v1.5 result and the descriptive REAL-log run (which shows v1.5
  over-fragmenting on actual sessions), not the v0.2 1.0 on synthetic data.

Usage:
    python eval/detection_eval.py                 # labeled + sweep + real, write CSVs
    python eval/detection_eval.py --mode labeled
    python eval/detection_eval.py --mode real
    python eval/detection_eval.py --no-chart
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from collections import Counter, defaultdict
from math import comb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from detector import ClusterDetector, DetectorConfig, Instance, make_detector
from detector import synthetic as syn
from shared.schemas import CandidateRoutine

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# The three detectors under test, in display order.
DETECTOR_ORDER = [
    ("v0.2-cluster", "v2"),
    ("v1.5-episode", "v1.5"),
    ("v0.1-substring", "v1"),
]

# A single v0.2 instance used only as a *similarity yardstick* for the
# descriptive real-log "mean intra-cluster similarity" column (applied uniformly
# to every detector's groups so the number is comparable).
_SIM = ClusterDetector()


# ── helpers: pull instance membership out of detector output ───────────────────

def _cluster_member_eids(cand: CandidateRoutine) -> list[int]:
    """Place element_ids of a candidate's member instances."""
    eids = []
    for ex in cand.examples:
        if ex.actions and ex.actions[0].action_type == "Place":
            eids.append(ex.actions[0].element_id)
    return eids


def _mean_intra_cluster_similarity(cand: CandidateRoutine) -> float:
    """Descriptive tightness for a surfaced routine, via the v0.2 similarity."""
    insts = [Instance(ex.actions[0].element_id, ex.actions) for ex in cand.examples
             if ex.actions and ex.actions[0].action_type == "Place"]
    n = len(insts)
    if n < 2:
        return 1.0
    total, pairs = 0.0, 0
    for i in range(n):
        for j in range(i + 1, n):
            total += _SIM.similarity(insts[i], insts[j])
            pairs += 1
    return total / pairs if pairs else 1.0


# ── metric (a): routine-level precision/recall/F1 ──────────────────────────────

def routine_level_prf(
    emitted_clusters: list[list[int]],
    labels: dict[int, str],
    min_cluster_size: int,
    purity_threshold: float,
) -> dict:
    """
    Match each emitted cluster to a ground-truth routine by MAJORITY label.

    A cluster is a true positive iff its majority label is a real routine
    (≥ N instances, not noise), that routine is not already claimed (greedy,
    one-to-one), and the cluster's purity (majority share) ≥ purity_threshold.

    Determinism: clusters processed largest-first, ties broken by the sorted
    member-id tuple; majority label ties broken by (count desc, label asc).
    """
    gt_counts = Counter(labels.values())
    target_routines = {
        lab for lab, c in gt_counts.items()
        if c >= min_cluster_size and not lab.startswith("noise")
    }

    order = sorted(
        range(len(emitted_clusters)),
        key=lambda i: (-len(emitted_clusters[i]), tuple(sorted(emitted_clusters[i]))),
    )

    claimed: set[str] = set()
    tp = 0
    for i in order:
        members = emitted_clusters[i]
        if not members:
            continue
        counts = Counter(labels.get(e, f"_unlabeled_{e}") for e in members)
        majority = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        purity = counts[majority] / len(members)
        if majority in target_routines and majority not in claimed and purity >= purity_threshold:
            claimed.add(majority)
            tp += 1

    n_emitted = len(emitted_clusters)
    n_targets = len(target_routines)
    precision = tp / n_emitted if n_emitted else 1.0
    recall = tp / n_targets if n_targets else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "precision": precision, "recall": recall, "f1": f1,
        "tp": tp, "emitted": n_emitted, "targets": n_targets,
    }


# ── metric (b): clustering quality over the instance partition ─────────────────

def _pred_label(pred: dict[int, str], eid: int) -> str:
    """Predicted group for an instance; missing → its own singleton."""
    return pred.get(eid, f"_missing_{eid}")


def pairwise_prf(items: list[int], gt: dict[int, str], pred: dict[int, str]) -> dict:
    """Pairwise precision/recall/F1 over all instance pairs."""
    tp = fp = fn = 0
    n = len(items)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = items[i], items[j]
            same_gt = gt[a] == gt[b]
            same_pred = _pred_label(pred, a) == _pred_label(pred, b)
            if same_gt and same_pred:
                tp += 1
            elif same_pred and not same_gt:
                fp += 1
            elif same_gt and not same_pred:
                fn += 1
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def adjusted_rand_index(items: list[int], gt: dict[int, str], pred: dict[int, str]) -> float:
    """Adjusted Rand Index over the instance partition (hand-rolled, no sklearn)."""
    n = len(items)
    if n < 2:
        return 1.0
    contingency: dict[tuple[str, str], int] = defaultdict(int)
    a_sizes: dict[str, int] = defaultdict(int)
    b_sizes: dict[str, int] = defaultdict(int)
    for k in items:
        g = gt[k]
        p = _pred_label(pred, k)
        contingency[(g, p)] += 1
        a_sizes[g] += 1
        b_sizes[p] += 1

    sum_cells = sum(comb(c, 2) for c in contingency.values())
    sum_a = sum(comb(c, 2) for c in a_sizes.values())
    sum_b = sum(comb(c, 2) for c in b_sizes.values())
    total = comb(n, 2)
    expected = (sum_a * sum_b) / total if total else 0.0
    max_index = (sum_a + sum_b) / 2.0
    denom = max_index - expected
    if denom == 0:
        return 1.0
    return (sum_cells - expected) / denom


# ── labeled run ────────────────────────────────────────────────────────────────

LABELED_FIELDS = [
    "detector",
    "routine_p@0.80", "routine_r@0.80", "routine_f1@0.80",
    "routine_p@0.50", "routine_r@0.50", "routine_f1@0.50",
    "pairwise_p", "pairwise_r", "pairwise_f1", "ari",
    "emitted", "targets",
]


def run_labeled() -> list[dict]:
    records, labels = syn.labeled_session()
    items = sorted(labels.keys())
    rows: list[dict] = []

    for display_name, key in DETECTOR_ORDER:
        det = make_detector(key)
        n = det.config.min_cluster_size

        emitted = [_cluster_member_eids(c) for c in det.detect(records, session_id="eval")]
        pred = det.partition(records)

        r80 = routine_level_prf(emitted, labels, n, 0.80)
        r50 = routine_level_prf(emitted, labels, n, 0.50)
        pw = pairwise_prf(items, labels, pred)
        ari = adjusted_rand_index(items, labels, pred)

        rows.append({
            "detector": display_name,
            "routine_p@0.80": round(r80["precision"], 3),
            "routine_r@0.80": round(r80["recall"], 3),
            "routine_f1@0.80": round(r80["f1"], 3),
            "routine_p@0.50": round(r50["precision"], 3),
            "routine_r@0.50": round(r50["recall"], 3),
            "routine_f1@0.50": round(r50["f1"], 3),
            "pairwise_p": round(pw["precision"], 3),
            "pairwise_r": round(pw["recall"], 3),
            "pairwise_f1": round(pw["f1"], 3),
            "ari": round(ari, 3),
            "emitted": r80["emitted"],
            "targets": r80["targets"],
        })
    return rows


# ── theta sweep (v0.2 only) ────────────────────────────────────────────────────

SWEEP_FIELDS = ["theta", "routine_f1@0.80", "pairwise_f1", "ari", "emitted"]
SWEEP_THETAS = [0.50, 0.60, 0.70, 0.80, 0.90]


def run_theta_sweep() -> list[dict]:
    records, labels = syn.labeled_session()
    items = sorted(labels.keys())
    rows: list[dict] = []
    for theta in SWEEP_THETAS:
        det = ClusterDetector(DetectorConfig(theta=theta))
        n = det.config.min_cluster_size
        emitted = [_cluster_member_eids(c) for c in det.detect(records, session_id="sweep")]
        pred = det.partition(records)
        r80 = routine_level_prf(emitted, labels, n, 0.80)
        pw = pairwise_prf(items, labels, pred)
        ari = adjusted_rand_index(items, labels, pred)
        rows.append({
            "theta": theta,
            "routine_f1@0.80": round(r80["f1"], 3),
            "pairwise_f1": round(pw["f1"], 3),
            "ari": round(ari, 3),
            "emitted": r80["emitted"],
        })
    return rows


# ── real-log descriptive run (NOT scored) ──────────────────────────────────────

REAL_FIELDS = ["detector", "routine_id", "label", "support", "mean_intra_similarity"]


def run_reallogs() -> list[dict]:
    # Real source is now generalBIMlog RevitLogger output (the retired revit_addin/
    # JSONL plugin is no longer read). See mcp_server.generalbimlog_reader.
    from mcp_server.log_reader import load_real_action_records

    records = load_real_action_records()

    rows: list[dict] = []
    if not records:
        return rows

    for display_name, key in DETECTOR_ORDER:
        det = make_detector(key)
        for cand in det.detect(records, session_id="reallogs"):
            rows.append({
                "detector": display_name,
                "routine_id": cand.id,
                "label": cand.label,
                "support": cand.support,
                "mean_intra_similarity": round(_mean_intra_cluster_similarity(cand), 3),
            })
    return rows


# ── output: CSV + console + optional chart ─────────────────────────────────────

def _rows_to_csv_string(fields: list[str], rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in fields})
    return buf.getvalue()


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.write_text(_rows_to_csv_string(fields, rows), encoding="utf-8")


def _print_table(title: str, fields: list[str], rows: list[dict]) -> None:
    widths = {f: max(len(f), *(len(str(r.get(f, ""))) for r in rows)) if rows else len(f)
              for f in fields}
    print(f"\n{title}")
    print("  " + "  ".join(f.ljust(widths[f]) for f in fields))
    print("  " + "  ".join("-" * widths[f] for f in fields))
    for r in rows:
        print("  " + "  ".join(str(r.get(f, "")).ljust(widths[f]) for f in fields))


def _maybe_chart(labeled_rows: list[dict]) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        print("\n(matplotlib not available — skipping chart)")
        return

    names = [r["detector"] for r in labeled_rows]
    x = range(len(names))
    routine_f1 = [r["routine_f1@0.80"] for r in labeled_rows]
    pairwise_f1 = [r["pairwise_f1"] for r in labeled_rows]
    ari = [r["ari"] for r in labeled_rows]

    w = 0.27
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.bar([i - w for i in x], routine_f1, w, label="routine F1 @0.80")
    ax.bar(list(x), pairwise_f1, w, label="pairwise F1")
    ax.bar([i + w for i in x], ari, w, label="ARI")
    ax.set_xticks(list(x)); ax.set_xticklabels(names)
    ax.set_ylim(0, 1.05); ax.set_ylabel("score")
    ax.set_title("Detection quality: v0.2 vs. baselines")
    ax.legend()
    fig.tight_layout()
    out = RESULTS_DIR / "detection_eval_f1.png"
    fig.savefig(out, dpi=120)
    print(f"\nChart -> {out}")


# ── determinism check ──────────────────────────────────────────────────────────

def assert_deterministic() -> None:
    a = _rows_to_csv_string(LABELED_FIELDS, run_labeled())
    b = _rows_to_csv_string(LABELED_FIELDS, run_labeled())
    assert a == b, "labeled CSV differs between runs"
    a2 = _rows_to_csv_string(SWEEP_FIELDS, run_theta_sweep())
    b2 = _rows_to_csv_string(SWEEP_FIELDS, run_theta_sweep())
    assert a2 == b2, "theta-sweep CSV differs between runs"
    print("Determinism check: PASS (two runs identical for labeled + sweep)")


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    # Windows consoles default to cp1252; force UTF-8 so symbols print everywhere.
    # Done here, NOT at import time — reconfiguring stdout on import detaches the
    # underlying buffer, which crashes pytest's output capture at teardown when this
    # module is imported by a test.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="v0.2-vs-baselines detection evaluation")
    ap.add_argument("--mode", choices=["all", "labeled", "real", "sweep"], default="all")
    ap.add_argument("--no-chart", action="store_true")
    ap.add_argument("--check-deterministic", action="store_true")
    args = ap.parse_args()

    if args.check_deterministic:
        assert_deterministic()
        return

    if args.mode in ("all", "labeled"):
        rows = run_labeled()
        _print_table("LABELED - scored (headline purity >=0.80; lenient >=0.50)",
                     LABELED_FIELDS, rows)
        _write_csv(RESULTS_DIR / "detection_eval.csv", LABELED_FIELDS, rows)
        print(f"\nCSV   -> {RESULTS_DIR / 'detection_eval.csv'}")
        if not args.no_chart:
            _maybe_chart(rows)

    if args.mode in ("all", "sweep"):
        sweep = run_theta_sweep()
        _print_table("THETA SWEEP — v0.2 only (sensitivity of F1 to theta)",
                     SWEEP_FIELDS, sweep)
        _write_csv(RESULTS_DIR / "theta_sweep.csv", SWEEP_FIELDS, sweep)
        print(f"\nCSV   -> {RESULTS_DIR / 'theta_sweep.csv'}")

    if args.mode in ("all", "real"):
        real = run_reallogs()
        if real:
            _print_table("REAL LOGS — DESCRIPTIVE ONLY (no ground truth, NOT scored)",
                         REAL_FIELDS, real)
            _write_csv(RESULTS_DIR / "detection_reallogs.csv", REAL_FIELDS, real)
            print(f"\nCSV   -> {RESULTS_DIR / 'detection_reallogs.csv'}")
        else:
            print("\nREAL LOGS — none found at the configured log directory (skipped).")

    # Always self-check determinism at the end of a full run.
    if args.mode == "all":
        assert_deterministic()


if __name__ == "__main__":
    main()
