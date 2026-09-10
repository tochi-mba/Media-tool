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
carry a stable `code` (`not_found`, `timeout`, …) rather than prose to be parsed.

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
- **No authentication.** The service drives a browser and writes files. Do not expose it
  beyond localhost or a trusted network without putting auth in front of it. See
  [operations.md](operations.md).
- **Jobs are in-memory.** A restart loses them, so an assistant holding a job id from before
  a deploy gets a `404`. [ADR-0003](adr/0003-in-memory-job-store.md) has the swap plan.
