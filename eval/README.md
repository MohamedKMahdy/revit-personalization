# `eval/` — evaluation harnesses

## `detection_eval.py` — v0.2 vs. baselines (detection gate)

Compares the v0.2 cluster detector against two baselines on the same data:
**v1.5** (the historical episode-grouping — a *strong* baseline) and **v0.1**
(the literal P/S/T/D substring matcher — a *weak* baseline).

```bash
python eval/detection_eval.py                 # labeled + theta sweep + real, write CSVs
python eval/detection_eval.py --mode labeled  # scored synthetic only
python eval/detection_eval.py --mode real     # descriptive real-log only
python eval/detection_eval.py --check-deterministic
```

### Two modes
- **LABELED (scored)** — a synthetic session with ground-truth routine labels.
  Reports routine-level precision/recall/F1 at **≥0.80 (headline)** and **≥0.50
  (lenient)** cluster purity, plus clustering quality: pairwise P/R/F1 and a
  hand-rolled **Adjusted Rand Index** over the instance partition. A theta sweep
  shows v0.2's sensitivity.
- **REAL (descriptive, NOT scored)** — runs all three detectors over the real
  `%LOCALAPPDATA%\RevitPersonalization\logs\session_*.jsonl`. Reports only
  unsupervised signals (routines surfaced, support, mean intra-cluster
  similarity). No precision/recall — there is no ground truth.

### ⚠ How to read the scores (do not misquote)
The labeled synthetic session is **constructed** to contain the exact failure
modes the detectors target (param/family separation, interleaving, a 3-vs-4
parameter variant, below-threshold distractors). **v0.2 scoring 1.0 is therefore
by construction** — it confirms v0.2 handles the cases it was designed for; it is
**not** an unbiased real-world accuracy figure.

The labeled run's value is **comparative**: on the same constructed cases, v1.5
splits the parameter variant (pairwise F1 / ARI < 1) and v0.1 collapses at the
stricter purity threshold (routine F1 1.0 → 0.5). **The generalization evidence
is the v1.5 gap and the descriptive real-log run** (where v1.5 over-fragments),
not the v0.2 synthetic 1.0.

### Outputs (`eval/results/`)
- `detection_eval.csv` — labeled, one row per detector
- `theta_sweep.csv` — v0.2 F1/ARI across theta ∈ {0.5…0.9}
- `detection_reallogs.csv` — descriptive real-log signals
- `detection_eval_f1.png` — optional bar chart (only if matplotlib installed)

Deterministic: fixed timestamps, no RNG, documented tie-breaks;
`--check-deterministic` asserts byte-identical CSV across runs.

## `run_experiment.py` — Pattern Agent quality vs. k examples (§4.4)

Separate harness: scores the LLM Pattern Agent's extracted motif against ground
truth across k example counts. Requires `ANTHROPIC_API_KEY`.
