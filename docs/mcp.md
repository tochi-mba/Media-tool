# Fronting this API with MCP

The plan is to expose this service as tools an AI assistant can call. No MCP server is built
yet — this describes what has already been done to make that a wrapper rather than a rewrite,
and what to watch for when you build it.

## What is already in place

**Stable operation ids.** Every route declares one, and most OpenAPI→MCP bridges use it
verbatim as the tool name:

| `operation_id` | Good tool? |
| --- | --- |
| `get_health` | Marginal — useful for diagnostics, noise otherwise. |
| `create_download_job` | Yes. The primary action. |
| `get_download_job` | Yes. Pair it with `wait_seconds`. |
| `cancel_download_job` | Yes. |
| `fetch_download_file` | **No** — see [Files](#files) below. |

A contract test in `tests/integration/test_downloads.py` pins this exact set and fails if a
description goes missing, so tool names cannot drift silently.

There is no tool for reading a credential, and there must never be one. keyring's form-secrets
endpoint is the only place credential material is returned anywhere in this system; media-tool
consumes it and does not re-expose it. The same contract test asserts no operation id contains
`secret`, `credential`, `password` or `token`.

**Descriptions written for a model.** Route `summary` and `description` say when to use the
operation and what comes back, not just what it is called. Field descriptions carry the rules
a model cannot infer — that `season: 0` means specials, that `episode` may be given without a
season. Request bodies have `examples`.

**Bodies reject unknown fields.** A model inventing `quality: "1080p"` gets a `422` naming the
field rather than silently having it ignored — a fast, legible failure instead of a confusing
success.

**One call instead of a loop.** `get_download_job` accepts `wait_seconds` (0–60) and returns
as soon as the job finishes. Without it, an assistant has to implement polling, and it will
either poll too fast or give up too early.

**Bounded responses.** No file bytes in JSON, no unbounded lists, a per-request item cap. A
job view stays a predictable size in a context window.

**One error shape.** Every failure is problem+json with a stable `type`, and item failures
carry a stable `code` (`not_found`, `timeout`, `reauthenticate`, `credential_missing`, …)
rather than prose to be parsed. `429` carries `Retry-After`, so a model has an interval to obey
instead of a retry loop to invent.

**Per-caller identity, already.** Every `/v1` route takes the caller's keyring token and works
only on that account's jobs and files. The MCP server does not have to implement any of that —
it has to *forward the token*, and nothing else.

## Building the server

Two reasonable routes:

1. **Generate from OpenAPI.** FastMCP can ingest `/openapi.json` and produce tools directly.
   Cheapest path, and the reason the schema is written the way it is.
2. **A thin hand-written server.** More control over which operations become tools and how
   results are summarized. Import `create_app`'s container directly rather than making HTTP
   calls to yourself — there is no reason to pay for a network hop in-process.

Either way, `operation_id`s are the contract. Treat renaming one as a breaking change.

## Files

`fetch_download_file` returns bytes. That is the wrong shape for a tool result: it would
either blow up the context window or arrive base64-encoded and useless. Expose it as an MCP
**resource**, or return the link and let the client fetch it out of band. The API already
returns a `links.file` path per item for exactly this.

## Known gaps to close first

- **No idempotency key.** Assistants retry. A retried `create_download_job` currently creates
  a second job. Deduplication within a request is handled, but across requests is not — an
  `Idempotency-Key` header mapping to an existing job id is the obvious fix.
- **Each person's token has to reach the tool call.** The MCP server must forward the caller's
  own keyring token as `Authorization: Bearer …`, not a token of its own. Everything about
  isolation rests on it: a server that used one shared token would make everyone the same
  account. If your bridge cannot carry a per-caller header, that is the thing to fix before
  anything else.
- **Tokens expire in fifteen minutes.** An assistant holding one across a long conversation
  will find it stale. Mint one per burst of work rather than at session start; an item that
  fails with `reauthenticate` is telling you exactly this.
- **Jobs are in-memory.** A restart loses them, so an assistant holding a job id from before
  a deploy gets a `404`. [ADR-0003](adr/0003-in-memory-job-store.md) has the swap plan.
