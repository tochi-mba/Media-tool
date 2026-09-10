# 0007 — API conventions, and operation ids as public contract

**Status:** Accepted

## Context

This is the first of several APIs, and the whole surface is intended to sit behind an MCP
server so an AI assistant can call it as tools. Without conventions fixed early, API #2 gets
re-decided from scratch and the tool surface becomes inconsistent.

## Decision

- Version prefix `/v1`, plural resource nouns.
- Every route declares `operation_id` as snake_case `verb_noun`. **These are public API** —
  they become MCP tool names.
- Every route declares a `summary` and a `description` written for a model to read: when to
  use it and what comes back.
- Every failure is RFC 9457 `application/problem+json`, mapped in exactly one place.
- Request bodies set `extra="forbid"`.
- Routers are registered in a `ROUTERS` registry; the app factory mounts them.

A contract test pins the exact set of operation ids and fails if any operation loses its
summary or description.

## Consequences

A new API inherits problem+json, request ids, access logging, and versioning by being added
to one tuple, so it starts consistent rather than having to remember to be.

Making operation ids contract means renaming one breaks every configured tool — which is why
the test pins them rather than leaving it to review. Descriptions being tested for existence
sounds pedantic, but a tool with no description is a tool a model will misuse.

`extra="forbid"` matters more for model callers than human ones: an assistant inventing
`quality: "1080p"` gets a `422` naming the field instead of a silent success that ignored it.

The costs: operation ids can never be tidied up later, and the tests fail for stylistic
reasons — an under-length description is a build failure. Both are deliberate. The alternative
is discovering the inconsistency once tools are configured against it.

## Known gap

No `Idempotency-Key` support. Assistants retry, and a retried submission currently creates a
second job. Duplicates *within* a request are deduplicated; across requests they are not. See
[docs/mcp.md](../mcp.md).
