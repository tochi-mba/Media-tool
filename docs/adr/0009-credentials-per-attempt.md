# 0009 — Credentials are resolved per attempt, never stored on a job

**Status:** Accepted

## Context

Some sites need a login before they will hand over a file. keyring stores those logins and
will return them, but only to a service presenting both its own token and the end user's — and
user tokens live **fifteen minutes** by default, while a browser download can comfortably
outlive that. keyring's own internal router notes that "a service holds these tokens for the
length of a job".

So there is a real question: resolve the credential once at submit time and keep it, or resolve
it each time it is needed?

## Decision

**Resolve at the start of each item's attempt.** Never at submit, and never on the job.

A job is stored, read back on every poll, and rendered into responses. A credential on one
would be a credential in all three, and in every log line that ever printed a job. The caller's
token travels with the running task instead, in a `Caller` value the runner holds for the
length of the job and nothing else sees.

Resolving per *attempt* rather than per item also means a retry after a long backoff re-checks
the token rather than reusing one that has since expired.

Three further rules follow:

- **An expired token is its own outcome.** The item fails with `reauthenticate` and a message
  saying to sign in again and resubmit — not a generic provider error, because the fix is
  entirely different from every other failure's.
- **Credential failures are not retried.** Signing in again, connecting an account, storing a
  missing field: all are things a person must do, and none is fixed by asking twice. Keyring
  being *down* is the one exception, because an outage passes.
- **The token is checked locally before it is forwarded.** keyring answers 401 both for a
  service token it does not recognise and for a user token it will not accept, and the wire
  does not distinguish them. Verifying our own copy first settles it: if the token was good a
  moment ago, a 401 is keyring refusing *us*, which is an operator's problem and not something
  the caller can fix by signing in again.

Login values are substituted in a separate pass, from a separate argument, and only into a
`fill` step's value. Never into the start URL, a selector, or a match term. A password in a URL
is a password in an access log, in a `Referer` header, and in somebody's browser history — and
the way to make that impossible is to have no code path that puts one there. The recipe model
rejects a `{secret.*}` placeholder anywhere else at load time.

## Consequences

Secrets exist in this service for the length of one attempt, in one local variable, and are
written nowhere. `FormSecrets` and `ResolvedCredential` do not render their values through
`repr`, `str`, or an f-string, because the realistic leak is a secret held in a local that a
traceback prints.

A job whose items take longer than the token's lifetime will fail the later ones with
`reauthenticate`. That is a real limitation and it is visible rather than silent. The
deployment knob is keyring's `KEYRING_ACCESS_TOKEN_TTL_SECONDS`; raising it weakens revocation,
which is the operator's trade to make knowingly.

Every attempt costs a call to keyring when the recipe needs a login. At this scale that is
nothing, and the alternative was holding credentials in memory for the length of a job.

## What would change this

If a job routinely outlives a token, the fix is a refresh path — media-tool asking keyring for
a fresh user token using a longer-lived grant — not storing the credential for longer.
