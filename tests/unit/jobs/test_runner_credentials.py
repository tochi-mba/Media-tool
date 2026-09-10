"""Resolving a caller's login while their download runs.

The rules being pinned here are about *when* and *how long*: at the start of each
attempt, never at submit, never on the job, and gone when the job is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from media_tool.core.config import Settings
from media_tool.core.keyring.credentials import Caller, FormSecrets
from media_tool.domain.errors import (
    CredentialNotFoundError,
    KeyringUnavailableError,
    ReauthenticationRequiredError,
)
from media_tool.domain.jobs import Job, JobStatus
from media_tool.domain.media import MediaQuery
from media_tool.jobs.runner import DownloadJobRunner
from media_tool.jobs.store import InMemoryJobStore
from media_tool.storage.local import LocalArtifactStore
from tests.fakes.accounts import ALICE
from tests.fakes.clock import FakeClock
from tests.fakes.provider import FakeProvider

if TYPE_CHECKING:
    from pathlib import Path

SERVICE = "somesite"
SECRETS = FormSecrets(service=SERVICE, fields={"username": "eve", "password": "hunter2"})


class RecordingCredentials:
    """A credential source that records what it was asked and answers as told."""

    def __init__(
        self, *, answer: FormSecrets = SECRETS, fail_with: Exception | None = None
    ) -> None:
        self.answer = answer
        self.fail_with = fail_with
        self.calls: list[tuple[str, str, str]] = []

    async def form_secrets(self, *, user_token: str, profile: str, service: str) -> FormSecrets:
        self.calls.append((user_token, profile, service))
        if self.fail_with is not None:
            raise self.fail_with
        return self.answer


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def jobs(clock: FakeClock) -> InMemoryJobStore:
    return InMemoryJobStore(clock=clock)


@pytest.fixture
def artifacts(tmp_path: Path, clock: FakeClock) -> LocalArtifactStore:
    return LocalArtifactStore(root=tmp_path / "artifacts", clock=clock, max_file_bytes=1_000_000)


def make_runner(
    provider: FakeProvider,
    jobs: InMemoryJobStore,
    artifacts: LocalArtifactStore,
    clock: FakeClock,
    credentials: RecordingCredentials | None,
    **overrides: object,
) -> DownloadJobRunner:
    base: dict[str, object] = {
        "require_authentication": False,
        "max_attempts": 1,
        "backoff_base_seconds": 0.01,
        "backoff_max_seconds": 0.01,
        "download_timeout_seconds": 5,
    }
    settings = Settings(_env_file=None, **{**base, **overrides})  # type: ignore[call-arg,arg-type]
    return DownloadJobRunner(
        provider=provider,
        job_store=jobs,
        artifact_store=artifacts,
        clock=clock,
        settings=settings,
        credentials=credentials,
        sleeper=_no_sleep,
        jitter=lambda delay: delay,
    )


async def _no_sleep(_seconds: float) -> None:
    return None


def make_job(clock: FakeClock, name: str = "Dune") -> Job:
    return Job.create(account=ALICE, queries=[MediaQuery.create(name=name)], now=clock.now())


CALLER = Caller(token="a-user-token", profile="work")  # noqa: S106 - a fixture


class TestResolving:
    async def test_a_provider_that_needs_no_login_is_given_none(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider()
        credentials = RecordingCredentials()
        runner = make_runner(provider, jobs, artifacts, clock, credentials)
        job = make_job(clock)
        await jobs.add(job)

        await runner.run(job, caller=CALLER)

        assert credentials.calls == []
        assert provider.secrets_seen == [None]

    async def test_a_provider_that_needs_one_is_given_it(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(login_service=SERVICE)
        credentials = RecordingCredentials()
        runner = make_runner(provider, jobs, artifacts, clock, credentials)
        job = make_job(clock)
        await jobs.add(job)

        await runner.run(job, caller=CALLER)

        assert job.status is JobStatus.SUCCEEDED
        assert provider.secrets_seen == [SECRETS]

    async def test_it_asks_with_the_callers_own_token_and_profile(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(login_service=SERVICE)
        credentials = RecordingCredentials()
        runner = make_runner(provider, jobs, artifacts, clock, credentials)
        job = make_job(clock)
        await jobs.add(job)

        await runner.run(job, caller=CALLER)

        assert credentials.calls == [("a-user-token", "work", SERVICE)]

    async def test_it_resolves_again_on_every_attempt(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        # A retry can happen long after the first try. Reusing the credential resolved
        # then would be reusing a token that may have expired since.
        provider = FakeProvider(login_service=SERVICE, fail_times={"unknown:dune": 1})
        credentials = RecordingCredentials()
        runner = make_runner(provider, jobs, artifacts, clock, credentials, max_attempts=2)
        job = make_job(clock)
        await jobs.add(job)

        await runner.run(job, caller=CALLER)

        assert len(credentials.calls) == 2


class TestCredentialFailures:
    async def test_an_expired_token_fails_the_item_as_reauthenticate(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        # Distinct from every other failure, because the fix is distinct: sign in again.
        provider = FakeProvider(login_service=SERVICE)
        credentials = RecordingCredentials(
            fail_with=ReauthenticationRequiredError("sign in again and resubmit")
        )
        runner = make_runner(provider, jobs, artifacts, clock, credentials)
        job = make_job(clock)
        await jobs.add(job)

        await runner.run(job, caller=CALLER)

        assert job.items[0].error is not None
        assert job.items[0].error.code == "reauthenticate"

    async def test_a_missing_credential_says_so(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(login_service=SERVICE)
        credentials = RecordingCredentials(fail_with=CredentialNotFoundError("not connected"))
        runner = make_runner(provider, jobs, artifacts, clock, credentials)
        job = make_job(clock)
        await jobs.add(job)

        await runner.run(job, caller=CALLER)

        assert job.items[0].error is not None
        assert job.items[0].error.code == "credential_missing"

    async def test_neither_is_retried(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        # Asking a second time with the same expired token cannot succeed, and trying
        # spends the caller's rate allowance on a foregone conclusion.
        provider = FakeProvider(login_service=SERVICE)
        credentials = RecordingCredentials(fail_with=ReauthenticationRequiredError("expired"))
        runner = make_runner(provider, jobs, artifacts, clock, credentials, max_attempts=3)
        job = make_job(clock)
        await jobs.add(job)

        await runner.run(job, caller=CALLER)

        assert len(credentials.calls) == 1

    async def test_keyring_being_down_is_retried(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        # The one credential failure worth trying again: an outage passes.
        provider = FakeProvider(login_service=SERVICE)
        credentials = RecordingCredentials(fail_with=KeyringUnavailableError("down"))
        runner = make_runner(provider, jobs, artifacts, clock, credentials, max_attempts=3)
        job = make_job(clock)
        await jobs.add(job)

        await runner.run(job, caller=CALLER)

        assert len(credentials.calls) == 3
        assert job.items[0].error is not None
        assert job.items[0].error.code == "provider_unavailable"

    async def test_a_site_needing_a_login_without_keyring_says_exactly_that(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        # The anonymous mode. Failing with "provider error" would send somebody looking
        # at the browser instead of at their configuration.
        provider = FakeProvider(login_service=SERVICE)
        runner = make_runner(provider, jobs, artifacts, clock, None)
        job = make_job(clock)
        await jobs.add(job)

        await runner.run(job, caller=None)

        assert job.items[0].error is not None
        assert job.items[0].error.code == "credential_missing"
        assert "without keyring" in job.items[0].error.message

    async def test_a_caller_with_no_token_cannot_drive_a_login(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(login_service=SERVICE)
        credentials = RecordingCredentials()
        runner = make_runner(provider, jobs, artifacts, clock, credentials)
        job = make_job(clock)
        await jobs.add(job)

        await runner.run(job, caller=None)

        assert credentials.calls == []
        assert job.items[0].error is not None
        assert job.items[0].error.code == "credential_missing"


class TestSecretsAreNotKept:
    async def test_no_secret_reaches_the_stored_job(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        # The job is stored, read back, and rendered into responses. Walking the whole
        # record is the only way to be sure nothing rode along into all three.
        provider = FakeProvider(login_service=SERVICE)
        runner = make_runner(provider, jobs, artifacts, clock, RecordingCredentials())
        job = make_job(clock)
        await jobs.add(job)

        await runner.run(job, caller=CALLER)

        stored = repr(await jobs.get(job.job_id, account=ALICE))
        assert "hunter2" not in stored
        assert "a-user-token" not in stored

    def test_a_caller_does_not_render_its_token(self) -> None:
        assert "a-user-token" not in repr(CALLER)
        assert "work" in repr(CALLER)
