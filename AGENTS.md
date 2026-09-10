# AGENTS.md

Working notes for anyone — human or agent — changing this codebase. Read this before your
first edit. It is the single source of truth for how work is done here; `CLAUDE.md` just
points at it.

## What this service is

Takes a list of media items — `{name, season?, episode?, year?}` — and fetches a file for
each one by driving a headless Chromium browser that clicks the download itself. Work is
asynchronous: submit a batch, get a job id, poll or long-poll for results, then fetch the
captured files.

The HTTP surface is designed to be fronted by an **MCP server** later, so an AI assistant
can call it as tools. That is why route `operation_id`s and descriptions are treated as
contract, not decoration — see [Invariants](#invariants).

## Commands

| Command | What it does |
| --- | --- |
| `make install` | Create the venv and install everything, including the browser extra. |
| `make check` | **The gate.** Format check, lint, strict types, layering contracts, tests at 100% branch coverage. Run before every commit. |
| `make test` | Tests only. |
| `make test-live` | The real-Chromium tests, deselected from the default run. |
| `make fmt` | Format and auto-fix. |
| `make run` | Serve on :8000 with reload. Docs at `/docs`. |
| `make cov` | HTML coverage report in `htmlcov/`. |

Always run `make check` rather than a bare `pytest` — piping any of these to `head`/`tail`
in a shell chain masks the exit code, which is how a broken commit slips through.

## The map

```
src/media_tool/
  core/        config, clock, logging, request context, and the composition root
  domain/      pure business types: MediaQuery, Job, DownloadArtifact. Imports nothing internal.
  storage/     ArtifactStore port + local filesystem adapter. Where captured files live.
  providers/   DownloadProvider port + adapters
    stub.py      the default: fabricates a real file, needs no browser
    browser/     Playwright runtime, site recipes, and the real download provider
  jobs/        the job store (and its long-poll) and the runner that executes jobs
  api/         FastAPI app, routers, wire schemas, problem+json errors, middleware
```

Dependencies point inward: `api → jobs → providers → storage → domain`. `core` is a shared
kernel everything may use, except `domain`.

## Invariants

These are enforced mechanically. If you find yourself wanting to break one, change the
enforcement deliberately and say why in the commit message — do not work around it.

1. **The domain imports nothing from the rest of the package.** Enforced by an
   import-linter contract in `pyproject.toml`.
2. **Layers point inward.** Also an import-linter contract.
3. **Playwright is only imported by `providers/browser/`.** A third contract. The package
   must import cleanly with the `browser` extra uninstalled, which is why
   `playwright_runtime.py` imports it lazily through an injectable factory.
4. **Nothing reads the wall clock directly.** Every component that behaves differently over
   time takes a `Clock`. The one exception is the year bound in `domain/media.py`, which is
   a validation limit rather than orchestration timing, and says so.
5. **Coverage is 100% branch coverage, and the exclusions are only non-executable lines** —
   `if TYPE_CHECKING:`, bare `...` protocol bodies, `@overload`, the `__main__` guard. There
   is no `# pragma: no cover` in `src/`. If a line is hard to cover, that is usually the
   code telling you it is shaped wrong.
6. **Route `operation_id`s are public API.** They become MCP tool names. A contract test in
   `tests/integration/test_downloads.py` pins the exact set and requires every operation to
   carry a summary and a real description. Renaming one is a breaking change.
7. **Site-supplied filenames are hostile.** They arrive from `Content-Disposition` and are
   sanitized in `storage/local.py` before touching disk. Never build a path from one
   directly.
8. **Unexpected exceptions never reach the client verbatim.** Their text can carry paths or
   credentials. Callers get a request id to quote instead.

## How we work: TDD

Every change follows red → green → refactor, and each commit leaves `make check` passing.

1. Write the test first. It should fail for the reason you expect — check that it does.
2. Write the smallest implementation that passes.
3. Refactor with the test as a safety net.

Some notes earned the hard way in this codebase:

- **Name tests after the behaviour, not the method.**
  `test_a_definitive_miss_is_not_retried` beats `test_download_retry_2`.
- **Write the "why" in the test when it is not obvious.** A comment explaining that a miss
  is not retried *because looking again will not make the site have the file* is worth more
  than the assertion.
- **Don't assert an object is truthy.** `assert await run(...)` always passes. Assert
  something that could fail.
- **Fakes are hand-written and must satisfy the real Protocol** (`tests/fakes/`). If the port
  changes, they fail to type-check, which is how you find out. A fake must also *reject what
  the real thing rejects* — ours failed to, and let a bug through that only real Chromium
  caught.
- **Never sleep in a test.** Inject the clock, the sleeper, and the jitter function, or wait
  on an `asyncio.Event`. A polling loop with a sleep is a slow test today and a flaky one
  next month.
- **If coverage says a line is missed but you can prove it runs**, it is usually the async
  tracer losing the last line of a coroutine or generator. Restructure the code — extracting
  the tail into a named function, or moving a log line before the awaited call — rather than
  adding a pragma. Both fixes here made the code better.

## Recipe: add a new API

The one you will use most, since more APIs are coming.

1. Create `src/media_tool/api/routers/<name>.py` with a module-level
   `router = APIRouter(prefix="/v1/<plural-noun>", tags=["<name>"])`.
2. Add wire models in `src/media_tool/api/schemas/<name>.py`. Set
   `model_config = ConfigDict(extra="forbid")` on request bodies so an invented field is
   rejected instead of ignored. Give every field a `description` and every model an
   `examples` entry — a model reads these to decide whether and how to call the tool.
3. On every route set:
   - `operation_id` — snake_case `verb_noun`, stable forever (it is the MCP tool name),
   - `summary` — a short imperative phrase,
   - `description` — a real paragraph saying when to use it and what comes back,
   - `responses` — every failure a caller can provoke, typed as `Problem`.
4. Register it: add the module to `ROUTERS` in `api/routers/__init__.py`. That is the only
   wiring step; problem+json, request ids, access logging and versioning are inherited.
5. Raise domain errors from handlers. Map any new one to a status code in the `_DOMAIN_STATUS`
   table in `api/errors.py` — never build an error response in a handler.
6. Tests: an integration test per behaviour in `tests/integration/`, and extend the OpenAPI
   contract test with the new `operation_id`.

## Recipe: add a download provider

1. Write the adapter under `src/media_tool/providers/`, satisfying the `DownloadProvider`
   protocol in `providers/base.py`: `name`, `download(query, sink)`, `healthy()`, `aclose()`.
2. Write into `sink.staging_path` and finish with `sink.commit(...)`. Do not touch the
   filesystem yourself — the sink owns hashing, the size ceiling, and the atomic publish.
3. Raise `ProviderNotFoundError` for a genuine miss (it is deliberately **not** retried),
   `ProviderTimeoutError` or `ProviderUnavailableError` for anything transient (those are).
4. Add a value to `ProviderName` in `core/config.py` and an entry to `_BUILDERS` in
   `providers/registry.py`. Validate its configuration there so a misconfiguration fails at
   startup, not mid-job.
5. Tests: a port-conformance test (`checked: DownloadProvider = your_provider`), the happy
   path, and each error path. If it talks to the outside world, hide that behind a port and
   fake the port.

## Recipe: add a site recipe

A recipe is data — no code changes needed to support a new site.

1. Copy `recipes/example.json`. The schema is `SiteRecipe` in
   `providers/browser/recipes.py`; the module docstring lists the placeholders
   (`{query}`, `{name}`, `{episode_tag}`, `{year}`).
2. Find selectors by opening the site with `headless=False`
   (`MEDIA_TOOL_BROWSER__HEADLESS=false`) and using the browser's inspector.
3. Set `result_selector` and `match.text_contains` so the right result is *identified*
   before anything is clicked. Getting this wrong means downloading the wrong file and
   reporting success, which is worse than failing.
4. `download_trigger` is the click a human would make.
5. Test it without hitting the site: add a page to `tests/live/site/` and a case to
   `tests/live/`, or unit-test it against `FakePage` in `tests/fakes/browser.py`.
6. Run it: `MEDIA_TOOL_PROVIDER=browser MEDIA_TOOL_RECIPE_PATH=recipes/yours.json make run`.

## Environment gotchas

- **Never run `playwright install` here.** Chromium is prebuilt at `/opt/pw-browsers`, and
  `PLAYWRIGHT_BROWSERS_PATH` already points at it. If the installed Playwright is newer than
  that build, set `MEDIA_TOOL_BROWSER__EXECUTABLE_PATH=/opt/pw-browsers/chromium` — passing
  an explicit path also bypasses the revision check.
- `asyncio_mode = "auto"`, so `async def test_*` needs no marker.
- Live browser tests are deselected by default via `-m "not live_browser"` in `addopts`.
- `filterwarnings = ["error"]`: a new deprecation warning fails the suite. That is on
  purpose — fix it rather than filtering it.
- Nested settings use a double underscore: `MEDIA_TOOL_BROWSER__HEADLESS`.

## Commit conventions

Conventional-commit subject (`feat(scope):`, `fix(scope):`, `chore:`), imperative mood, no
trailing period. The body explains **why** — the tradeoff, the failure mode being prevented,
the thing that surprised you. A reader six months from now has the diff already; what they
lack is your reasoning.

## Definition of done

- [ ] Tests were written first, and failed first.
- [ ] `make check` passes: format, lint, strict types, layering contracts, 100% coverage.
- [ ] `make test-live` passes if you touched anything under `providers/browser/`.
- [ ] New behaviour is covered by a test named after the behaviour.
- [ ] Public HTTP changes: `operation_id`s stable, descriptions written for a model to read,
      contract test updated.
- [ ] Docs updated — this file for workflow, `docs/` for design, an ADR for a decision that
      future-you would otherwise re-litigate.
