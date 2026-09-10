# Playwright's own image, so the Chromium build and its system libraries already match
# the pinned Playwright version. Building Chromium's dependency list by hand is a
# reliable source of "works locally, fails in the container".
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, in their own layer: application edits then rebuild in seconds
# rather than re-resolving the whole tree.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --all-extras --no-install-project --no-dev

COPY src/ src/
COPY recipes/ recipes/
RUN uv sync --all-extras --no-dev

# Artifacts are written at runtime and must not live in the image layers.
RUN mkdir -p /var/lib/media-tool/artifacts && chown -R pwuser:pwuser /var/lib/media-tool /app
VOLUME ["/var/lib/media-tool/artifacts"]

USER pwuser

ENV MEDIA_TOOL_HOST=0.0.0.0 \
    MEDIA_TOOL_PORT=8000 \
    MEDIA_TOOL_ARTIFACT_DIR=/var/lib/media-tool/artifacts \
    MEDIA_TOOL_LOG_FORMAT=json

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthy', timeout=4).status == 200 else 1)"

CMD ["media-tool"]
