> Directly inspired by [**codex-swear-meter**](https://github.com/petergpt/codex-swear-meter) by [Peter Gostev (@petergpt)](https://github.com/petergpt), whose local-first "swear meter" over Codex session logs established the core insight this project builds on. swear-to-skill aims to extend the usefulness of swear-meter, from a benchmark, into an automated remediation pipeline, improving future behavior from past mistakes, and reuses its MIT-licensed seed lexicons.

# swear-to-skill

Your coding-agent transcripts already contain the moment something went wrong.
swear-to-skill keeps those local signals, turns genuine recurring failures into
reviewable remedies, and measures whether the remedy helped. It is both a private
frustration meter and a careful improvement pipeline for Claude Code.

<!-- Screenshot placeholder: local meter dashboard -->

## Quickstart

Requires Python 3.11+ and [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
for the judgment stages. The meter and scanner work without a Claude call.

```sh
uvx swear-to-skill init && s2s backfill && s2s scan && s2s meter --open
```

`init` installs the lightweight Claude Code hooks, the local `/s2s` companion skill,
and a commented config at `~/.s2s/config.toml`. `backfill` preserves old sessions
before Claude Code's retention cleanup can remove them; `scan` records local
detection signals; `meter --open` writes and opens a self-contained `file://`
dashboard.

Run `s2s doctor` after your first approvals to verify the installed remedies are discoverable.

## How the pipeline works

Archive → scan → triage → curate → synthesize → gate → audit. Regex scanning is free;
Claude-backed stages use closed-set labels and merge-biased review so recurring
problems converge instead of silently fragmenting. The default gate puts every
proposed change in human review. Read the full rationale, state machine, and cost
discipline in [NORTHSTAR](docs/NORTHSTAR.md).

## The `/s2s` skill

After `s2s init`, ask Claude Code for `/s2s`. It reports pipeline health and the
meter, presents pending proposals one at a time with their evidence, and waits for
your explicit approval, edit, rejection, or skip. It never treats a summary as
approval.

## Autonomy mode

Autonomy is opt-in: `s2s autonomy on` lets unattended pumps install eligible
CLAUDE.md and skill remedies. Treat it as permission to change your agent setup
without a review click—not as a substitute for review.

Its guardrails are on by default:

- Weekly and total-active-remedy caps (defaults: 3 per rolling week, 15 active).
- Higher confidence thresholds, especially for skills and singleton evidence.
- Provenance, install records, audit digest, and `s2s rollback <id>`.
- `s2s autonomy off` as an immediate kill switch; existing remedies remain until you
  explicitly roll them back.

## Codex support

Codex session logs are supported for detection and the local meter. Remedy installation
targets Claude Code in v0.1.0.

## Privacy

swear-to-skill is local-first. Archives, ledger, config, remedies, and dashboard stay
on your machine. Transcript content leaves the machine only in your own `claude -p`
calls for triage, curation, and synthesis. If you opt into webhook notifications,
their bounded event metadata is sent to that webhook.

The dashboard is a local HTML file, but its aggregates and proposal excerpts can still
be sensitive. A screenshot or a manually shared dashboard reveals what it shows;
sharing is always your choice.

## Cost expectations

| Stage | Default cost shape |
| --- | --- |
| Archive / scan / meter | Free and local |
| Triage | Sonnet, cents per incident (Haiku is the frugal option) |
| Curation | Sonnet, one batched pass over new incidents and touched clusters |
| Synthesis | Sonnet (or Opus for high-stakes work), per promoted cluster |
| Audit | Local measurement; Curator judgment piggybacks on review |

Defaults aim for pennies per week in steady use. A large historical backfill is a
one-time operation that estimates cost and asks before Claude-backed work exceeds the
configured threshold.

## FAQ

### Why archive first?

Claude Code's `cleanupPeriodDays` retention setting can remove older transcripts
(commonly after 30 days). `s2s backfill` copies them into the private s2s archive
before that happens.

### Why did nothing become a skill yet?

That is often the correct outcome. Most fixes should be a small CLAUDE.md rule, a hook,
or benchmark-only data; a skill is for a genuinely procedural remedy. Check `s2s
status`: a high singleton ratio means incidents are not yet converging, while a high
`other` share signals a taxonomy gap that the curator should garden.

### Does it need an API key?

No. Claude-backed stages call your existing authenticated `claude` CLI. The scanner,
meter, dashboard, and status view do not need Claude calls.

## Evals — try it before you trust it

Before integrating (and especially before enabling autonomy), you can watch the whole
pipeline work on a safe, fully synthetic corpus — with Claude genuinely in the loop —
inside a sandbox that never touches your real transcripts, `~/.claude`, or `~/.s2s`:

```bash
s2s eval --mode mock          # free wiring check (no model calls)
s2s eval --mode live --quick  # small real-Claude run, cost-estimated & confirmed
s2s eval --mode live --record # full run; records responses for free replays
s2s eval --mode replay        # deterministic re-run from recordings (free)
```

Each run produces a greenlight report (`report.html`) with PASS/FAIL against
thresholds and narrative excerpts tracing real incidents through detection → triage →
clustering → remedy → judge verdicts. `s2s eval --matrix baseline,premium` compares
model/prompt configurations side by side. Design details: [NORTHSTAR §15](docs/NORTHSTAR.md#15-eval-harness--proving-quality-not-just-function).
No API key is needed — evals use your `claude` CLI subscription like everything else.

## Measured performance

Full live golden-corpus results (v2 prompts, 2026-07-10) are strong across all
three triage models: detection recall and dedup-as-revision catch 1.0, stability
(flip rates) 0.0, pipeline invariants 1.0, and ~0.1–0.2 remedies per incident.

Full methodology, run IDs, and results: [evals/RESULTS.md](evals/RESULTS.md).

| Triage model | Authenticity precision | Authenticity recall | Label agreement | Convergence | Evidence contamination |
| --- | ---: | ---: | ---: | ---: | --- |
| Haiku (previous default) | 0.89 | 0.96 | 0.64 | 0.4 | some (spam precision 0.85) |
| Sonnet (new default) | 1.00 | 0.85 | 0.72 | 0.6 | none (1.0 / clean) |
| `codex:gpt-5.6-luna` | 1.00 | 0.89 | 0.60 | 0.2 | none (1.0 / clean) |

Precision and contamination are safety-critical for a system that installs remedies:
a missed incident waits for recurrence, while a contaminated remedy installs
misinformation. That makes Sonnet the default. Haiku is cheaper with higher recall
but some contamination risk; choose it via `[models] triage` in config. For Codex CLI
operators, Luna has excellent authenticity but lower label agreement than the Claude
models on these prompts; use `codex:gpt-5.6-luna` and measure your own arm with
`s2s eval --matrix`.

Convergence (0.6 best) remains the active frontier (issues #68/#69 lineage);
thresholds are intentionally aspirational and unchanged. Reproduce on your machine
with `s2s eval --mode live --quick`; recorded replays make re-scoring free.

## License and attribution

swear-to-skill is [MIT licensed](LICENSE). It vendors adapted MIT-licensed seed
lexicons from codex-swear-meter; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

---

*Skills generated by this pipeline gently ask your agent to append a small "· used skill \<name\> (from swear-to-skill)" note to its task summaries — a deliberate guard against the silent failure of skills existing but never being seen or used — and if you prefer clean summaries you can turn it off by setting `attribution = false` under `[visibility]` in `~/.s2s/config.toml`.*
