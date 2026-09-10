# 0004 — Capture downloads via the browser, not by re-fetching the URL

**Status:** Accepted

## Context

Once the browser has found the download link, there are two ways to get the bytes: extract the
`href` and fetch it with an HTTP client, or let the browser perform the download and capture
what it produces.

## Decision

Let the browser do it. The trigger click happens inside Playwright's `expect_download()`, and
the resulting file is handed to the artifact store.

## Consequences

The download inherits the browser's full session: cookies, referer, any token the page put on
the link, and whatever redirect chain or interstitial the site interposes. Re-fetching an
`href` with a separate client throws all of that away, which on many sites yields a login page
or a 403 rather than the file.

It also means there is no second definition of "how we make requests" to keep in sync with the
browser's.

The cost is that the bytes flow through Chromium, which is heavier than a plain HTTP GET, and
that we depend on `accept_downloads` behaving — a browser-level concern rather than an HTTP
one. That is the main reason the live tests exist: they perform a genuine automated download
against a local fixture server, which is how we know the capture path actually works.

This is also what makes the service worth building at all. Fetching a URL is a one-liner; the
hard part is arriving at a page in a state where the download is available, and only a real
browser session gets there.

## When to revisit

If a target site turns out to serve plain, unauthenticated, stable URLs, a direct fetch would
be cheaper. That would be a new provider behind the same port, not a change to this one.
