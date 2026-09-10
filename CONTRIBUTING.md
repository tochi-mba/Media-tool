# Contributing

## Setup

```bash
make install          # venv + all dependencies, including the browser extra
make check            # confirm a clean checkout is green before you change anything
```

Optionally `uv run pre-commit install` to run the fast checks on every commit.

## The loop

1. **Write the failing test first.** Confirm it fails for the reason you expect.
2. Write the smallest implementation that passes it.
3. Refactor.
4. `make check` — and `make test-live` if you touched `providers/browser/`.
5. Commit.

`make check` runs format, lint, strict types, the architectural contracts, and the tests at
100% branch coverage. It is the same thing CI runs; there should be no surprises.

## What reviewers look for

- The test is named after the behaviour, and would fail if the behaviour regressed.
- The commit body explains *why*, not what. The diff already says what.
- Cross-layer changes respect the import contracts. If you need to change a contract, do it
  deliberately and say why.
- New HTTP surface follows the conventions in
  [AGENTS.md](AGENTS.md#recipe-add-a-new-api) — stable operation ids, descriptions written
  for a model to read.
- A decision a future reader would question has an ADR in `docs/adr/`.

## Before you ask "why is it like this?"

[AGENTS.md](AGENTS.md) covers workflow and the invariants that are mechanically enforced.
`docs/adr/` covers the decisions and what would justify changing them. Both are meant to be
edited — if something there is wrong or stale, fixing it is a welcome change on its own.
