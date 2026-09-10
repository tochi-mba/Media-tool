"""The composition root.

Every adapter is chosen and wired here, once, and handed to the app. Nothing else
constructs its own dependencies -- which is what makes the whole service testable by
substitution and what keeps the choice of provider a configuration decision.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from media_tool.core.clock import SystemClock
from media_tool.core.keyring.authenticator import (
    Authenticator,
    KeyringAuthenticator,
    SingleAccountAuthenticator,
)
from media_tool.core.keyring.client import KeyringClient
from media_tool.core.keyring.tokens import TokenVerifier
from media_tool.core.limits import AccountQuotas, AccountRateLimiter
from media_tool.core.logging import get_logger
from media_tool.domain.accounts import AccountId
from media_tool.jobs.runner import DownloadJobRunner
from media_tool.jobs.store import InMemoryJobStore
from media_tool.providers.registry import build_provider
from media_tool.storage.local import LocalArtifactStore

if TYPE_CHECKING:
    import httpx

    from media_tool.core.clock import Clock
    from media_tool.core.config import Settings
    from media_tool.providers.base import DownloadProvider

logger = get_logger(__name__)

SHUTDOWN_DRAIN_SECONDS = 10.0
"""How long in-flight downloads get to finish before shutdown cancels them."""


@dataclass(slots=True)
class Container:
    """Everything the API needs, already wired together."""

    settings: Settings
    clock: Clock
    jobs: InMemoryJobStore
    artifacts: LocalArtifactStore
    provider: DownloadProvider
    runner: DownloadJobRunner
    authenticator: Authenticator
    quotas: AccountQuotas
    rate_limiter: AccountRateLimiter
    started_monotonic: float
    keyring: KeyringClient | None = None
    _sweeper: asyncio.Task[None] | None = None

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        clock: Clock | None = None,
        keyring_transport: httpx.AsyncBaseTransport | None = None,
    ) -> Container:
        """Construct every adapter named by ``settings``.

        Args:
            settings: the configuration to wire.
            clock: substituted by tests that need to control time.
            keyring_transport: substituted by tests, which serve keyring in-process
                rather than over a socket. The same seam the browser adapter uses for
                Playwright, and for the same reason: the alternative is a test suite
                that needs a second service running.
        """
        clock = clock or SystemClock()
        jobs = InMemoryJobStore(clock=clock)
        artifacts = LocalArtifactStore(
            root=settings.artifact_dir,
            clock=clock,
            max_file_bytes=settings.max_file_bytes,
        )
        provider = build_provider(settings)
        keyring, authenticator = _build_authentication(
            settings, clock=clock, transport=keyring_transport
        )

        return cls(
            settings=settings,
            clock=clock,
            jobs=jobs,
            artifacts=artifacts,
            provider=provider,
            runner=DownloadJobRunner(
                provider=provider,
                job_store=jobs,
                artifact_store=artifacts,
                clock=clock,
                settings=settings,
            ),
            authenticator=authenticator,
            quotas=AccountQuotas(jobs=jobs, artifacts=artifacts, settings=settings),
            rate_limiter=AccountRateLimiter(
                clock=clock,
                burst=settings.request_burst_per_account,
                per_second=settings.requests_per_second_per_account,
            ),
            keyring=keyring,
            started_monotonic=clock.monotonic(),
        )

    @property
    def uptime_seconds(self) -> float:
        return self.clock.monotonic() - self.started_monotonic

    def start_sweeper(self) -> None:
        """Begin evicting expired jobs and their files in the background."""
        self._sweeper = asyncio.create_task(self._sweep_forever(), name="retention-sweeper")

    async def aclose(self) -> None:
        """Shut everything down in dependency order."""
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None

        await self.runner.aclose(drain_timeout=SHUTDOWN_DRAIN_SECONDS)

        if self.keyring is not None:
            await self.keyring.aclose()

    async def sweep_once(self) -> int:
        """Evict expired jobs and delete their artifacts. Returns how many went.

        Job retention drives artifact retention, so a file never outlives the record
        that describes it.
        """
        purged = await self.jobs.purge_expired(ttl_seconds=self.settings.job_ttl_seconds)
        for job in purged:
            self.artifacts.purge_job(account=job.account, job_id=job.job_id)

        # Catches files whose job record died with a previous process.
        self.artifacts.purge_expired(ttl_seconds=self.settings.artifact_ttl_seconds)

        if purged:
            logger.info("jobs_purged", count=len(purged))
        return len(purged)

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(self.settings.job_sweep_interval_seconds)
            try:
                await self.sweep_once()
            except Exception:
                # A sweep failure must not kill the sweeper; the next tick tries again.
                logger.exception("sweep_failed")


def _build_authentication(
    settings: Settings,
    *,
    clock: Clock,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[KeyringClient | None, Authenticator]:
    """Choose how callers are identified, and say so in the log either way.

    The two modes are two objects rather than one object with a flag, so that nothing
    downstream has to ask which one it got.
    """
    if not settings.require_authentication:
        account = AccountId.parse(settings.anonymous_account)
        logger.warning(
            "authentication_disabled",
            account=str(account),
            detail="every request is attributed to one account; do not expose this",
        )
        return None, SingleAccountAuthenticator(account)

    # Guaranteed present: Settings refuses to construct with authentication on and
    # keyring unconfigured.
    base_url = str(settings.keyring_base_url)
    keyring = KeyringClient(
        base_url=base_url,
        service_token=settings.keyring_service_token,
        timeout_seconds=settings.keyring_timeout_seconds,
        transport=transport,
    )
    verifier = TokenVerifier(
        fetch_jwks=keyring.fetch_jwks,
        issuer=settings.keyring_issuer,
        audience=settings.keyring_audience,
        clock=clock,
        cache_ttl_seconds=settings.keyring_jwks_cache_ttl_seconds,
    )
    logger.info("authentication_enabled", keyring=base_url, audience=settings.keyring_audience)
    return keyring, KeyringAuthenticator(verifier)
