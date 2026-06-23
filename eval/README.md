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

## `possibility_matrix.py` — the detector's operating envelope (capability-boundary map)

Where `detection_eval.py` scores one labeled session, the **possibility matrix**
enumerates the *space* of pattern variations a BIM-authoring routine can take and
checks, per scenario, whether v0.2 behaves as **intended** — and where v0.1/v1.5 do
not. 35 scenarios across **seven dimensions**: shape, parameter/family variation,
order & optionality, temporal, noise/corrections, threshold/ranking, and the
stateful **cooldown** axis.

```bash
python eval/possibility_matrix.py            # matrix + baseline comparison + summary
python eval/possibility_matrix.py --real     # + descriptive real-log loop-closure
python eval/possibility_matrix.py --csv-only
```

### Why a hand-built matrix is defensible (read this)
Each scenario carries an **epistemic class**, shown in the `class` column:
- **confirm** (`core`, 23 rows) — *confirmatory*: verifies design intent on canonical
  inputs. "core 23/23" is **not** a generalization claim.
- **falsify** (`boundary`/`out_of_scope`, 12 rows) — *falsifiable predictions* the
  detector could have failed. This is the real evidential content, and it is **not**
  all green:
  - `order_optional_tag` — a routine whose terminal Tag is optional **fragments**
    (similarity 0.75 < θ 0.80). A characterized tolerance gap, not a relabel: no single
    θ absorbs an optional tag (0.75) without merging distinct families (0.66) — a 0.09
    margin. Documented design trade-off (precision over tag-optionality recall).
  - `noise_frequent_spurious` — a frequent *trivial* repeat is a **predicted false
    positive** the detector commits: structural detection has no notion of "meaningful";
    semantic filtering is downstream/future work.

Scenarios were enumerated from the detector's stated design dimensions and expected
outcomes fixed *before* running it (no post-hoc relabeling); the cross-product is
sampled, not exhaustive.

### What it shows
- **v0.2 dominates the baselines across the space** (`possibility_matrix_compare.csv`):
  v0.1 collapses distinct families (`var_different_family`, `var_similar_distinct`),
  v1.5 over-fragments and surfaces noise (`shape_place_only`, `order_param_permutation`).
  The two v0.2 failures are **shared by all three detectors** — open problems, not v0.2
  regressions.
- **out_of_scope limitations are explicit**: multi-element compound routines (partial
  recovery only), param-only edits on existing elements, place-only/2-token shapes.
- **`--real` loop-closure**: runs v0.2 on the real logs and maps each surfaced routine to
  its matrix shape-class — the detector's behaviour on data it did **not** construct is
  consistent with the matrix's core predictions.

### Outputs (`eval/results/`)
- `possibility_matrix.csv` — per-scenario verdict + metrics (degenerate metrics blanked)
- `possibility_matrix_compare.csv` — v0.1 / v1.5 / v0.2 actual-vs-intended per scenario

Deterministic; regression-guarded by `tests/test_possibility_matrix.py` (core must stay
PASS; the two documented limitations must stay FAIL so the matrix can't silently turn green).

## `run_experiment.py` — Pattern Agent quality vs. k examples (§4.4)

Separate harness: scores the LLM Pattern Agent's extracted motif against ground
truth across k example counts. Requires `ANTHROPIC_API_KEY`.
