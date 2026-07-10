# swear-to-skill

> Directly inspired by [**codex-swear-meter**](https://github.com/petergpt/codex-swear-meter) by [Peter Gostev (@petergpt)](https://github.com/petergpt), whose local-first "swear meter" over Codex session logs established the core insight this project builds on. swear-to-skill aims to extend the usefulness of swear-meter, from a benchmark, into an automated remediation pipeline, improving future behavior from past mistakes, and reuses its MIT-licensed seed lexicons.

Every time you get frustrated with your coding agent, you emit a high-signal label on a
real failure mode — in your own words, at the exact moment it happened, with full
context sitting in a transcript on your disk.

**swear-to-skill** mines those moments from your local **Claude Code** (and Codex)
session transcripts and turns *recurring, generalizable* failure modes into durable
remedies: skills, `CLAUDE.md` rules, hooks, or settings — then measures whether each
remedy actually reduced the failure mode it was built for.

Two products in one pipeline:

1. **The meter** — your frustration rate over time, per model, per project. A personal
   benchmark of agent quality (and a fun chart).
2. **The pipeline** — detection → triage → curation → synthesis → admission → outcome
   measurement. Human-gated by default; fully autonomous if you opt in.

Local-first: nothing leaves your machine except through your own `claude` CLI calls
(your existing subscription — no API key needed).

## The /s2s companion skill

`s2s init` installs the local `/s2s` Claude Code skill. It summarizes status and the
swear meter, walks pending remedy proposals one at a time, and only acts after your
explicit approval or rejection. It can also link the local dashboard; publishing it
as a private artifact is always optional and user-initiated.
<!-- Screenshot placeholder: /s2s proposal review -->

## Status

🚧 **Pre-implementation.** The complete architecture lives in
[docs/NORTHSTAR.md](docs/NORTHSTAR.md). Implementation is planned as PR-scoped issues
grouped into sprints — see the issue tracker.

## Design at a glance

- **Fragmentation-proof clustering by construction** — cheap models never make
  merge/match decisions; they classify into a closed failure-mode taxonomy, so similar
  incidents can only converge. Open-set judgment (new categories, splits) is reserved
  for capable models under explicitly merge-biased instructions.
- **Thresholds prioritize; intelligence decides** — every logged incident is eventually
  judged in full context by a capable model (once — O(new) cost, never O(ledger)),
  which can promote even a single instructive incident to a remedy.
- **Most frustrations don't deserve a skill** — remedies route to the cheapest fix:
  usually a one-line `CLAUDE.md` rule; a skill only when the fix is genuinely
  procedural; sometimes "benchmark-only."
- **Reversible & auditable** — every installed remedy carries provenance to its
  evidence and rolls back with one command.

## License

[MIT](LICENSE). Vendored third-party material: see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
