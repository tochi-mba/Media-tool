"""The job aggregate: what a batch of downloads is doing and how it ends."""

from __future__ import annotations

from datetime import timedelta

import pytest

from media_tool.domain.artifacts import DownloadArtifact
from media_tool.domain.errors import InvalidJobTransitionError, JobItemNotFoundError
from media_tool.domain.jobs import ItemStatus, Job, JobStatus
from media_tool.domain.media import MediaQuery
from tests.fakes.accounts import ALICE
from tests.fakes.clock import EPOCH, FakeClock


def make_job(*names: str, clock: FakeClock | None = None) -> Job:
    clock = clock or FakeClock()
    queries = [MediaQuery.create(name=name) for name in names or ("Dune",)]
    return Job.create(account=ALICE, queries=queries, now=clock.now())


def make_artifact(filename: str = "dune.bin") -> DownloadArtifact:
    return DownloadArtifact(
        filename=filename,
        size_bytes=1024,
        content_type="application/octet-stream",
        sha256="0" * 64,
        source_url="https://example.test/dune",
        duration_seconds=1.5,
        downloaded_at=EPOCH,
    )


class TestCreation:
    def test_a_new_job_is_queued_with_pending_items(self) -> None:
        job = make_job("Dune", "Severance")

        assert job.status is JobStatus.QUEUED
        assert job.item_count == 2
        assert [item.status for item in job.items] == [ItemStatus.PENDING] * 2

    def test_items_keep_the_order_they_were_submitted_in(self) -> None:
        job = make_job("A", "B", "C")

        assert [item.query.name for item in job.items] == ["A", "B", "C"]

    def test_each_job_gets_a_distinct_id(self) -> None:
        assert make_job().job_id != make_job().job_id

    def test_a_job_needs_at_least_one_item(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            Job.create(account=ALICE, queries=[], now=EPOCH)


class TestLifecycle:
    def test_starting_moves_a_queued_job_to_running(self) -> None:
        job = make_job()

        job.start(now=EPOCH)

        assert job.status is JobStatus.RUNNING
        assert job.started_at == EPOCH

    def test_a_job_cannot_start_twice(self) -> None:
        job = make_job()
        job.start(now=EPOCH)

        with pytest.raises(InvalidJobTransitionError, match="running"):
            job.start(now=EPOCH)

    def test_every_item_succeeding_makes_the_job_succeed(self) -> None:
        job = make_job("A", "B")
        job.start(now=EPOCH)

        job.record_success(0, make_artifact(), now=EPOCH)
        job.record_success(1, make_artifact(), now=EPOCH)

        assert job.status is JobStatus.SUCCEEDED
        assert job.completed_at == EPOCH

    def test_every_item_failing_makes_the_job_fail(self) -> None:
        job = make_job("A", "B")
        job.start(now=EPOCH)

        job.record_failure(0, code="not_found", message="nope", now=EPOCH)
        job.record_failure(1, code="timeout", message="slow", now=EPOCH)

        assert job.status is JobStatus.FAILED

    def test_a_mix_of_outcomes_is_partial(self) -> None:
        job = make_job("A", "B")
        job.start(now=EPOCH)

        job.record_success(0, make_artifact(), now=EPOCH)
        job.record_failure(1, code="not_found", message="nope", now=EPOCH)

        assert job.status is JobStatus.PARTIAL

    def test_the_job_stays_running_until_the_last_item_lands(self) -> None:
        job = make_job("A", "B")
        job.start(now=EPOCH)

        job.record_success(0, make_artifact(), now=EPOCH)

        assert job.status is JobStatus.RUNNING
        assert job.completed_at is None


class TestCancellation:
    def test_cancelling_marks_outstanding_items_cancelled(self) -> None:
        job = make_job("A", "B")
        job.start(now=EPOCH)
        job.record_success(0, make_artifact(), now=EPOCH)

        job.cancel(now=EPOCH)

        assert job.status is JobStatus.CANCELLED
        assert job.items[0].status is ItemStatus.SUCCEEDED
        assert job.items[1].status is ItemStatus.CANCELLED

    def test_cancelling_a_queued_job_works(self) -> None:
        job = make_job()

        job.cancel(now=EPOCH)

        assert job.status is JobStatus.CANCELLED

    def test_a_finished_job_cannot_be_cancelled(self) -> None:
        job = make_job()
        job.start(now=EPOCH)
        job.record_success(0, make_artifact(), now=EPOCH)

        with pytest.raises(InvalidJobTransitionError, match="succeeded"):
            job.cancel(now=EPOCH)

    def test_results_arriving_after_cancellation_are_ignored(self) -> None:
        # The runner may already be mid-flight when a cancel lands.
        job = make_job("A", "B")
        job.start(now=EPOCH)
        job.cancel(now=EPOCH)

        job.record_success(0, make_artifact(), now=EPOCH)

        assert job.status is JobStatus.CANCELLED
        assert job.items[0].status is ItemStatus.CANCELLED

    def test_failures_arriving_after_cancellation_are_ignored(self) -> None:
        # An in-flight download that errors out as the cancel lands must not flip a
        # cancelled job to failed.
        job = make_job("A", "B")
        job.start(now=EPOCH)
        job.cancel(now=EPOCH)

        job.record_failure(0, code="timeout", message="too slow", now=EPOCH)

        assert job.status is JobStatus.CANCELLED
        assert job.items[0].status is ItemStatus.CANCELLED
        assert job.items[0].error is None


class TestItemAccess:
    def test_an_unknown_index_is_rejected(self) -> None:
        job = make_job()

        with pytest.raises(JobItemNotFoundError):
            job.item(5)

    def test_a_negative_index_does_not_wrap_around(self) -> None:
        job = make_job("A", "B")

        with pytest.raises(JobItemNotFoundError):
            job.item(-1)

    def test_recording_against_an_unknown_index_is_rejected(self) -> None:
        job = make_job()

        with pytest.raises(JobItemNotFoundError):
            job.record_failure(9, code="x", message="y", now=EPOCH)


class TestCounts:
    def test_counts_summarize_progress(self) -> None:
        job = make_job("A", "B", "C")
        job.start(now=EPOCH)
        job.record_success(0, make_artifact(), now=EPOCH)
        job.record_failure(1, code="x", message="y", now=EPOCH)

        assert job.counts() == {"total": 3, "pending": 1, "succeeded": 1, "failed": 1}

    def test_cancelled_items_are_counted_separately(self) -> None:
        job = make_job("A", "B")
        job.start(now=EPOCH)
        job.cancel(now=EPOCH)

        assert job.counts()["cancelled"] == 2


class TestRetention:
    def test_a_job_expires_once_its_ttl_has_passed(self) -> None:
        clock = FakeClock()
        job = make_job(clock=clock)

        clock.advance(timedelta(seconds=61))

        assert job.is_expired(now=clock.now(), ttl_seconds=60)

    def test_a_job_within_its_ttl_has_not_expired(self) -> None:
        clock = FakeClock()
        job = make_job(clock=clock)

        clock.advance(timedelta(seconds=59))

        assert not job.is_expired(now=clock.now(), ttl_seconds=60)

    def test_expiry_runs_from_the_last_update_not_creation(self) -> None:
        clock = FakeClock()
        job = make_job(clock=clock)
        clock.advance(timedelta(seconds=50))
        job.start(now=clock.now())

        clock.advance(timedelta(seconds=30))

        assert not job.is_expired(now=clock.now(), ttl_seconds=60)


class TestTerminality:
    @pytest.mark.parametrize(
        ("status", "terminal"),
        [
            (JobStatus.QUEUED, False),
            (JobStatus.RUNNING, False),
            (JobStatus.SUCCEEDED, True),
            (JobStatus.PARTIAL, True),
            (JobStatus.FAILED, True),
            (JobStatus.CANCELLED, True),
        ],
    )
    def test_terminal_statuses(self, status: JobStatus, terminal: bool) -> None:
        assert status.is_terminal is terminal
