# Testing

```bash
make check      # the gate: format, lint, strict types, layering, tests at 100% coverage
make test-live  # the real-Chromium tests, deselected from the default run
```

## Layout

| Directory | What lives there |
| --- | --- |
| `tests/unit/` | One module under test, collaborators faked. Fast. |
| `tests/integration/` | The real app over HTTP via `httpx` `ASGITransport`, lifespan included. |
| `tests/live/` | Real Chromium against a fixture site. Marked `live_browser`, opt-in. |
| `tests/fakes/` | Hand-written test doubles. |

## The 100% rule

Branch coverage, enforced at 100% in CI. The only exclusions are non-executable lines —
`if TYPE_CHECKING:`, bare `...` protocol bodies, `@overload`, the `__main__` guard — declared
in `pyproject.toml`. There is no `# pragma: no cover` anywhere in `src/`.

This is not coverage theatre. In practice it was the thing that surfaced real bugs: a
cancelled job that could be flipped to `failed` by a late result, a mid-capture browser crash
that escaped unclassified, a request id missing from exactly the response that tells you to
quote it. Each was found because a line was uncovered and asking "what would cover this?"
described a scenario nobody had thought about.

When a line resists coverage, the code is usually shaped wrong. Twice here the async tracer
lost the last line of a coroutine; both times the fix — extracting the tail into a named
function, and logging *before* the awaited call so a hung shutdown still leaves a record —
was an improvement on its own terms.

## No sleeping

There is no `sleep` in the suite. Time is injected everywhere: `Clock` for timestamps and
TTLs, an injectable sleeper and jitter function in the job runner. Where a test must wait for
background work, it waits on an `asyncio.Event`, never on a polling loop.

That is why retry backoff can be asserted exactly:

```python
assert runner.slept == [1.0, 2.0, 4.0, 4.0]  # exponential, capped at 4
```

## Fakes, not mocks

Fakes in `tests/fakes/` are hand-written and must satisfy the real Protocol — if a port
changes, they stop type-checking, which is how you find out.

A fake must also **reject what the real thing rejects**. Ours did not, and accepted a
`downloads_path` argument that real Playwright refuses; the bug survived a full suite of
green unit tests and was caught by the first live run. `FakeBrowser.new_context` now validates
its keyword arguments the way the driver does.

`FakeKeyring` follows the same rule and is the most important instance of it. It signs real
RS256 tokens with a real generated key, serves a real JWKS, and answers the internal endpoints
exactly as strictly as keyring does: both credentials or nothing, the account taken from the
user's token and from nowhere else, `404` rather than an empty answer when there is no such
credential. A fake more permissive than the real service would let this code depend on
something keyring refuses in production — and the whole isolation story rests on that contract.

## Everything runs authenticated

The `client` fixture carries a valid token, so every endpoint test exercises the path the
endpoint actually takes in production. `anonymous_client` and `other_client` exist for the two
cases that need something else: testing refusal, and testing that one account cannot reach
another's work.

`tests/integration/test_isolation.py` is its own file on purpose. Isolation bugs are silent —
nothing about the service's behaviour reveals one — so each test is named for the property it
holds rather than the code that holds it, and a refactor that breaks the property breaks a test
whose name says what was lost. One of them asserts the refusal is a `404` and **not** a `403`,
because `403` is the intuitive answer and the wrong one.

Two more sit in the contract test: nothing in the published schema may name an account, and no
operation id may contain `secret`, `credential`, `password` or `token`. Both exist to fail if
somebody later adds the obvious convenience.

## Nothing races the stub

The stub provider finishes a job in microseconds. A test that submits one and expects it to
still be running passes or fails by timing rather than by the rule it checks — so tests that
need work in flight seed it through the store instead of submitting it.

## The live browser tests

They drive real Chromium against a fixture site served from `127.0.0.1` on a random port by a
threaded `http.server`. No network, no proxy, no dependence on anyone else's uptime — so they
are reproducible rather than flaky, and they are the only thing that proves the fake is
telling the truth.

The fixture handler sets `Content-Disposition: attachment` on `.bin` files, which is what
turns a navigation into a download — the reason a real server is used rather than `file://`
URLs.

They do not contribute to the coverage number; the unit tests cover the same code paths with
the fake driver. They exist to verify the *contract* with Playwright.

**Never run `playwright install`** in this environment. Chromium is prebuilt at
`/opt/pw-browsers`; if the installed Playwright is newer than that build, point at it with
`MEDIA_TOOL_BROWSER__EXECUTABLE_PATH=/opt/pw-browsers/chromium`, which also bypasses the
revision check.

## Property-based tests

`tests/unit/domain/test_media.py` uses Hypothesis for the normalization and identity rules.
One property had to be narrowed: case folding is not round-trip stable across all of Unicode
(`"ı".upper()` is `"I"`, which folds to `"i"`), so the identity property is stated over an
alphabet where `upper()` is reversible. The comment in the test explains why, because the
narrower property is a real limitation of the key, not a test convenience.
