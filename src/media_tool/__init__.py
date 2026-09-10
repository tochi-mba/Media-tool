"""Media Tool: an async service for fetching media files via automated browser downloads.

The public surface is the FastAPI application built by
:func:`media_tool.api.app.create_app`. Everything else is internal and free to change,
with the exception of the HTTP contract documented in ``docs/api.md`` — route
``operation_id``s are treated as public because they become MCP tool names.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
