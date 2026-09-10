"""Providers that behave exactly as a test needs them to."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from media_tool.providers.base import ProviderError

if TYPE_CHECKING:
    from media_tool.domain.artifacts import DownloadArtifact
    from media_tool.domain.media import MediaQuery
    from media_tool.storage.base import ArtifactSink

PAYLOAD = b"fake-download-payload"


class FakeProvider:
    """Records every call and does whatever it was told to do.

    Written by hand rather than mocked so that it must satisfy the real
    :class:`~media_tool.providers.base.DownloadProvider` protocol -- if the port changes,
    this fails to type-check, which is how the change is meant to be noticed.
    """

    def __init__(
        self,
        *,
        fail_with: dict[str, Exception] | None = None,
        fail_times: dict[str, int] | None = None,
        hang_keys: frozenset[str] = frozenset(),
        healthy_result: bool = True,
    ) -> None:
        self._fail_with = fail_with or {}
        self._fail_times = dict(fail_times or {})
        self._hang_keys = hang_keys
        self._healthy = healthy_result

        self.calls: list[str] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self.closed = False

    @property
    def name(self) -> str:
        return "fake"

    async def healthy(self) -> bool:
        return self._healthy

    async def aclose(self) -> None:
        self.closed = True

    async def download(self, *, query: MediaQuery, sink: ArtifactSink) -> DownloadArtifact:
        self.calls.append(query.key)
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            # Yield so overlapping downloads actually overlap.
            await asyncio.sleep(0)

            if query.key in self._hang_keys:
                await asyncio.Event().wait()

            if (remaining := self._fail_times.get(query.key, 0)) > 0:
                self._fail_times[query.key] = remaining - 1
                msg = f"transient failure for {query.key}"
                raise ProviderError(msg)

            if (error := self._fail_with.get(query.key)) is not None:
                raise error

            sink.staging_path.write_bytes(PAYLOAD)
            return sink.commit(
                suggested_filename=f"{query.key.replace(':', '-')}.bin",
                content_type="application/octet-stream",
                source_url=f"fake://{query.key}",
            )
        finally:
            self.concurrent -= 1
