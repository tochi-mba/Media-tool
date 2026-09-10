# Architecture decision records

One file per decision that a future reader would otherwise re-litigate. Each says what was
decided, what it cost, and what would justify changing it.

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-ports-and-adapters.md) | Ports and adapters, enforced in CI | Accepted |
| [0002](0002-async-job-queue.md) | Asynchronous jobs rather than a synchronous call | Accepted |
| [0003](0003-in-memory-job-store.md) | In-memory job store for v1 | Accepted, expected to change |
| [0004](0004-browser-download-capture.md) | Capture downloads via the browser, not by re-fetching the URL | Accepted |
| [0005](0005-recipe-driven-sites.md) | Sites described by data, not code | Accepted |
| [0006](0006-two-phase-artifact-storage.md) | Two-phase artifact storage | Accepted |
| [0007](0007-api-conventions.md) | API conventions, and operation ids as public contract | Accepted |
| [0008](0008-delegated-identity.md) | Identity is delegated to keyring, and verified locally | Accepted |
| [0009](0009-credentials-per-attempt.md) | Credentials are resolved per attempt, never stored on a job | Accepted |
| [0010](0010-account-scoping.md) | Isolation is structural, and cross-account access is a 404 | Accepted |
