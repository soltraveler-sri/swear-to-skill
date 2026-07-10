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
| Triage | Haiku, cents per incident |
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

## Evals (coming)

The synthetic-corpus eval harness in [NORTHSTAR §15](docs/NORTHSTAR.md#15-eval-harness--proving-quality-not-just-function) is designed but not built in v0.1.0.

## License and attribution

swear-to-skill is [MIT licensed](LICENSE). It vendors adapted MIT-licensed seed
lexicons from codex-swear-meter; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
