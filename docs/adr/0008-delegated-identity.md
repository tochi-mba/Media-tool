# 0008 — Identity is delegated to keyring, and verified locally

**Status:** Accepted

## Context

media-tool had one global identity: every job and every file belonged to nobody in
particular. The goal is a handful of people — family and friends — each with their own
assistant, against one internet-facing deployment. That needs the service to know *who* is
asking and to hold each person's work apart from everyone else's.

keyring already answers "who is this" and "what credential does this person have for X". It
mints short-lived RS256 tokens and publishes its public keys at a well-known path.

The alternatives were: build accounts here (a second password database, a second place to get
password reset wrong, and a third when the next API arrives); or call keyring on every request
to check the token (a network round trip per request, and keyring becomes a hard dependency
for every read rather than a soft one).

## Decision

**media-tool authenticates nobody and mints nothing.** A caller presents a token keyring
signed. This service verifies it locally against keyring's published keys, pinning the issuer
and an audience of `media-tool`, and takes the account from the `sub` claim.

Consequences that follow from that, each deliberate:

- **The account comes from the token and from nowhere else.** No request body, path or query
  parameter names an account. A contract test walks the whole published schema and asserts
  that none ever will.
- **Verification is local.** Keys are fetched once and cached; an unknown key id earns one
  refetch, which is how key rotation is survived without a restart.
- **A revoked session stays valid until its token expires** — at most fifteen minutes by
  default. That is the trade keyring already made when it chose that lifetime, and inheriting
  it is cheaper than inventing a revocation channel between two services.
- **Keyring being unreachable is a 503, never a 401.** A caller with a perfectly good token
  must not be sent off to re-authenticate against a keyring that cannot answer either.
- **Cached keys are served through an outage.** The keys are public and change rarely; the
  outage is transient. Refusing good tokens for the duration would be strictly worse.
- **Every rejection says the same thing.** Which check failed — expiry, audience, signature,
  subject — is nobody's business, and a distinct message per failure is a free oracle for what
  a forged token needs to look like next.

Authentication can be switched off, and then every request is attributed to one fixed account.
That mode exists for a laptop. It is a separate implementation of the same port rather than a
flag, so nothing downstream branches on it; wiring it logs a warning and `/healthy` reports
`identity.mode = "disabled"`. `Settings` refuses to construct with authentication on and
keyring unconfigured, so the failure is at startup rather than one unexplained 503 per request.

## Consequences

There is exactly one account database for the whole system, and the next API inherits this
arrangement by verifying the same tokens.

media-tool cannot be run without keyring, which is a real coupling — and the honest one: a
service that cannot say who is asking has no business holding several people's files.

The audience pin means a token minted for another service cannot be replayed here. It also
means an operator who renames the service in keyring must change `MEDIA_TOOL_KEYRING_AUDIENCE`
to match, or every token is refused.

## What would change this

A revocation feed from keyring — a webhook, or a short-lived deny list — would remove the
fifteen-minute window without adding a per-request round trip. Worth doing if the window ever
matters more than the simplicity does.
