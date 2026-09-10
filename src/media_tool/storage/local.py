"""Local-filesystem artifact storage.

Layout::

    <root>/.staging/<uuid>        in-progress captures, invisible to readers
    <root>/<job_id>/<index>/<filename>

Giving every item its own directory means two items in a batch can produce files with
the same name without either one clobbering the other, and it makes lookup a direct
path build rather than a search.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from media_tool.domain.artifacts import DownloadArtifact
from media_tool.domain.errors import ArtifactNotFoundError, ArtifactTooLargeError
from media_tool.storage.base import StorageHealth

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from media_tool.core.clock import Clock

DEFAULT_FILENAME = "download.bin"
"""Used when a site supplies a name that sanitizes away to nothing."""

MAX_FILENAME_BYTES = 255
"""The limit on every filesystem worth supporting."""

STAGING_DIR_NAME = ".staging"
HASH_CHUNK_BYTES = 1024 * 1024

_SEPARATORS = re.compile(r"[/\\]")
_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


def sanitize_filename(suggested: str) -> str:
    """Reduce a site-supplied filename to something safe to write.

    The name is only ever used as a single path segment, so every route out of that
    segment is closed: directory separators are dropped along with everything before
    them, control characters are stripped, and leading dots are removed so the result
    can be neither a traversal nor a hidden file.
    """
    name = unicodedata.normalize("NFC", suggested)
    name = _SEPARATORS.split(name)[-1]
    # Whitespace first so a newline becomes a space rather than being deleted and
    # silently gluing two words together; then strip what remains of the control range.
    name = _WHITESPACE.sub(" ", name)
    name = _UNSAFE.sub("", name)
    name = name.strip()
    name = name.lstrip(".").strip()

    if not name:
        return DEFAULT_FILENAME
    return _truncate(name)


def _truncate(name: str) -> str:
    """Trim to the filesystem limit, keeping the extension so the file stays openable."""
    if len(name.encode()) <= MAX_FILENAME_BYTES:
        return name

    stem, dot, suffix = name.rpartition(".")
    suffix = f"{dot}{suffix}" if dot else ""
    budget = MAX_FILENAME_BYTES - len(suffix.encode())
    trimmed = stem.encode()[:budget].decode(errors="ignore")
    return f"{trimmed}{suffix}"


@dataclass(slots=True)
class _LocalSink:
    """One staged capture on local disk."""

    staging_path: Path
    destination_dir: Path
    max_file_bytes: int
    clock: Clock
    started_at: datetime
    started_monotonic: float

    def commit(
        self,
        *,
        suggested_filename: str,
        content_type: str,
        source_url: str,
    ) -> DownloadArtifact:
        if not self.staging_path.exists():
            msg = "cannot commit: nothing was written to the staging path"
            raise ArtifactNotFoundError(msg)

        size_bytes = self.staging_path.stat().st_size
        if size_bytes > self.max_file_bytes:
            self.staging_path.unlink()
            msg = (
                f"download is {size_bytes} bytes, which exceeds the "
                f"{self.max_file_bytes} byte ceiling"
            )
            raise ArtifactTooLargeError(msg)

        digest = _sha256(self.staging_path)
        filename = sanitize_filename(suggested_filename)

        self.destination_dir.mkdir(parents=True, exist_ok=True)
        destination = self.destination_dir / filename
        # Same filesystem as the staging area, so this is an atomic rename: a reader
        # either sees no file or sees the whole file, never a partial one.
        self.staging_path.replace(destination)

        return DownloadArtifact(
            filename=filename,
            size_bytes=size_bytes,
            content_type=content_type,
            sha256=digest,
            source_url=source_url,
            duration_seconds=self.clock.monotonic() - self.started_monotonic,
            downloaded_at=self.started_at,
        )


def _sha256(path: Path) -> str:
    """Digest a file in chunks; downloads can be far larger than memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


class LocalArtifactStore:
    """Stores captured files under a single root directory."""

    def __init__(self, *, root: Path, clock: Clock, max_file_bytes: int) -> None:
        self.root = root.expanduser().resolve()
        self.staging_root = self.root / STAGING_DIR_NAME
        self._clock = clock
        self._max_file_bytes = max_file_bytes
        self.staging_root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def reserve(self, *, job_id: str, index: int) -> Iterator[_LocalSink]:
        """Open a staging slot, cleaning it up on any exit that did not publish."""
        staging_path = self.staging_root / uuid.uuid4().hex
        sink = _LocalSink(
            staging_path=staging_path,
            destination_dir=self.root / job_id / str(index),
            max_file_bytes=self._max_file_bytes,
            clock=self._clock,
            started_at=self._clock.now(),
            started_monotonic=self._clock.monotonic(),
        )
        try:
            yield sink
        finally:
            staging_path.unlink(missing_ok=True)

    def locate(self, *, job_id: str, index: int, filename: str) -> Path:
        """Resolve one stored artifact, refusing anything that addresses outside the root."""
        if not _SAFE_SEGMENT.match(job_id) or not _SAFE_SEGMENT.match(filename):
            msg = f"no artifact for job {job_id!r} item {index}"
            raise ArtifactNotFoundError(msg)

        candidate = (self.root / job_id / str(index) / filename).resolve()

        # Belt and braces: the segment check above should make this unreachable, but a
        # symlink inside the root could still point out of it.
        if self.root not in candidate.parents or not candidate.is_file():
            msg = f"no artifact for job {job_id!r} item {index}"
            raise ArtifactNotFoundError(msg)

        return candidate

    def purge_job(self, job_id: str) -> bool:
        """Delete one job's artifacts. Returns whether there was anything to delete.

        This is the primary retention path: the job store knows, from the injected
        clock, when a job has aged out, and this drops its files at the same moment.
        """
        if not _SAFE_SEGMENT.match(job_id):
            return False

        job_dir = self.root / job_id
        if not job_dir.is_dir():
            return False

        shutil.rmtree(job_dir, ignore_errors=True)
        return True

    def purge_expired(self, *, ttl_seconds: float) -> int:
        """Sweep job directories untouched for longer than ``ttl_seconds``.

        A backstop for orphans -- files whose job record vanished in a crash -- rather
        than the normal retention path, which is :meth:`purge_job`. It reads filesystem
        modification times because that is the only record an orphan still has; it is
        the one place in this class that does not consult the injected clock.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=ttl_seconds)
        removed = 0

        for job_dir in self.root.iterdir():
            if job_dir.name == STAGING_DIR_NAME or not job_dir.is_dir():
                continue
            if _modified_at(job_dir) < cutoff:
                shutil.rmtree(job_dir, ignore_errors=True)
                removed += 1

        return removed

    def health(self) -> StorageHealth:
        """Report writability and remaining space."""
        probe = self.staging_root / f".health-{uuid.uuid4().hex}"
        try:
            probe.touch()
            probe.unlink()
        except OSError:
            writable = False
        else:
            writable = True

        try:
            free_bytes = shutil.disk_usage(self.root).free
        except OSError:
            # The root can disappear underneath us -- an unmounted volume, say.
            free_bytes = 0

        return StorageHealth(writable=writable, free_bytes=free_bytes)


def _modified_at(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
