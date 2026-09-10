"""The download provider port.

A provider is handed one query and one staging sink, and is responsible for getting a
file into that sink by whatever means it has -- today a stub, and a headless browser
that clicks the download itself.

Providers raise :class:`ProviderError`; each subclass carries a stable ``code`` that
becomes the machine-readable reason on a failed item, so a client can branch on "not
found" versus "timed out" without parsing prose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from media_tool.core.keyring.credentials import FormSecrets
    from media_tool.domain.artifacts import DownloadArtifact
    from media_tool.domain.media import MediaQuery
    from media_tool.storage.base import ArtifactSink


class ProviderError(Exception):
    """A download could not be completed."""

    code = "provider_error"


class ProviderNotFoundError(ProviderError):
    """The provider looked and found nothing matching the query."""

    code = "not_found"


class ProviderTimeoutError(ProviderError):
    """The provider ran out of time."""

    code = "timeout"


class ProviderUnavailableError(ProviderError):
    """The provider itself is not usable -- no browser, bad recipe, site unreachable."""

    code = "provider_unavailable"


class ProviderCredentialError(ProviderError):
    """The login this download needs could not be used.

    Never retried. Every case underneath needs somebody to do something -- sign in
    again, connect an account, store another field -- and none of them is fixed by
    asking a second time.
    """

    code = "credential_error"


class ProviderReauthenticationError(ProviderCredentialError):
    """The caller's token expired while their download was still running."""

    code = "reauthenticate"


class ProviderCredentialMissingError(ProviderCredentialError):
    """No stored login for this site, or one missing a field the recipe needs."""

    code = "credential_missing"


class DownloadProvider(Protocol):
    """Fetches one file for one query."""

    @property
    def name(self) -> str:
        """Short identifier, reported by the health endpoint."""
        ...

    @property
    def requires_login(self) -> str | None:
        """The service whose stored login this provider needs, or ``None`` if none.

        Declared rather than discovered so that the caller can resolve the credential
        before any work starts, and so that a provider needing one cannot be run without
        it by accident.
        """
        ...

    async def download(
        self, *, query: MediaQuery, sink: ArtifactSink, secrets: FormSecrets | None = None
    ) -> DownloadArtifact:
        """Fetch ``query`` into ``sink`` and commit it.

        Implementations write to ``sink.staging_path`` and call ``sink.commit(...)``;
        the sink decides whether the result is acceptable.

        ``secrets`` is present exactly when :attr:`requires_login` named a service. It is
        for this call only: it is not stored, not logged, and not carried into the job.

        Raises:
            ProviderError: for any failure attributable to the fetch itself.
        """
        ...

    async def healthy(self) -> bool:
        """Whether the provider is currently able to serve downloads."""
        ...

    async def aclose(self) -> None:
        """Release whatever the provider holds open. Must be safe to call twice."""
        ...
