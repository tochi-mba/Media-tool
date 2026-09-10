# AGENTS.md

Working notes for anyone — human or agent — changing this codebase. Read this before your
first edit. It is the single source of truth for how work is done here; `CLAUDE.md` just
points at it.

## What this service is

Takes a list of media items — `{name, season?, episode?, year?}` — and fetches a file for
each one by driving a headless Chromium browser that clicks the download itself. Work is
asynchronous: submit a batch, get a job id, poll or long-poll for results, then fetch the
captured files.

It is **multi-tenant**. Several people share one deployment, each with their own assistant.
media-tool authenticates nobody itself: callers present a token that **keyring** signed, this
service verifies it locally, and the account comes from that token and from nowhere else. Every
job, every file, and every limit belongs to one account. When a site needs a login, keyring
holds it and it is read at the moment it is used.

If you change anything that touches a job, a file, or a credential, read
[ADR-0008](docs/adr/0008-delegated-identity.md), [ADR-0009](docs/adr/0009-credentials-per-attempt.md)
and [ADR-0010](docs/adr/0010-account-scoping.md) first. Those three decisions are the ones an
otherwise reasonable change quietly undoes.

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
  core/        config, clock, logging, request context, per-account limits, composition root
    keyring/     token verification, the keyring HTTP client, credential resolution
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
9. **Every store method that reads somebody's data takes an `AccountId`.** Not a string, and
   not an implicit one from a context variable — a parameter, so the signature says the method
   behaves differently for different callers. A job carries its own account; a store may never
   file one under a different account than the job names.
10. **Cross-account access is `404`, never `403`.** A `403` confirms the resource exists, which
    is exactly what somebody guessing at ids wants to know. Every new resource needs a test
    asserting the other account gets a `404`, named for that property.
11. **The account comes from the caller's token and from nowhere else.** No request body, path
    or query parameter may name an account. A contract test walks the whole published schema
    and fails if one ever does.
12. **Credentials are resolved per attempt and stored nowhere** — not on a job, not in a
    response, not in a log record. `FormSecrets` and `ResolvedCredential` do not render their
    values; keep it that way, because the realistic leak is a secret in a local that a
    traceback prints.
13. **A `{secret.*}` placeholder may only be typed into a `fill` step's value.** Never a URL, a
    selector, or a match term. The recipe model rejects it at load, and the rejection is the
    point: a password in a URL is a password in an access log.

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
- **Never race the stub provider.** It finishes a job in microseconds, so a test that submits
  one and expects it to still be running passes or fails by timing. Seed the state you need
  through the store instead.
- **Every test runs authenticated**, against an in-process keyring that signs real RS256
  tokens (`tests/fakes/keyring.py`). Use the `client` fixture; `anonymous_client` and
  `other_client` exist for testing refusal and isolation. An endpoint test that quietly ran
  unauthenticated would stop testing what the endpoint does in production.
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
6. Take the account as a parameter: `account: AccountDep`. Pass it to every store call. If
   the route reads a resource, add a test that the other account gets a `404`.
7. Tests: an integration test per behaviour in `tests/integration/`, and extend the OpenAPI
   contract test with the new `operation_id`.

## Recipe: add a download provider

1. Write the adapter under `src/media_tool/providers/`, satisfying the `DownloadProvider`
   protocol in `providers/base.py`: `name`, `download(query, sink)`, `healthy()`, `aclose()`.
2. Write into `sink.staging_path` and finish with `sink.commit(...)`. Do not touch the
   filesystem yourself — the sink owns hashing, the size ceiling, and the atomic publish.
3. Raise `ProviderNotFoundError` for a genuine miss (it is deliberately **not** retried),
   `ProviderTimeoutError` or `ProviderUnavailableError` for anything transient (those are), and
   a `ProviderCredentialError` subclass when a login is the problem (never retried — every one
   of those needs a person to do something).
4. Implement `requires_login`. Return `None` unless the provider genuinely needs a stored login;
   returning a service name makes the runner resolve one before every attempt. A provider that
   needs none must **raise** if handed a credential anyway — being given one it did not ask for
   is a wiring mistake, and shrugging at it is how a secret ends up somewhere nobody meant.
5. Add a value to `ProviderName` in `core/config.py` and an entry to `_BUILDERS` in
   `providers/registry.py`. Validate its configuration there so a misconfiguration fails at
   startup, not mid-job.
6. Tests: a port-conformance test (`checked: DownloadProvider = your_provider`), the happy
   path, and each error path. If it talks to the outside world, hide that behind a port and
   fake the port.

## Recipe: add a site recipe

A recipe is data — no code changes needed to support a new site.

1. Copy `recipes/example.json`. The schema is `SiteRecipe` in
   `providers/browser/recipes.py`; the module docstring lists the placeholders
   (`{query}`, `{name}`, `{episode_tag}`, `{year}`).
2. Find selectors by opening the site with `headless=False`
   (`MEDIA_TOOL_BROWSER__HEADLESS=false`) and using the browser's inspector.
3. If the site needs a login, declare it — `"login": {"service": "<keyring service name>"}` —
   and type the values with `{secret.username}`, `{secret.password}`, or any other field name
   stored for that service. They may appear only in a `fill` step's `value`; anywhere else is
   rejected at load. A recipe must declare the login it types and type the login it declares.
4. Set `result_selector` and `match.text_contains` so the right result is *identified*
   before anything is clicked. Getting this wrong means downloading the wrong file and
   reporting success, which is worse than failing.
5. `download_trigger` is the click a human would make.
6. Test it without hitting the site: add a page to `tests/live/site/` and a case to
   `tests/live/`, or unit-test it against `FakePage` in `tests/fakes/browser.py`.
7. Run it: `MEDIA_TOOL_PROVIDER=browser MEDIA_TOOL_RECIPE_PATH=recipes/yours.json make run`.

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
- [ ] Anything touching a job, a file, or a credential: the account is a parameter, the
      cross-account case has a test that expects `404`, and no secret is reachable from a
      stored record, a response, or a log line.
- [ ] Docs updated — this file for workflow, `docs/` for design, an ADR for a decision that
      future-you would otherwise re-litigate.
