# Changelog

All notable changes to this project are documented here.

## 0.3.0 — Unreleased

- Changed the default triage model to Sonnet after measured golden-corpus results
  showed cleaner remedy evidence; Haiku remains the explicitly selectable frugal arm.
- Documented the live golden-corpus comparison and the Codex CLI transport arm for
  `codex:gpt-5.6-luna`.

## 0.2.1 — 2026-07-10

- Live-eval hardening: subscription-auth transport fixes (claude subprocess now
  always sees the user's real credentials regardless of sandbox overrides),
  synthesize v2 dedup contract with stable reference ids and validation-aware
  retries, curator cluster-synthesize precedence (anti-convergence fix, curate
  v2 prompt), duplicate-evidence normalization, per-group synthesis failure
  isolation, subset-aware eval scoring with small-sample honesty, and richer
  CLI error diagnostics. First full-corpus live eval runs end to end.

## 0.2.0 — 2026-07-10

- Added the end-to-end eval harness (NORTHSTAR §15): a fully synthetic, ground-truth
  annotated golden corpus exercised through the real parsers; sandboxed
  `s2s eval` runner with mock/replay/live modes (live is cost-estimated and
  confirm-gated; replay is a free deterministic baseline).
- Deterministic quality scorers targeting the headline failure modes: convergence
  (anti-fragmentation), spam precision (anti-clutter), detection/triage accuracy,
  N-run stability, and pipeline invariants over repeated cycles.
- LLM-judge grading with versioned rubrics (remedy quality, counterfactual
  prevention), a hand-written calibration set with inversion detection, and
  self-grading-bias disclosure.
- Greenlight report (`report.html`/`report.md`) with narrative excerpt chains,
  failure spotlights, honest-limits footer, and CI exit codes.
- A/B experiment matrix: named profiles varying per-stage model and prompt version,
  comparative reports with deltas, and prompt-version plumbing across all judgment
  surfaces.

## 0.1.0 — 2026-07-09

- Built the local substrate: defensive Claude Code and Codex transcript adapters,
  durable archives, configurable lexicon scanning, SQLite ledger, and local meter.
- Added the judgment loop: structured Claude CLI calls, closed-set triage,
  merge-biased curation, taxonomy gardening, queue-backed pumping, and cost/status
  reporting.
- Added remedies: synthesis and deduplication, human proposal review, safe installs,
  rollback records, notifications, and the `/s2s` companion skill.
- Completed the improvement loop with outcome auditing, revision/retirement proposals,
  opt-in guarded autonomy, Codex detection/meter support, and release packaging.
