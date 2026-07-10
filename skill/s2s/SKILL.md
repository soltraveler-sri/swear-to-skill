---
name: s2s
description: Use when the user types /s2s or asks to review s2s proposals, see their swear meter / frustration dashboard, or check swear-to-skill status.
allowed-tools: Bash(s2s *),Read
---

<!-- s2s-skill-version: 1 -->

Run `s2s status` and `s2s proposals --json`. In 2–3 lines, summarize pipeline health and the meter headline, then list pending proposals by id, remedy type, and title (use the drafted content when it supplies one).

For review, present one pending proposal at a time: remedy type, drafted content, evidence quotes, dedup verdict, and confidence. Ask: approve, edit, reject, or skip. Never run s2s approve without the user having explicitly approved that specific proposal in this conversation. Summarizing is not approval. If ambiguous, ask. After an explicit approval, run `s2s approve <id>`; if the user asks to edit, run `s2s approve <id> --edit`; after a rejection with a reason, run `s2s reject <id> --reason "…"`.

For the dashboard, run `s2s meter` and present its clickable `file://` path. If there are no pending proposals, say so and offer status or the meter. Explain why a proposal exists only from the JSON evidence; do not inspect the ledger or make pipeline calls. The user may ask Claude to publish the local dashboard HTML as a private artifact if they wish; never do this automatically.
