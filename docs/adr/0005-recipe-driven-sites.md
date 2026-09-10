# 0005 — Sites described by data, not code

**Status:** Accepted

## Context

No target site had been chosen when the browser provider was written. Every site has different
selectors, a different search URL, and a different way of presenting results.

## Decision

A `SiteRecipe` — validated JSON — describes the flow: where to start, what steps to perform,
how to recognize the right result, and what to click. The provider executes recipes and
contains nothing site-specific.

## Consequences

Supporting a new site is a JSON file and a test, not a release. Selectors drift constantly on
scraped sites; fixing one becomes a config change rather than a code change and a deploy.

Recipes are validated on load and the service **refuses to start** with a broken one. That is
deliberate: a bad recipe is a deployment mistake, and failing at startup is better than
accepting work that cannot possibly be done.

`match.text_contains` runs before the trigger is clicked. Without it the click would land on
whatever happened to be first, and the job would report success for the wrong file — worse
than a clean failure, because nothing signals it. A placeholder that resolves to empty does
not constrain, so `{episode_tag}` simply does not apply to a film.

The costs are real. A recipe is a small declarative language, and a declarative language grows:
today it has three actions, and a site needing conditional logic or pagination will not fit.
Recipes are also trusted input — one names the URLs to visit and the elements to click, so it
must be treated like code and never accepted from an untrusted source.

## When to revisit

When a site needs branching, retries within the flow, or multi-page traversal, stop extending
the recipe schema and write a dedicated provider for that site behind the same port. The
schema should stay small enough to read at a glance.
