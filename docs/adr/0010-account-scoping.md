# 0010 — Isolation is structural, and cross-account access is a 404

**Status:** Accepted

## Context

Once several people share one deployment, the question stops being "is this caller
authenticated" and becomes "is this *their* job". Getting that wrong is the failure mode that
matters most here: it is silent, it leaks somebody's private downloads, and nothing about the
service's behaviour reveals it.

Job ids are uuid4, so a caller cannot realistically guess another's. That is not the same as
checking, and only one of the two is a rule.

## Decision

**A job belongs to exactly one account, and carries it.** The account is on the aggregate
rather than passed alongside, so there is nowhere for a second opinion to live: a store cannot
file a job under an account the job disagrees with.

**Every read takes the account making it.** A job belonging to somebody else is reported
exactly as one that does not exist — same status, same error type, same message.

**Cross-account access is `404`, never `403`.** A `403` confirms the resource exists, which is
precisely what somebody probing for job ids wants to know. This is the counter-intuitive half
of the decision and has its own test, because "Forbidden" is the answer that reads as correct
and is not.

**The account is the outermost path segment in storage:**
`<root>/<account>/<job_id>/<index>/<filename>`. One account's files are not merely unreachable
through the API; they are somewhere else on disk. A path built for the wrong account does not
name another account's file — it names nothing. That is the difference between an
authorization bug leaking files and an authorization bug producing a 404.

`AccountId` is a parsed type, not a string. The parse is the single place that decides what an
account may be called: one safe path segment, so a `sub` claim of `../acct_bob` is refused at
the boundary rather than discovered later by the filesystem. It is deliberately not a `str`
subclass — comparing equal to a bare string is what would let one be passed where the type is
required.

**Every account has its own limits.** Concurrent jobs, bytes on disk, and request rate, each
per account. Without them, one person queuing a hundred downloads makes the service useless
for everyone else. The two quotas trip independently and the `429` names which, because
"wait for something to finish" and "delete some files" are different actions.

## Consequences

There are two checks where one would do — the store's and the filesystem's — and that is the
point: the second one holds when the first one has a bug.

The isolation tests live in their own file, one per resource, each named for the property
rather than the mechanism, so that a refactor which breaks the property breaks a test whose
name says what was lost.

`AGENTS.md` carries the resulting rule: **every new store method takes an `AccountId`**, and a
new resource is not done until there is a test asserting the other account gets a 404.

The service-wide job count in `/healthy` is the one number that is not per-account. It reports
on the process rather than on a caller and holds no ids, so it tells nobody anything about
anybody.

## What would change this

Nothing about a shared deployment. If media-tool ever became single-tenant per process, the
scoping would still be right and merely redundant — which is the correct direction for a
mistake in this area to point.
