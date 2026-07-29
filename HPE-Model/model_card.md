# Model Card — Deployment Risk Scorer v1

## Overview
XGBoost binary classifier estimating the probability that a change introduces a defect,
trained on ApacheJIT and calibrated with isotonic regression.

- **Trained** 2026-07-29
- **Dataset** ApacheJIT (Keshavarz & Nagappan, MSR 2022) — 106,674 commits, 15 Apache projects, 2003-09-11–2019-12-26
- **Split** `mature` (temporal) — train 66,144 / val 10,688 / test 19,445
- **Features** 12 — ns, nd, nf, ent, la, ld, ndev, age, nuc, aexp, arexp, asexp

## Performance (held-out test)
| Metric | Value | PRD target |
|---|---|---|
| AUC-ROC | 0.8006 | ≥ 0.72 |
| AUC-PR | 0.5675 | baseline 0.2436 |
| Brier | 0.1468 | ≤ 0.18 |
| Brier (constant base rate) | 0.1842 | reference |
| Top-decile lift | 2.79x | — |
| Top-decile recall | 28.0% | — |

## Limitations — read before trusting a score

1. **Labels are SZZ-derived defect labels, not production incidents.** The model predicts
   "a later bugfix will touch lines this change introduced." It does not predict outages,
   rollbacks, or SLO breaches. Any claim that this is incident prediction rather than
   just-in-time defect prediction is unsupported by the training data.
2. **Training domain is Apache OSS Java projects.** Transfer to a different language,
   review culture, or repository size is unvalidated.
3. **`lt` is unavailable** in ApacheJIT, so the canonical Kamei feature set is
   incomplete by one feature.
4. **`fix` is excluded.**
5. **Entropy is raw, not normalised.** The extractor must match `entropy_spec` in
   `feature_schema_v1.json` exactly.
6. **History features are approximations at inference.** PRD Module 3 samples ≤20 files
   and ≤50 commits per file, then extrapolates. ApacheJIT computed them over full history.
   This is a training/serving distribution gap that is not corrected for.
7. **Label maturity.** Recent commits are systematically under-labelled. The 2019 tail is excluded from this split.

## Intended use
Advisory ranking of changes by relative defect risk. Not an auto-merge gate, not a
security control, not a substitute for review or CI.
