"""A provider that fabricates files instead of fetching them.

This is the default, and it is what makes the service runnable and fully testable with
no browser installed. It is not a mock: it produces a real file with real bytes through
the real storage path, so everything downstream of it is exercised for real.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from media_tool.domain.media import MediaKind
from media_tool.providers.base import ProviderError, ProviderNotFoundError

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet

    from media_tool.core.keyring.credentials import FormSecrets
    from media_tool.domain.artifacts import DownloadArtifact
    from media_tool.domain.media import MediaQuery
    from media_tool.storage.base import ArtifactSink

STUB_CONTENT_BYTES = 4096
STUB_CONTENT_TYPE = "application/octet-stream"
FILENAME_SUFFIX = ".stub.bin"


class StubDownloadProvider:
    """Produces deterministic placeholder files."""

    def __init__(self, *, missing: AbstractSet[str] | None = None) -> None:
        """Create the provider.

        Args:
            missing: canonical query keys that should fail with
                :class:`ProviderNotFoundError`. Lets callers exercise failure handling
                downstream without needing a flaky real provider.
        """
        self._missing = frozenset(missing or ())

    @property
    def name(self) -> str:
        return "stub"

    @property
    def requires_login(self) -> str | None:
        """Nothing to log in to. It fabricates files."""
        return None

    async def healthy(self) -> bool:
        return True

    async def aclose(self) -> None:
        """Nothing is held open."""

    async def download(
        self, *, query: MediaQuery, sink: ArtifactSink, secrets: FormSecrets | None = None
    ) -> DownloadArtifact:
        if secrets is not None:
            # Not merely unused: a provider handed a credential it did not ask for is a
            # wiring mistake, and quietly ignoring one is how a secret ends up somewhere
            # nobody meant it to be.
            msg = "the stub provider needs no login and will not be given one"
            raise ProviderError(msg)

        if query.key in self._missing:
            msg = f"no result for {query.name!r}"
            raise ProviderNotFoundError(msg)

        sink.staging_path.write_bytes(_content_for(query))

        return sink.commit(
            suggested_filename=_filename_for(query),
            content_type=STUB_CONTENT_TYPE,
            source_url=f"stub://{query.key}",
        )


def _content_for(query: MediaQuery) -> bytes:
    """Derive stable bytes from the query, so the same request always hashes the same."""
    seed = hashlib.sha256(query.key.encode()).digest()
    repeats = -(-STUB_CONTENT_BYTES // len(seed))  # ceiling division
    return (seed * repeats)[:STUB_CONTENT_BYTES]


def _filename_for(query: MediaQuery) -> str:
    """Name the file after what was asked for, so a directory listing is readable."""
    parts = ["-".join(query.name.casefold().split())]

    if query.kind is MediaKind.SERIES and (tag := query.episode_tag()) is not None:
        parts.append(tag.casefold())
    elif query.year is not None:
        parts.append(str(query.year))

    return f"{'-'.join(parts)}{FILENAME_SUFFIX}"
