# Contributing

Start each change from the current `staging` branch and open its pull request back to
`staging`. Keep a PR scoped to one issue; split unrelated cleanup into a follow-up.

`docs/NORTHSTAR.md` is the design authority. If implementation must deviate from it,
update that document in the same PR and call out the deviation in the PR description.

The runtime stays zero-dependency: use the Python standard library in `src/s2s/` and
keep developer-only tools in the dev extra or CI. Test fixtures must be synthetic;
never commit real transcripts, source code from transcripts, secrets, or identifying
paths. Before adding a ledger migration, check `SCHEMA_VERSION` and the numbered
`MIGRATIONS` map again after rebasing so two branches do not claim the same number.

Run the applicable tests and lint locally, document any environment limitation, and
keep generated output and dependency directories out of commits.

- Release bumps must update BOTH `pyproject.toml` and `src/s2s/__init__.py.__version__` (the source fallback); the version test mirrors the CLI's resolution so CI catches a miss.
