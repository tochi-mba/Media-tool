# 0001 — Ports and adapters, enforced in CI

**Status:** Accepted

## Context

The service was specified with a stub provider today and headless-browser downloading as the
real goal. The obvious failure mode was building the stub straight into the request handler
and then rewriting everything when the browser arrived.

## Decision

Ports and adapters. `domain/` holds pure types and rules and imports nothing else in the
package. Concrete concerns — HTTP, Playwright, the filesystem — are adapters behind ports.

The direction is enforced by **import-linter contracts in `pyproject.toml`**, checked in CI:
the domain imports nothing internal, layers point inward, and Playwright may only be imported
by `providers/browser/`.

## Consequences

Swapping the stub for real browser downloading is `MEDIA_TOOL_PROVIDER=browser` plus a recipe
file. The job runner, the API, and storage are untouched by it.

The enforcement is the load-bearing part. A documented layering rule decays; a failing CI job
does not. It also caught a real mistake — the registry needs to construct the browser adapter,
which required deciding explicitly that it may import that module but still not Playwright
itself.

The cost is indirection: reading one request end to end means visiting several modules, and
adding a provider means touching a port, an adapter, and a registry entry. Worth it here
because the whole point is that the adapter behind the port is going to change.

## When to revisit

If this service only ever has one provider and one storage backend, the ports are ceremony.
That is not the trajectory — more APIs and an MCP front end are planned.
