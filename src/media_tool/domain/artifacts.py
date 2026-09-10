"""What a successful download produced."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

DEFAULT_CONTENT_TYPE = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class DownloadArtifact:
    """Metadata describing one captured file.

    Deliberately separate from where the bytes live: the storage adapter owns the path,
    this describes the content. That separation is what lets local disk be swapped for
    object storage without the job aggregate noticing.
    """

    filename: str
    """Sanitized name safe to write to disk and to echo back in a header."""

    size_bytes: int
    content_type: str
    sha256: str
    """Hex digest computed while the bytes were being stored, not read back afterwards."""

    source_url: str
    """Where the file actually came from, which may differ from the page that was driven."""

    duration_seconds: float
    downloaded_at: datetime
