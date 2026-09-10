"""The artifact storage port.

Storage is expressed as a two-phase reservation rather than a "here are some bytes"
call, because the thing producing the bytes is a browser: Playwright writes a download
to a path it is given. The store hands out a staging path, the provider fills it, and
the store decides whether the result is acceptable and where it belongs.

That shape also means the size ceiling, the digest, and the atomic publish all live in
one place, and swapping local disk for object storage does not change the provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from pathlib import Path

    from media_tool.domain.artifacts import DownloadArtifact


@dataclass(frozen=True, slots=True)
class StorageHealth:
    """Whether the store can currently accept writes, and how much room is left."""

    writable: bool
    free_bytes: int


class ArtifactSink(Protocol):
    """One in-progress capture."""

    @property
    def staging_path(self) -> Path:
        """Where the producer should write. Not visible to readers until committed."""
        ...

    def commit(
        self,
        *,
        suggested_filename: str,
        content_type: str,
        source_url: str,
    ) -> DownloadArtifact:
        """Validate, hash, and publish what was staged.

        ``suggested_filename`` arrives from a remote site and is treated as hostile: it
        is sanitized, never used as a path.

        Raises:
            ArtifactTooLargeError: if the staged file exceeds the configured ceiling.
            ArtifactNotFoundError: if nothing was written to the staging path.
        """
        ...


class ArtifactStore(Protocol):
    """Where captured files live."""

    def reserve(self, *, job_id: str, index: int) -> AbstractContextManager[ArtifactSink]:
        """Open a staging slot for one item.

        Leaving the context without committing discards whatever was staged, so a failed
        or abandoned download never leaves a partial file behind.
        """
        ...

    def locate(self, *, job_id: str, index: int, filename: str) -> Path:
        """Resolve a stored artifact to a readable path.

        Raises:
            ArtifactNotFoundError: if it does not exist, or if the arguments try to
                address anything outside this store.
        """
        ...

    def link_artifact(
        self, *, job_id: str, source_index: int, target_index: int, filename: str
    ) -> DownloadArtifact:
        """Expose an already-stored artifact under a second item index."""
        ...

    def purge_job(self, job_id: str) -> bool:
        """Delete one job's artifacts. Returns whether there was anything to delete."""
        ...

    def purge_expired(self, *, ttl_seconds: float) -> int:
        """Sweep orphaned artifacts idle longer than ``ttl_seconds``. Returns how many went."""
        ...

    def health(self) -> StorageHealth:
        """Report whether the store can currently accept writes."""
        ...
