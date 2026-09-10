"""Local artifact storage.

Filenames here come from a remote site via ``Content-Disposition``. They are hostile
input, and most of this file exists to prove they are treated that way.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from typing import TYPE_CHECKING

import pytest

from media_tool.domain.errors import ArtifactNotFoundError, ArtifactTooLargeError
from media_tool.storage.local import DEFAULT_FILENAME, LocalArtifactStore
from tests.fakes.clock import FakeClock

if TYPE_CHECKING:
    from pathlib import Path

    from media_tool.domain.artifacts import DownloadArtifact
    from media_tool.storage.base import ArtifactStore

PAYLOAD = b"the quick brown fox" * 16


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> LocalArtifactStore:
    return LocalArtifactStore(root=tmp_path / "artifacts", clock=clock, max_file_bytes=1024)


def stage(
    store: LocalArtifactStore,
    payload: bytes = PAYLOAD,
    *,
    suggested_filename: str = "dune.mkv",
    content_type: str = "video/x-matroska",
    source_url: str = "https://example.test/dune.mkv",
    index: int = 0,
) -> DownloadArtifact:
    """Run one full reserve-write-commit cycle."""
    with store.reserve(job_id="job1", index=index) as sink:
        sink.staging_path.write_bytes(payload)
        return sink.commit(
            suggested_filename=suggested_filename,
            content_type=content_type,
            source_url=source_url,
        )


def commit_without_writing(store: LocalArtifactStore) -> DownloadArtifact:
    """Commit a reservation that nothing ever wrote to."""
    with store.reserve(job_id="job1", index=0) as sink:
        return sink.commit(
            suggested_filename="a.bin",
            content_type="application/octet-stream",
            source_url="https://example.test/a",
        )


def stage_then_raise(store: LocalArtifactStore) -> None:
    """Abandon a reservation by failing partway through."""
    with store.reserve(job_id="job1", index=0) as sink:
        sink.staging_path.write_bytes(PAYLOAD)
        raise RuntimeError


class TestPortConformance:
    def test_the_local_store_satisfies_the_port(self, store: LocalArtifactStore) -> None:
        checked: ArtifactStore = store

        assert checked is store


class TestHappyPath:
    def test_committing_returns_metadata_describing_the_file(
        self, store: LocalArtifactStore
    ) -> None:
        artifact = stage(store)

        assert artifact.filename == "dune.mkv"
        assert artifact.size_bytes == len(PAYLOAD)
        assert artifact.content_type == "video/x-matroska"
        assert artifact.source_url == "https://example.test/dune.mkv"
        assert len(artifact.sha256) == 64

    def test_the_digest_is_of_the_bytes_actually_written(self, store: LocalArtifactStore) -> None:
        artifact = stage(store, payload=b"abc")

        assert artifact.sha256 == hashlib.sha256(b"abc").hexdigest()

    def test_the_file_is_readable_afterwards(self, store: LocalArtifactStore) -> None:
        artifact = stage(store)

        located = store.locate(job_id="job1", index=0, filename=artifact.filename)

        assert located.read_bytes() == PAYLOAD

    def test_duration_is_measured_across_the_reservation(
        self, store: LocalArtifactStore, clock: FakeClock
    ) -> None:
        with store.reserve(job_id="job1", index=0) as sink:
            clock.advance(2.5)
            sink.staging_path.write_bytes(PAYLOAD)
            artifact = sink.commit(
                suggested_filename="a.bin",
                content_type="application/octet-stream",
                source_url="https://example.test/a",
            )

        assert artifact.duration_seconds == pytest.approx(2.5)

    def test_items_in_the_same_job_do_not_collide(self, store: LocalArtifactStore) -> None:
        for index in (0, 1):
            stage(store, payload=bytes([index]), suggested_filename="same-name.bin", index=index)

        first = store.locate(job_id="job1", index=0, filename="same-name.bin")
        second = store.locate(job_id="job1", index=1, filename="same-name.bin")
        assert first.read_bytes() != second.read_bytes()


class TestFilenameSanitization:
    @pytest.mark.parametrize(
        ("supplied", "expected"),
        [
            ("../../etc/passwd", "passwd"),
            ("/etc/shadow", "shadow"),
            ("..\\..\\windows\\system32\\config", "config"),
            ("....//....//evil.bin", "evil.bin"),
            ("..", DEFAULT_FILENAME),
            (".", DEFAULT_FILENAME),
            ("", DEFAULT_FILENAME),
            ("   ", DEFAULT_FILENAME),
            (".hidden", "hidden"),
            ("nul\x00byte.bin", "nulbyte.bin"),
            ("bell\x07.bin", "bell.bin"),
            ("new\nline.bin", "new line.bin"),
            ("  spaced  .bin  ", "spaced .bin"),
        ],
    )
    def test_hostile_names_are_defanged(
        self, store: LocalArtifactStore, supplied: str, expected: str
    ) -> None:
        artifact = stage(store, suggested_filename=supplied)

        assert artifact.filename == expected

    def test_a_sanitized_name_never_escapes_the_job_directory(
        self, store: LocalArtifactStore
    ) -> None:
        artifact = stage(store, suggested_filename="../../../../../../tmp/pwned")

        located = store.locate(job_id="job1", index=0, filename=artifact.filename)
        assert store.root in located.parents

    def test_absurdly_long_names_are_truncated_but_keep_their_extension(
        self, store: LocalArtifactStore
    ) -> None:
        artifact = stage(store, suggested_filename="x" * 400 + ".mkv")

        assert len(artifact.filename.encode()) <= 255
        assert artifact.filename.endswith(".mkv")

    def test_a_name_that_is_only_an_extension_is_kept(self, store: LocalArtifactStore) -> None:
        artifact = stage(store, suggested_filename="archive.tar.gz")

        assert artifact.filename == "archive.tar.gz"


class TestSizeLimit:
    def test_oversized_files_are_rejected(self, store: LocalArtifactStore) -> None:
        with pytest.raises(ArtifactTooLargeError, match="1024"):
            stage(store, payload=b"x" * 2048, suggested_filename="big.bin")

    def test_a_rejected_file_leaves_nothing_behind(self, store: LocalArtifactStore) -> None:
        with pytest.raises(ArtifactTooLargeError):
            stage(store, payload=b"x" * 2048, suggested_filename="big.bin")

        assert list(store.root.rglob("*.bin")) == []
        assert not any(store.staging_root.iterdir())

    def test_a_file_exactly_at_the_limit_is_accepted(self, store: LocalArtifactStore) -> None:
        artifact = stage(store, payload=b"x" * 1024)

        assert artifact.size_bytes == 1024


class TestAbandonedReservations:
    def test_leaving_the_block_without_committing_cleans_up(
        self, store: LocalArtifactStore
    ) -> None:
        with store.reserve(job_id="job1", index=0) as sink:
            sink.staging_path.write_bytes(PAYLOAD)

        assert not any(store.staging_root.iterdir())

    def test_an_exception_inside_the_block_cleans_up_and_propagates(
        self, store: LocalArtifactStore
    ) -> None:
        with pytest.raises(RuntimeError):
            stage_then_raise(store)

        assert not any(store.staging_root.iterdir())

    def test_committing_without_writing_anything_is_rejected(
        self, store: LocalArtifactStore
    ) -> None:
        with pytest.raises(ArtifactNotFoundError, match="nothing was written"):
            commit_without_writing(store)


class TestLocate:
    def test_an_unknown_job_is_not_found(self, store: LocalArtifactStore) -> None:
        with pytest.raises(ArtifactNotFoundError):
            store.locate(job_id="nope", index=0, filename="a.bin")

    def test_a_mismatched_filename_is_not_found(self, store: LocalArtifactStore) -> None:
        stage(store)

        with pytest.raises(ArtifactNotFoundError):
            store.locate(job_id="job1", index=0, filename="other.bin")

    @pytest.mark.parametrize("hostile", ["../../../etc/passwd", "/etc/passwd", "..", "a/b"])
    def test_traversal_through_the_lookup_is_refused(
        self, store: LocalArtifactStore, hostile: str
    ) -> None:
        stage(store)

        with pytest.raises(ArtifactNotFoundError):
            store.locate(job_id="job1", index=0, filename=hostile)

    def test_traversal_through_the_job_id_is_refused(self, store: LocalArtifactStore) -> None:
        stage(store)

        with pytest.raises(ArtifactNotFoundError):
            store.locate(job_id="../..", index=0, filename="dune.mkv")

    def test_a_symlink_pointing_out_of_the_root_is_refused(
        self, store: LocalArtifactStore, tmp_path: Path
    ) -> None:
        # The name passes the segment check, so this is what the containment check is
        # actually for.
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"secret")
        item_dir = store.root / "job1" / "0"
        item_dir.mkdir(parents=True, exist_ok=True)
        (item_dir / "innocent.bin").symlink_to(outside)

        with pytest.raises(ArtifactNotFoundError):
            store.locate(job_id="job1", index=0, filename="innocent.bin")


def age(path: Path, seconds: float) -> None:
    """Backdate a path's modification time, which is all an orphan has left."""
    stamp = path.stat().st_mtime - seconds
    os.utime(path, (stamp, stamp))


class TestLinking:
    """One download can satisfy several submitted items."""

    def test_a_linked_artifact_is_readable_at_the_new_index(
        self, store: LocalArtifactStore
    ) -> None:
        original = stage(store)

        linked = store.link_artifact(
            job_id="job1", source_index=0, target_index=2, filename=original.filename
        )

        located = store.locate(job_id="job1", index=2, filename=linked.filename)
        assert located.read_bytes() == PAYLOAD
        assert linked.sha256 == original.sha256

    def test_linking_does_not_duplicate_the_bytes(self, store: LocalArtifactStore) -> None:
        original = stage(store)

        store.link_artifact(
            job_id="job1", source_index=0, target_index=2, filename=original.filename
        )

        source = store.locate(job_id="job1", index=0, filename=original.filename)
        target = store.locate(job_id="job1", index=2, filename=original.filename)
        assert source.stat().st_ino == target.stat().st_ino

    def test_it_falls_back_to_copying_where_hard_links_are_unavailable(
        self, store: LocalArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = stage(store)

        def no_links(*_args: object, **_kwargs: object) -> None:
            raise OSError(18, "Invalid cross-device link")

        monkeypatch.setattr(os, "link", no_links)

        linked = store.link_artifact(
            job_id="job1", source_index=0, target_index=2, filename=original.filename
        )

        located = store.locate(job_id="job1", index=2, filename=linked.filename)
        assert located.read_bytes() == PAYLOAD

    def test_linking_a_missing_artifact_is_not_found(self, store: LocalArtifactStore) -> None:
        with pytest.raises(ArtifactNotFoundError):
            store.link_artifact(
                job_id="job1", source_index=0, target_index=1, filename="absent.bin"
            )

    def test_relinking_over_an_existing_file_replaces_it(self, store: LocalArtifactStore) -> None:
        original = stage(store)
        stage(store, payload=b"different", suggested_filename=original.filename, index=2)

        store.link_artifact(
            job_id="job1", source_index=0, target_index=2, filename=original.filename
        )

        located = store.locate(job_id="job1", index=2, filename=original.filename)
        assert located.read_bytes() == PAYLOAD


class TestPurgeJob:
    """The normal retention path: driven by the job store, which knows the clock."""

    def test_purging_a_job_removes_its_files(self, store: LocalArtifactStore) -> None:
        stage(store)

        assert store.purge_job("job1") is True
        with pytest.raises(ArtifactNotFoundError):
            store.locate(job_id="job1", index=0, filename="dune.mkv")

    def test_purging_an_unknown_job_reports_nothing_to_do(self, store: LocalArtifactStore) -> None:
        assert store.purge_job("never-existed") is False

    @pytest.mark.parametrize("hostile", ["../..", "/etc", "a/b", ""])
    def test_a_hostile_job_id_deletes_nothing(
        self, store: LocalArtifactStore, hostile: str
    ) -> None:
        stage(store)

        assert store.purge_job(hostile) is False
        assert store.locate(job_id="job1", index=0, filename="dune.mkv").exists()


class TestOrphanSweep:
    """The backstop: files whose job record died with the process."""

    def test_stale_orphans_are_swept(self, store: LocalArtifactStore) -> None:
        stage(store)
        age(store.root / "job1", seconds=61)

        assert store.purge_expired(ttl_seconds=60) == 1
        with pytest.raises(ArtifactNotFoundError):
            store.locate(job_id="job1", index=0, filename="dune.mkv")

    def test_recent_artifacts_survive_a_sweep(self, store: LocalArtifactStore) -> None:
        stage(store)

        assert store.purge_expired(ttl_seconds=60) == 0
        assert store.locate(job_id="job1", index=0, filename="dune.mkv").exists()

    def test_sweeping_an_empty_store_is_harmless(self, store: LocalArtifactStore) -> None:
        assert store.purge_expired(ttl_seconds=60) == 0

    def test_the_staging_area_is_never_swept_as_if_it_were_a_job(
        self, store: LocalArtifactStore
    ) -> None:
        stage(store)
        age(store.root / "job1", seconds=61)
        age(store.staging_root, seconds=61)

        store.purge_expired(ttl_seconds=60)

        assert store.staging_root.is_dir()

    def test_stray_files_in_the_root_are_left_alone(self, store: LocalArtifactStore) -> None:
        stray = store.root / "README"
        stray.write_text("not a job")
        age(stray, seconds=999)

        assert store.purge_expired(ttl_seconds=60) == 0
        assert stray.exists()


class TestHealth:
    def test_a_writable_store_reports_healthy(self, store: LocalArtifactStore) -> None:
        health = store.health()

        assert health.writable is True
        assert health.free_bytes > 0

    def test_a_store_whose_root_vanished_reports_unhealthy(
        self, tmp_path: Path, clock: FakeClock
    ) -> None:
        # An unmounted volume, or someone cleaning up /var. Health must report it
        # rather than raising out of the health check.
        store = LocalArtifactStore(root=tmp_path / "gone", clock=clock, max_file_bytes=1024)
        shutil.rmtree(store.root)

        health = store.health()

        assert health.writable is False
        assert health.free_bytes == 0
