# Media Tool

An async HTTP service that takes a list of media items — `{name, season?, episode?, year?}` — and
fetches a file for each one by driving a **headless Chromium browser that clicks the download
itself**. No human at the keyboard.

Work is submitted as a job, runs in the background under a concurrency cap, and is polled (or
long-polled) for results. Captured files are hashed, size-capped, stored, and served back.

```bash
curl -sS -X POST localhost:8000/v1/downloads \
  -H 'content-type: application/json' \
  -d '{"items": [{"name": "Severance", "season": 1, "episode": 3},
                 {"name": "Dune", "year": 2021}]}'
# 202 Accepted, Location: /v1/downloads/6f1c…
```

## Status

v1 ships the full pipeline end-to-end: validation, job orchestration, artifact storage, and a
working Playwright download adapter. The default provider is a deterministic **stub**, so the
service runs and is fully exercisable without a browser. Point it at a real site by writing a
[site recipe](docs/api.md) and setting `MEDIA_TOOL_PROVIDER=browser`.

## Quickstart

```bash
make install     # uv sync --all-extras --group dev
make check       # lint + strict types + layering contracts + tests at 100% coverage
make run         # http://localhost:8000  (docs at /docs)
```

```bash
curl -s localhost:8000/healthy | jq
```

## Documentation

| Document | What's in it |
| --- | --- |
| [AGENTS.md](AGENTS.md) | **Start here when changing the code.** Project map, invariants, commands, and step-by-step recipes for adding an API, a provider, or a site recipe. |
| [docs/architecture.md](docs/architecture.md) | The ports-and-adapters layout and why the layering is enforced in CI. |
| [docs/api.md](docs/api.md) | The full HTTP contract and the site-recipe schema. |
| [docs/mcp.md](docs/mcp.md) | Fronting this API with an MCP server so an assistant can call it as tools. |
| [docs/testing.md](docs/testing.md) | The TDD loop, the coverage policy, and how the browser is tested without flakes. |
| [docs/operations.md](docs/operations.md) | Configuration, deployment, limits, and the security posture. |
| [docs/adr/](docs/adr/) | Why each significant decision was made. |

## License

MIT — see [LICENSE](LICENSE).
