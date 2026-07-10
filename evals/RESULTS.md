# Golden-corpus results

This is the detailed reference for the compressed [Measured performance](../README.md#measured-performance)
table in the README. These are live results for corpus v1 with v2 prompts, run on
2026-07-10. They describe the evaluated synthetic corpus, not an individual
operator's transcripts.

## Methodology

Corpus v1 is a fully synthetic, annotated corpus: 20 sessions, 38 incidents,
seven clusters, three singletons, 10 decoys, and two known scanner misses.
It contains fabricated Claude Code JSONL and Codex rollout sessions, so the
pipeline can be measured without reading a user's real history. The corpus's five
named recurrence clusters are expected to converge; singleton and decoy annotations
must not become clusters or remedies.

The harness has three modes:

- `mock` uses canned responses to prove wiring without model calls.
- `replay` reuses recorded live responses deterministically and is the free
  regression baseline.
- `live` makes end-to-end model calls in a disposable `S2S_HOME` and fake HOME;
  it is the only mode that measures model behavior.

`--quick` is a deterministic subset, not a second corpus: every incident in the
first named cluster, the first remedy-worthy singleton, and the first two decoys.
Known scanner misses remain misses, so selected annotations can outnumber
downstream triage calls. The thresholds in [thresholds.toml](thresholds.toml) are
aspirational gates, deliberately left unchanged while this public corpus exposes
where the system still needs work; a threshold miss is evidence, not a reason to
rewrite the measurement.

## Runs

| Arm | Model | Run ID |
| --- | --- | --- |
| Haiku | Haiku | `evalruns/20260710T094558.356810Z` |
| Sonnet | Sonnet (default) | `matrix-20260710T102611` Sonnet arm |
| Luna | `codex:gpt-5.6-luna` | `evalruns/20260710T112613.729633Z` |

## Full results

Shared full-corpus pipeline results were detection recall **1.0**,
dedup-as-revision catch **1.0**, stability flip rates **0.0**, and pipeline
invariants **1.0** for all three arms. The observed remedy rate was approximately
0.1–0.2 remedies per incident.

| Triage model | Authenticity precision | Authenticity recall | Label agreement | Exact label agreement | Convergence | Singleton ratio | Fast-track recall | Spam clean | Spam precision | Dedup catch | CLAUDE.md share |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Haiku (previous default) | 0.893 | 0.962 | 0.64 | 0.8 | 0.4 | 0.286 | 1.0 | fail | 0.85 | 1.0 | 0.667 |
| Sonnet (new default) | 1.0 | 0.846 | 0.72 | 0.8 | 0.6 | 0.333 | 1.0 | 1.0 | 1.0 | 1.0 | 0.75 |
| `codex:gpt-5.6-luna` | 1.0 | 0.885 | 0.60 | 0.4 | 0.2 | 0.444 | 0.8 | 1.0 | 1.0 | 1.0 | 1.0 |

The README rounds the first two authenticity values to two decimals; this table
retains the recorded precision and recall where supplied. Its headline table is
checked against the corresponding columns here so the compressed view cannot drift.

| Metric | Gate in `thresholds.toml` | Best observed result | Status |
| --- | ---: | ---: | --- |
| Authenticity precision | >= 0.90 | 1.0 | met by Sonnet and Luna |
| Authenticity recall | >= 0.90 | 0.962 | met by Haiku |
| Label agreement | >= 0.80 | 0.72 | active gap |
| Exact label agreement | >= 0.70 | 0.8 | met by Haiku and Sonnet |
| Convergence | >= 0.80 | 0.6 | active frontier |
| Fast-track recall | >= 0.90 | 1.0 | Luna below gate at 0.8 |
| Singleton ratio | <= 0.25 | 0.286 | active gap |
| Spam clean | 1.0 | 1.0 | Haiku failed at 0.85 spam precision |
| Spam precision | >= 0.95 | 1.0 | Haiku below gate at 0.85 |
| CLAUDE.md share | >= 0.70 | 1.0 | Haiku below gate at 0.667 |

## What the arms show

Haiku has the strongest authenticity recall (0.962) and is the lower-cost option,
but its 0.893 authenticity precision and 0.85 spam precision admit contamination.
That makes it useful when recall and cost are the priority, but not the safety-first
default.

Sonnet has perfect authenticity precision and clean spam results, plus the best
convergence score (0.6). Its authenticity recall is lower (0.846), but it is the
default because precision and contamination are safety-critical for a system that
installs remedies: a missed incident can wait for a recurrence, while a contaminated
remedy can install misinformation.

Luna has perfect authenticity and clean-spam results, but 0.60 label agreement and
0.4 exact-label agreement on these Claude-tuned v2 prompts. The Codex arm also has
transport-specific conditions: `--ephemeral`, strict-schema output, and MCP
isolation. Those conditions make the result useful for Codex operators, but not a
drop-in claim that its label behavior matches the Claude-tuned prompt arms.

## The convergence frontier

Convergence remains the active frontier: the best result is 0.6 against the
unchanged 0.8 threshold. The remaining gap is label-boundary scatter rather than a
reason to lower the gate, continuing the investigation lineage of issues #68 and
#69. Keep the threshold at 0.8 and use replay comparisons to show whether prompt or
model changes genuinely reduce fragmentation.

## Reproduce or inspect

```bash
# A small, cost-estimated live subset.
s2s eval --mode live --quick

# Record a full live run, then replay its recorded responses for free.
s2s eval --mode live --record
s2s eval --mode replay

# Compare two or more named, baseline-first profiles on the same corpus.
s2s eval --matrix baseline,premium
```

To rescore existing artifacts without rerunning the pipeline or judge, use the
runner's `rescore_run` operation on a directory containing `record.json` and
`judge-results.json`:

```python
from pathlib import Path
from s2s.evalrun import rescore_run

rescore_run(Path("evalruns/<run-id>"))
```

This checkout does not expose a `s2s eval --rescore` CLI flag; documenting that
spelling as a runnable command would be inaccurate.

## Honest limits

These scores measure a versioned, public synthetic corpus. They do not certify
performance on a person's real transcripts, and live scores can change as model
versions change. The public corpus can also be overfit; use an alternate held-out
corpus with `--corpus <alt>` before treating a prompt win as a product conclusion.
