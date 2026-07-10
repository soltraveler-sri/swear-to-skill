# swear-to-skill — North-Star Design

> **Status:** Approved design baseline. Implementation planning (PR-scoped issues,
> sprint clusters) derives from this document. Changes to the architecture described
> here should update this document first.

---

## 1. Vision

Every time you get frustrated with a coding agent, you are emitting a high-signal label
on a real failure mode — for free, in your own words, at the exact moment it happened,
with the full context sitting in a transcript on your disk.

**swear-to-skill** mines those moments from your local Claude Code (and Codex) session
transcripts and converts *recurring, generalizable* failure modes into durable remedies:
skills, `CLAUDE.md` rules, hooks, or settings. It closes the loop by measuring whether
each remedy actually reduced the failure mode it was built for. Left running, it is a
thin, honest form of recursive improvement: the more you use your agent, the better
your agent gets at not repeating the things that annoy you.

Two products in one pipeline:

1. **The meter** (byproduct, fun, shareable): your frustration rate over time, per
   model, per project — a personal benchmark of agent quality.
2. **The pipeline** (the point): detection → triage → curation → synthesis → admission
   → outcome measurement.

### Lineage and credit

This project is directly inspired by
[petergpt/codex-swear-meter](https://github.com/petergpt/codex-swear-meter) (Peter
Gostev), which established the core insight — that rudeness toward a coding agent is a
measurable, local-first benchmark signal — and proved the extraction mechanics over
Codex session logs. swear-to-skill extends that idea from *benchmark* to *remediation
pipeline*, and centers Claude Code. We reuse its MIT-licensed seed lexicons and several
of its extraction disciplines (see §13).

---

## 2. Design principles

**P1 — Fragmentation is the fatal failure mode.**
A cautious model asked "are these two incidents the same?" will say no, forever, and
the pipeline silently dies: every incident gets its own bucket, no bucket ever recurs,
no remedy is ever produced. This failure is *invisible* (the system looks like it's
working — it's just "waiting for recurrence"). Over-merging, by contrast, is visible
and self-correcting: a downstream, smarter model with full evidence in front of it can
split a too-broad cluster, and worst case a remedy covers two scenarios instead of one
— which is strictly fine. Therefore, at every point in the pipeline:
- **Cheap models never make merge/match decisions.** They perform closed-set
  classification only (assigning to a fixed taxonomy), where "same cluster" is a
  side effect of sharing a label, not a judgment call.
- **Where open-set judgment is unavoidable, it runs on a capable model with
  asymmetric, merge-biased instructions**: attaching to an existing bucket is the
  default; creating a new bucket requires an articulated justification for why no
  existing bucket could cover it.
- **Every error the conservative tier *can* make must be recoverable downstream.**
  The system's error topology is: cheap tier can only over-merge (recoverable);
  only the capable tier can split or create (accountable).

**P2 — Thresholds prioritize; intelligence decides.**
No deterministic occurrence count is ever the sole gate on remedy creation. Every
incident in the ledger is eventually seen, in full context, by a capable model with
the authority to promote it — including promoting a *single* incident whose lesson is
general (some things only need to happen once to be worth learning). Recurrence
thresholds exist only to *fast-track* synthesis, never to *block* it. Symmetrically,
recurrence alone doesn't force a remedy: the reviewing model can judge a recurring
pattern unworthy (not actionable, model-level, already covered).

**P3 — Pay for each token of judgment exactly once.**
"Intelligence sees everything" must not mean "re-feed the ledger on every run."
Each incident receives exactly one full-context review; thereafter it exists in
review passes only as a one-line digest entry, and is re-surfaced with full context
only on an event (a new incident landing in its cluster, or a remedy being drafted
from it). Review cost is O(new incidents), never O(ledger size).

**P4 — Most frustrations don't deserve a skill.**
The synthesis stage routes to the *cheapest remedy that fixes the failure mode*:
usually a one-line `CLAUDE.md` rule; sometimes a hook or setting; a skill only when
the fix is genuinely procedural/multi-step; sometimes "benchmark-only" (model-level
failure no instruction can fix). Skill count is an explicit budget, not an
accumulating side effect.

**P5 — Human-gated by default, autonomous by choice.**
The admission gate is a *policy point*, not a hardcoded stage. Default policy:
human review of every proposal. Opt-in policy: fully autonomous
collect→synthesize→install, with guardrails (caps, provenance, git-tracked rollback,
audit digest). The architecture must make autonomy a configuration flip, not a fork.

**P6 — Local-first, transparent, reversible.**
No transcript content leaves the machine except through the user's own `claude` CLI
calls (which use their existing subscription/auth — no API key required). Every
artifact the system writes is inspectable text/SQLite, every installed remedy carries
provenance back to its evidence, and every installation is reversible with one command.

**P7 — Guests in someone else's schema.**
Transcript formats are undocumented and version-drifting. Parsers are defensive
(fallbacks, tolerant of unknown line types, never crash the pipeline on a weird line),
adapters are isolated per CLI, and the raw archive preserves originals so re-parsing
after a format change is always possible.

---

## 3. System overview

```
                       ┌────────────────────────────────────────────────┐
                       │                  TRANSCRIPTS                   │
                       │  ~/.claude/projects/**/*.jsonl   (primary)     │
                       │  ~/.codex/sessions/** + history.jsonl (adapter)│
                       └───────────────┬────────────────────────────────┘
                                       │ SessionEnd hook (live) / backfill (historical)
                        ┌──────────────▼──────────────┐
              Stage 0   │       ARCHIVER (free)        │  copy-out before retention cleanup
                        └──────────────┬──────────────┘
                        ┌──────────────▼──────────────┐
              Stage 1   │    SCANNER (free, regex)     │  lexicon hits over direct user msgs
                        └──────────────┬──────────────┘   → detection queue   [meter data]
                        ┌──────────────▼──────────────┐
              Stage 2   │   TRIAGER (Haiku, per-hit)   │  real frustration? + closed-set
                        │   closed-set classification  │  failure-mode label + context pack
                        └──────────────┬──────────────┘   → LEDGER (incident: open)
                        ┌──────────────▼──────────────┐
              Stage 3   │  CURATOR (Sonnet, batched)   │  judgment over every incident once:
                        │  promote / park / dismiss    │  singleton promotion allowed;
                        │  + taxonomy gardening        │  merge-biased; O(new) economics
                        └──────────────┬──────────────┘
                        ┌──────────────▼──────────────┐
              Stage 4   │ SYNTHESIST (Sonnet/Opus)     │  remedy proposal + evidence:
                        │ remedy routing               │  skill | CLAUDE.md | hook | none
                        └──────────────┬──────────────┘
                        ┌──────────────▼──────────────┐
              Stage 5   │  GATE (policy point)         │  default: human review queue
                        │  review │ autonomous         │  opt-in: auto-install w/ guardrails
                        └──────────────┬──────────────┘
                        ┌──────────────▼──────────────┐
              Stage 6   │  AUDITOR (free + periodic)   │  per-cluster incident rate pre/post
                        │  outcome measurement         │  remedy → revise/retire proposals
                        └─────────────────────────────┘
```

One CLI (`s2s`) drives everything; hooks make the live path automatic.

---

## 4. Stages

### Stage 0 — Archiver (deterministic, free)

- **Trigger:** Claude Code `SessionEnd` hook (receives `transcript_path` directly);
  plus `s2s backfill` for the existing historical corpus.
- **Action:** copy the finished transcript into the s2s archive
  (`~/.s2s/archive/<project-slug>/<session-id>.jsonl`), append to the processing queue.
- **Why it exists:** Claude Code's `cleanupPeriodDays` (default 30) deletes old
  transcripts. The transcripts are this system's training data; step zero is making
  them durable. The hook body is milliseconds of file copy — no LLM, no parsing.

### Stage 1 — Scanner (deterministic, free)

- **Input:** queued transcripts (live) or the archive (backfill).
- **Extraction discipline** (adopted from codex-swear-meter, adapted to Claude Code's
  schema): parse only *direct human* messages — `type:"user"`, `isMeta` false,
  `isSidechain` false, string content, scaffold/automation prefixes filtered; dedup on
  `(session_id, timestamp, message)`.
- **Detection:** compiled word-boundary regexes from JSON lexicons. Seeded with the
  upstream MIT lexicons (13 categories, 327 terms — notably the non-profanity
  categories like `agent_callout` ("not what I asked"), `trust_break` ("hallucinated"),
  `incomplete_work`, `rework_cost`, which are purer failure-mode signals than actual
  swearing). Lexicons are user-editable; an n-gram candidate-phrase miner (upstream
  technique) periodically proposes user-specific phrasings.
- **Output:** detection records (message + hit metadata) → triage queue. Also feeds
  the **meter**: per-week/per-model/per-project frustration rates and the HTML
  dashboard, which requires no LLM at all.

### Stage 2 — Triager (Haiku, cheap, per-incident)

For each detection, build a **context pack** by walking the `parentUuid` chain:
the user request that preceded the incident, a compact digest of what the agent
did (tool calls summarized, not raw dumps), the frustrated message itself, and the
following exchange (how/whether it got resolved). Then one `claude -p
--output-format json --json-schema … --model haiku` call (neutral-cwd,
no session persistence) answers:

1. **Authenticity:** is this genuine frustration *at the agent's behavior*?
   (Not: quoting, venting about a flaky third-party, self-directed, playful.)
2. **Closed-set classification:** assign exactly one primary failure-mode label from
   the canonical taxonomy (§6). `other` is a legal answer and is *expected* to be
   used rather than forced misfits — the gardener mines it.
3. **Generalized one-liner:** the failure mode restated with no project-specific
   nouns. (Used downstream as digest text — never as a clustering key.)
4. Severity + confidence.

Dismissals are kept (state `dismissed-triage`) — nothing is deleted, and triage
dismissals are sampled by the Curator for quality control.

**Crucially, the Triager makes no comparisons and no merge decisions** (P1). Two
incidents cluster by receiving the same label independently.

### Stage 3 — Curator (Sonnet, batched judgment)

The intelligence-in-the-loop that sees everything (P2), at O(new) cost (P3).

- **Trigger:** accumulation-based — ≥N unreviewed incidents (default 10) or ≥7 days
  since last pass, whichever first; also on demand (`s2s review`).
- **Input per pass:** (a) full context packs for *unreviewed* incidents only;
  (b) the **ledger digest**: one line per active cluster (label, count, one-liners,
  state) and one line per installed/proposed remedy; (c) full packs for parked
  incidents *only in clusters touched by new arrivals* (event-driven resurfacing).
- **Authority (per incident):**
  - `promote` — attach to a synthesis candidate. **Singletons may be promoted**: a
    one-off incident whose lesson generalizes ("even one occurrence teaches X")
    goes straight to the Synthesist with `singleton` provenance.
  - `park` — plausible pattern, not yet actionable; wake condition = next arrival
    in its cluster.
  - `dismiss` — reviewed and judged not remedy-worthy (recorded with reason; still
    counts in meter stats).
  - `reassign` — fix a triage label.
- **Authority (per cluster):** recurring clusters are *fast-tracked* to synthesis at
  ≥3 incidents spanning ≥2 projects — but the Curator may also judge a recurring
  cluster unworthy (P2 cuts both ways) or send an under-threshold cluster forward.
- **Taxonomy gardening (same pass):** review the `other` bucket and label
  distribution; merge near-duplicate labels; propose new canonical labels when
  `other` accumulates a coherent sub-pattern. Merge-biased per P1: keeping two
  labels separate requires articulating why one remedy could not cover both.

Every incident thus gets exactly one full-context judgment, and lives afterward as
a digest line until an event resurfaces it.

### Stage 4 — Synthesist (Sonnet default; Opus for high-stakes)

Input: a promoted cluster (or singleton) with full evidence. Output: a **remedy
proposal** — a self-contained artifact containing:

- **Remedy routing** (P4), in order of preference:
  1. `claude-md` — a one/two-line standing rule (global or per-project).
  2. `hook`/`setting` — mechanical enforcement where possible.
  3. `skill` — only when the fix is procedural/multi-step; includes full `SKILL.md`
     with a carefully written `description` (the auto-invocation trigger — its
     quality *is* the skill's effectiveness).
  4. `benchmark-only` — model-level failure; no instruction remedy; meter data only.
- The generalized failure statement, the drafted remedy text, evidence excerpts
  (incident IDs + quotes), and a **dedup check** against existing skills,
  `CLAUDE.md` contents, and prior proposals (the Synthesist is shown their digests
  and must explicitly clear or flag overlap — merge-biased: overlapping proposals
  become *revisions* of the existing remedy, not siblings).
- If the cluster is heterogeneous (over-merged upstream — the recoverable error
  P1 deliberately permits), the Synthesist may split it or write one remedy
  covering both variants, whichever is genuinely better.

### Stage 5 — Gate (policy point)

**`review` policy (default):** proposals land in a queue; `s2s proposals` renders a
digest (remedy, evidence, dedup verdict). The user approves, edits, or rejects;
approval installs (writes skill dir / appends CLAUDE.md block / registers hook).

**`autonomous` policy (opt-in, §8):** the same install path runs without a human,
under guardrails.

Either way, **installation is transactional and reversible**: every installed remedy
is recorded in a git-tracked state dir with provenance (cluster, incidents,
proposal), tagged in-file (`<!-- s2s:remedy-id -->` / frontmatter comment), and
removable via `s2s rollback <remedy-id>`.

### Stage 6 — Auditor (deterministic + periodic judgment)

Stage 1 never stops running, so outcome data is free: for each installed remedy,
compare its cluster's incident rate (normalized per active session) before vs.
after installation. Periodically (or in the Curator pass) flag:
- remedies whose failure mode persists → propose revision (evidence: the new
  incidents that occurred *despite* the remedy);
- remedies whose cluster has been silent for a long horizon → candidate for
  retirement/archival (skills budget hygiene, P4).

Revision/retirement proposals flow through the same Gate as new remedies.

---

## 5. The ledger

SQLite (stdlib) at `~/.s2s/ledger.db`. Core entities:

- **incident** — id, source (claude-code|codex), session id, project, timestamps,
  message, context-pack pointer (into archive), label, one-liner, severity,
  confidence, **state**, state history.
- **cluster** — implicit = (label) after gardening; materialized view with counts,
  project spread, first/last seen, linked remedies.
- **proposal** — remedy type, drafted content, evidence incident IDs, dedup verdict,
  gate status, install record.
- **remedy** — installed artifact, provenance, install/rollback metadata, outcome
  stats.
- **run log** — every LLM call: stage, model, tokens/cost, inputs digest (audit +
  cost accounting; surfaced by `s2s status`).

**Incident state machine:**

```
detected ─▶ triaged ─┬─▶ dismissed-triage ──(Curator QC resurrection)──▶ open
                     └─▶ open ─▶ (Curator) ─┬─▶ promoted ─▶ in-proposal ─▶ remedied
                                            ├─▶ parked  ──(new arrival in cluster)──▶ open
                                            └─▶ dismissed-reviewed
```

Nothing is deleted; dismissed incidents still feed the meter and remain queryable.

---

## 6. The taxonomy (anti-fragmentation core)

Seed canonical failure-mode labels (v1 — expected to be gardened):

| Label | Gist |
|---|---|
| `premature-completion-claim` | said "done"/"fixed" when it wasn't |
| `unverified-change` | claimed a fix without running/testing it |
| `ignored-instruction` | explicit user instruction not followed |
| `forgotten-context` | lost an earlier constraint mid-session |
| `scope-deviation` | did more or less than asked |
| `hallucinated-interface` | invented API/flag/file/behavior |
| `destructive-action` | deleted/overwrote/reset without care |
| `repeated-after-correction` | same mistake again after being corrected |
| `shallow-investigation` | guessed instead of reading the code/logs |
| `misread-request` | solved the wrong problem |
| `overengineering` | needless complexity/abstraction |
| `environment-mismanagement` | wrong dir/branch/deps/tool state |
| `house-rules-violation` | ignored CLAUDE.md / workflow conventions |
| `tool-misuse` | wrong tool, wasteful calls, permission thrash |
| `communication-failure` | buried the outcome, unclear/false reporting |
| `other` | escape hatch — gardened, never a dead end |

Rules that make this fragmentation-proof by construction:

1. The Triager picks from this list — it cannot create labels, so it cannot
   fragment. Two similar incidents can only converge or land in `other`.
2. `other` is monitored: when it accumulates, the *Curator* (capable model) mines
   it for new canonical labels — creation of buckets is exclusively a
   capable-model, merge-biased act.
3. Labels are deliberately coarse. Fine-grained distinctions are made *inside* a
   cluster by the Synthesist at remedy time — with all evidence in view — which is
   the correct altitude for splitting.
4. All prompts that compare anything carry the asymmetry instruction verbatim:
   *"Attaching two similar-but-not-identical items is a cheap, recoverable error;
   keeping them apart when related is silent and fatal. When uncertain, attach.
   To keep items apart you must state why one remedy could not cover both."*
5. **Fragmentation telemetry:** `s2s status` reports the singleton ratio (% of
   clusters with exactly one incident) and `other` share. A sustained
   singleton ratio near 100% is the RSS-pipeline death pattern — surfaced
   explicitly instead of discovered by absence of output.

---

## 7. Review economics

| Stage | Model | Cost shape | Trigger |
|---|---|---|---|
| Archive/Scan | none | ~0 | SessionEnd hook / backfill |
| Triage | Haiku | ¢-level per incident, O(hits) | batched (default daily or on-demand) |
| Curate | Sonnet | one batched pass, O(new incidents + touched clusters) | ≥10 unreviewed or 7 days |
| Synthesize | Sonnet/Opus | per promoted cluster (rare) | promotion |
| Audit | none + Curator | ~0 | continuous / piggybacks on Curate |

All LLM calls go through the user's `claude -p` (subscription auth, neutral-cwd isolation for
reproducibility, `--json-schema` for structure). Costs are logged per run and
surfaced. Defaults keep steady-state spend at pennies/week; backfilling 1,000+
historical sessions is a one-time, explicitly confirmed operation with a cost
estimate up front.

---

## 8. Autonomy mode

`s2s autonomy on` flips the Gate policy. Same pipeline, no human at Stage 5.

Guardrails (all configurable, all on by default):

- **Caps:** max auto-installed remedies per week (default 3) and max total active
  auto-skills (default 15); beyond caps, proposals queue for human review anyway.
- **Provenance + reversibility:** every auto-install is git-committed to the s2s
  state dir with full evidence; `s2s rollback` reverts any remedy;
  `s2s autonomy off` is an instant kill switch (existing remedies stay unless
  rolled back).
- **Conservatism escalation:** auto mode requires a higher Synthesist confidence
  bar than review mode; `claude-md`-line remedies (cheap, low-blast-radius) are
  preferred over skills more aggressively.
- **Audit digest:** every autonomous action appends to a human-readable digest
  (`s2s status` / optional SessionStart one-liner: "s2s installed 1 remedy since
  your last session — `s2s log` to inspect").
- **Self-correction stays on:** the Auditor's revise/retire loop is *more*
  important in auto mode and runs autonomously under the same caps.

Build order: the Gate ships as `review` first; `autonomous` is a policy module
added once the install/rollback/audit substrate is proven. Not "finished" until
autonomy mode exists (explicit project requirement).

---

## 9. Runtime & UX: scheduling, notifications, dashboard

### 9.1 Scheduling: durable queue, opportunistic pump

All pipeline work is queue-backed in the ledger (SQLite). **Triggers never create
work; they only pump the queue** — so if the machine is off, `claude` is
unavailable, or a run is interrupted, items simply wait. Nothing is ever skipped,
only deferred.

- **Default trigger — opportunistic pump:** the SessionEnd hook, after its
  inline archive+scan (milliseconds, no LLM), spawns a detached background
  process (`s2s pump --background`) that checks accumulation thresholds
  (triage: ≥K untriaged or ≥24h since last run; curate: ≥10 unreviewed or ≥7
  days) and runs whichever stages are due. Rationale: this ties compute to
  actual usage — new data exists exactly when sessions end, the machine is
  necessarily on, and the `claude` CLI auth is warm. Zero configuration, no
  daemon.
- **Concurrency safety:** a lockfile makes pumps mutually exclusive; every
  stage is idempotent and resumable mid-batch (state transitions are per-item).
- **Optional fixed cadence:** `s2s schedule install` sets up a launchd agent
  (macOS) / systemd user timer (Linux) invoking `s2s pump`. Strictly additive —
  the opportunistic path remains the backbone.
- **Manual:** `s2s run` (pump now), `s2s review` (force a Curator pass).

### 9.2 Human-in-the-loop notification (layered)

- **Layer 0 — on demand (always):** `s2s status`, `s2s proposals`.
- **Layer 1 — default:** a **SessionStart digest line** injected by hook. It
  reads a precomputed status file (written by the pump — no LLM, no latency,
  no DB query at session start): e.g. *"s2s: 2 remedy proposals awaiting
  review — `s2s proposals` or `/s2s`."* Silent when there is nothing to say.
- **Layer 2 — opt-in (config):** desktop notification (macOS `osascript` /
  Linux `notify-send`) and a **generic webhook** (JSON POST, Slack/Discord
  compatible) fired when a proposal enters the queue or an autonomous action
  occurs. No email, no cloud service in v1.

### 9.3 Dashboard & in-session surface

- **Primary: self-contained local HTML** (upstream's proven approach, and the
  P6-correct one): a single static file with the meter charts (weekly
  frustration rate, per-model, per-project), pipeline health (queue depths,
  singleton ratio, `other` share, cost log), and the current proposals digest.
  `s2s meter --open` regenerates and opens it; the `file://` path is clickable
  from terminals and inside Claude Code.
- **Companion Claude Code skill `/s2s`** (vendored in this repo under
  `skill/`, installed to `~/.claude/skills/s2s/` by `s2s init`): surfaces
  status inline, links the dashboard, and enables **conversational proposal
  review** — Claude presents each pending proposal with its evidence and runs
  `s2s approve <id>` / `s2s reject <id>` on the user's word. The human gate
  thus lives natively inside the Claude Code workflow.
- **Claude Artifacts:** not a default surface — artifacts are uploaded to
  claude.ai hosting, and P6 says transcript-derived data stays local unless
  the user chooses otherwise. Documented as an optional, user-initiated way to
  share the dashboard; never automatic.

---

## 10. Adapters

A thin adapter per CLI normalizes transcripts into (direct-user-message stream +
context-pack builder). Everything from Stage 1 onward is adapter-agnostic.

- **`claude-code` (primary):** `~/.claude/projects/<slug>/<session>.jsonl`.
  Typed JSONL lines; user messages = `type:"user"`, filter `isMeta`,
  `isSidechain`, non-string content; context via `parentUuid` chain; rich
  metadata (cwd, gitBranch, version, timestamps). Live trigger: SessionEnd hook.
- **`codex`:** `~/.codex/history.jsonl` (flat prompt index — detection is a grep)
  joined to `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` for context;
  `event_msg.user_message` primary with `response_item` fallback (upstream's
  proven dual-path); subagent sessions filtered via `session_meta.source`.
  No SessionEnd hook equivalent → periodic/on-demand scan.

Remedy installation targets Claude Code natively (`~/.claude/skills/`,
`CLAUDE.md`, hooks/settings). Codex remedy installation (`AGENTS.md`,
`~/.codex/skills/`) is a post-v1 extension — the proposal format is
target-agnostic by design.

---

## 11. Tech stack & repo layout

- **Python ≥3.11, stdlib-only core** (argparse, sqlite3, re, json, pathlib) —
  upstream proved this is enough; zero-dep install (`uvx swear-to-skill` / `pipx`).
  LLM access is exclusively via subprocess to the user's `claude` CLI — no SDK
  dependency, no API key, works on any Claude subscription.
- **One console entry point:** `s2s` (alias `swear-to-skill`) with subcommands:
  `init` (installs hooks, /s2s skill, config), `backfill`, `scan`, `triage`,
  `review`, `run`, `pump`, `schedule install|remove`, `proposals`,
  `approve/reject`, `meter` (HTML dashboard), `status`, `log`, `rollback`,
  `autonomy on|off`.
- Layout:

```
swear-to-skill/
├── README.md                  # first line: credit + link to codex-swear-meter
├── LICENSE                    # MIT
├── THIRD_PARTY_NOTICES.md     # upstream MIT license text (lexicons)
├── docs/NORTHSTAR.md          # this document
├── pyproject.toml
├── src/s2s/
│   ├── adapters/              # claude_code.py, codex.py
│   ├── lexicons/              # seed JSON (vendored, notice-preserved) + user overrides
│   ├── scanner.py  triager.py  curator.py  synthesist.py
│   ├── gate.py  auditor.py  ledger.py  llm.py  meter.py  cli.py
│   ├── pump.py  notify.py
│   └── prompts/               # versioned prompt + json-schema files
├── skill/                     # vendored /s2s Claude Code companion skill
└── tests/                     # fixture transcripts, mock-LLM pipeline tests
```

- **Testing:** fixture JSONL transcripts + a mock `claude` binary make the whole
  pipeline testable at zero API cost; real-call tests are few, small, and
  explicitly marked (cost discipline).

---

## 12. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Transcript schema drift (undocumented, versioned) | Defensive parsers, per-adapter isolation, raw archive enables re-parse (P7) |
| `cleanupPeriodDays` deletes history | Stage 0 archiver is the first thing installed |
| Fragmentation (the RSS failure) | §6 by-construction design + singleton-ratio telemetry |
| Skill spam / context bloat | Remedy routing prefers CLAUDE.md lines; skill caps; Auditor retirement; lazy-loading limits cost to descriptions |
| Haiku triage quality | Closed-set task (easy mode); Curator samples dismissals + reassigns labels |
| Cheap-model over-merge | Deliberately permitted; Synthesist splits with full evidence (P1) |
| Cost creep | Batched triggers, run log with per-stage cost, backfill requires confirmation |
| Privacy (transcripts contain code/secrets) | Local-only processing; meter HTML contains aggregates + excerpts the user chooses to share; docs warn like upstream does |
| Auto mode installs a bad remedy | Confidence bar, caps, git-tracked rollback, audit digest, kill switch |

---

## 13. Licensing & attribution

- This project: **MIT**.
- Upstream: [codex-swear-meter](https://github.com/petergpt/codex-swear-meter),
  MIT, Copyright (c) 2026 Peter. We vendor its lexicon JSONs (adapted) and adopt
  several extraction techniques. MIT obligations and good practice, both honored:
  - README **first line** credits the project and author with a link.
  - `THIRD_PARTY_NOTICES.md` reproduces the upstream MIT license and copyright
    notice verbatim, scoped to the vendored material.
  - Vendored lexicon files carry a header comment noting origin and license.
  - No code is copied wholesale; techniques are reimplemented, ideas credited.

---

## 14. Non-goals (v1)

- Not a sentiment analyzer or psychological profile — matches are review leads
  (upstream's framing, kept).
- No cloud service, no telemetry, no account.
- No cross-user/shared skill marketplace (interesting later; out of scope).
- No real-time in-session intervention (SessionEnd is the unit of work).
- Codex *remedy installation* (detection/triage of Codex logs IS in scope).

---

## 15. Eval harness — proving quality, not just function

The test suite proves the machine works; the eval harness proves the machine is
*good*. It exists for two audiences: **a prospective user** who likes the idea
but won't point an autonomous remedy-writer at their real workflow on faith —
they run the evals, watch the whole pipeline operate on a safe corpus with
Claude genuinely in the loop, and get a greenlight (or not) plus legible
evidence; and **us**, measuring output quality to decide where to improve
prompts, raise reasoning effort, or change models.

### 15.1 The golden corpus (safe by construction)

A curated, fully synthetic transcript corpus — fabricated projects and sessions
in the real Claude Code JSONL schema (and Codex rollout schema), with planted
incidents. Every planted incident carries a **ground-truth annotation**:
authentic frustration or decoy (venting at a third-party tool, quoting, playful
swearing); its true failure-mode cluster (the corpus contains K clusters spread
across projects, plus genuine singletons); whether it is remedy-worthy and the
expected remedy *shape*; and near-duplicates of remedies that already "exist"
(to test dedup). The corpus is data, versioned in-repo, with a lint that
enforces schema validity and privacy-safety (no real names, paths, or code).
Evals never read a user's real transcripts unless explicitly pointed at them.

### 15.2 Sandboxed, three-mode execution

Every eval run fabricates a disposable environment (tmp `S2S_HOME`, fake
`$HOME` targets) — the proven smoke-test pattern, productized. Three modes:

- **`mock`** — canned responses; free; proves wiring (CI default).
- **`replay`** — recorded real-model responses replayed deterministically;
  free; the regression baseline.
- **`live`** — real `claude` calls end to end; costs real money (estimated
  up front, confirm-gated, with a `--quick` subset); the trust-building run
  and the only mode that measures real model behavior.

For corpus v1, `--quick` is deterministic rather than another mutable corpus
annotation: it selects every incident in the manifest's first named cluster,
the first remedy-worthy singleton, and the first two decoys. Known scanner
misses remain honest misses, so the selected annotation count may exceed the
number of downstream triage calls.

### 15.3 What gets measured

Deterministic, ground-truth-scored metrics:
- **Detection recall** (planted trigger phrases found) and scaffold-filter
  precision.
- **Triage quality**: authenticity precision/recall against annotations;
  label agreement with ground-truth clusters.
- **Convergence score** (the anti-fragmentation gauge, §6 rule 5): do the K
  planted clusters converge — singleton ratio vs expected, recurrence
  fast-tracks fired, `other` share. A corpus that should cluster and doesn't
  is a failing eval, not a silent death.
- **Spam precision** (the anti-clutter gauge): decoys and unworthy incidents
  that must NOT become remedies; remedies-per-incident ratio; dedup catch
  rate on planted near-duplicates; remedy-type routing distribution
  (`claude-md` should dominate; skills should be rare).
- **Stability**: N-run repeat of stochastic stages on identical inputs —
  label flip rate, verdict flip rate, schema-retry rate. "Works" and "works
  reliably" are different claims.
- **Pipeline invariants over time**: repeated pump cycles over a growing
  corpus — idempotence, state-machine integrity, O(new) economics hold on
  the tenth run, not just the first.

Model-graded (LLM-judge) metrics, rubric-driven, judge model configurable and
distinct from the system-under-test by default:
- **Remedy quality rubric**: actionable, general without vagueness, trigger
  quality of skill descriptions, no project-specific nouns leaked, correct
  routing rationale.
- **Counterfactual prevention** (goal-level, not action-level): *would this
  remedy, had it existed, plausibly have prevented the planted incidents it
  was synthesized from?* A proposal being created is not success; a remedy
  that addresses the actual failure is.
- Judge prompts are versioned files, same as pipeline prompts.

### 15.4 The greenlight report

One command (`s2s eval`) produces machine-readable results plus a
human-readable report: a PASS/FAIL greenlight against thresholds, per-metric
scores, cost of the run, and **narrative excerpts** that make the pipeline
legible — e.g. *found "not what I asked" in session X → extracted context
showing the agent renamed the wrong file → triaged `ignored-instruction`
(authentic, 0.91) → clustered with 2 prior incidents → synthesized a
CLAUDE.md rule: "…" → judge: would have prevented 3/3 (excerpt)*. Exit code
reflects the gate so CI can consume it.

### 15.5 A/B experiment matrix

Named experiment profiles vary the pipeline's judgment surfaces: per-stage
**model** (including Codex-integration users' models), **reasoning effort**,
and **prompt version** (prompts are versioned files precisely so arms can pin
them — for the Triager, Curator, Synthesist, and the judge itself). The
runner executes the same corpus per arm and emits a comparative report
(metric deltas, cost deltas, stability deltas). This is how "should the
Synthesist be Opus?" or "does prompt v2 reduce fragmentation?" become
measurements instead of opinions. Prompt changes re-run the replay baseline
like code regressions (prompts are second-class code nowhere in this repo).

### 15.6 Honest limits

Evals measure the pipeline on a synthetic corpus; they do not certify
performance on any individual's real transcripts, and live-mode scores vary
with model versions. The report says so. The corpus is also a public target —
overfitting prompts to the corpus is a known hazard; the A/B runner's held-out
flag (`--corpus <alt>`) and corpus versioning exist to keep us honest.

## 16. Build order

1. **Sprint 1 — Substrate:** repo scaffold, ledger, Claude Code adapter +
   defensive parser, archiver + SessionEnd hook, backfill, scanner + vendored
   lexicons, meter v0 (stats + HTML). *System is already useful here (the meter).*
2. **Sprint 2 — Judgment:** `llm.py` (claude -p wrapper + schemas + run log),
   Triager, Curator (core pass, then gardening + fragmentation telemetry),
   pump & scheduling.
3. **Sprint 3 — Remedies:** Synthesist + remedy routing, Gate (`review`
   policy) + install/rollback substrate, notifications, `/s2s` companion skill.
4. **Sprint 4 — The loop:** Auditor, revise/retire, autonomy policy + guardrails,
   Codex adapter, docs/README/CI/packaging, public release hygiene.
5. **Sprint 5 — Eval harness (§15):** golden corpus + annotations, sandboxed
   three-mode runner, deterministic scorers (convergence, spam precision,
   stability), LLM-judge rubrics + counterfactual grading, greenlight report,
   A/B experiment matrix.

The sprint milestones and PR-scoped issues in the GitHub tracker are the
authoritative decomposition of this build order.
