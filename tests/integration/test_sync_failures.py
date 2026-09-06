from __future__ import annotations

import os
from pathlib import Path

import pytest

from sync.archive.models import Manifest
from sync.archive.store import SnapshotStore
from sync.instagram.errors import (
    SizeError,
    TransientExhaustionError,
    UnavailablePostError,
    ValidationError,
)
from sync.instagram.models import VerificationStatus
from tests.integration.fakes import SyncHarness, public_image


@pytest.mark.parametrize(
    "phase", ["download", "processing", "validation", "budget", "feed", "swap"]
)
def test_raised_failure_preserves_every_original_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    harness = SyncHarness(tmp_path)
    harness.seed(2)
    harness.removals.write_text("OLD000\n", encoding="utf-8")
    harness.client.saved = [public_image("NEWPUBLIC")]
    before = harness.snapshot_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected interruption")

    if phase == "download":
        harness.client.download_failure = RuntimeError("injected interruption")
    elif phase == "processing":
        monkeypatch.setattr(harness.processor, "_process_item", fail)
    elif phase == "validation":
        validate = SnapshotStore.validate_files
        calls = 0

        def fail_final_validation(store: SnapshotStore, manifest: Manifest) -> None:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("injected interruption")
            validate(store, manifest)

        monkeypatch.setattr(SnapshotStore, "validate_files", fail_final_validation)
    elif phase == "budget":
        monkeypatch.setattr("sync.engine.check_budget", fail)
    elif phase == "feed":
        harness.client.feed_failure = RuntimeError("injected interruption")
    else:
        original = os.replace

        def fail_install(source: str | Path, destination: str | Path) -> None:
            if Path(destination) == harness.snapshot and ".staging-" in Path(source).name:
                raise RuntimeError("injected interruption")
            original(source, destination)

        monkeypatch.setattr("sync.engine.os.replace", fail_install)
    with pytest.raises(RuntimeError, match="injected interruption"):
        harness.run()
    assert harness.snapshot_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["removals.txt", "snapshot"]


def test_rejected_budget_aborts_without_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = SyncHarness(tmp_path)
    harness.seed(1)
    before = harness.snapshot_bytes()
    monkeypatch.setattr("sync.archive.budget.REJECT_BYTES", 1)
    with pytest.raises(SizeError):
        harness.run()
    assert harness.snapshot_bytes() == before


@pytest.mark.parametrize("status", list(VerificationStatus))
def test_invalid_media_is_skipped_only_after_definitive_verification(
    tmp_path: Path, status: VerificationStatus
) -> None:
    harness = SyncHarness(tmp_path)
    harness.client.saved = [public_image("FAILPUBLIC")]
    harness.client.invalid_image = True
    harness.client.statuses["FAILPUBLIC"] = status
    report = harness.run()
    assert report.new_count == 0
    assert SnapshotStore(harness.snapshot).load()[0].posts == ()
    assert list((harness.snapshot / "media").iterdir()) == []
    if status is VerificationStatus.PUBLIC:
        assert report.media_failure_shortcodes == ("FAILPUBLIC",)
    else:
        assert "FAILPUBLIC" not in repr(report)
        assert (report.private_skip_count, report.unavailable_skip_count) == (
            (1, 0) if status is VerificationStatus.PRIVATE else (0, 1)
        )


def test_invalid_media_with_transient_reverification_aborts(tmp_path: Path) -> None:
    harness = SyncHarness(tmp_path)
    harness.client.saved = [public_image("FAILPUBLIC")]
    harness.client.invalid_image = True
    harness.client.statuses["FAILPUBLIC"] = TransientExhaustionError("safe error")
    before = harness.snapshot_bytes()
    with pytest.raises(TransientExhaustionError):
        harness.run()
    assert harness.snapshot_bytes() == before


def test_restart_restores_backup_when_interrupted_between_renames(tmp_path: Path) -> None:
    harness = SyncHarness(tmp_path)
    harness.seed(1)
    before = harness.snapshot_bytes()
    harness.snapshot.rename(tmp_path / ".snapshot.backup")
    harness.client.feed_failure = RuntimeError("abort after recovery")
    with pytest.raises(RuntimeError):
        harness.run()
    assert harness.snapshot_bytes() == before
    assert not (tmp_path / ".snapshot.backup").exists()


def test_restart_with_installed_snapshot_discards_old_backup(tmp_path: Path) -> None:
    import shutil

    harness = SyncHarness(tmp_path)
    harness.seed(1)
    shutil.copytree(harness.snapshot, tmp_path / ".snapshot.backup")
    harness.client.saved = [public_image("NEWPUBLIC")]
    assert harness.run().new_count == 1
    assert not (tmp_path / ".snapshot.backup").exists()


def test_recovery_refuses_unrecognized_backup_directory(tmp_path: Path) -> None:
    harness = SyncHarness(tmp_path)
    backup = tmp_path / ".snapshot.backup"
    backup.mkdir()
    owned_file = backup / "caller-owned.txt"
    owned_file.write_bytes(b"preserve this unrelated directory")
    before = harness.snapshot_bytes()
    with pytest.raises((ValueError, ValidationError)):
        harness.run()
    assert owned_file.read_bytes() == b"preserve this unrelated directory"
    assert harness.snapshot_bytes() == before


def test_snapshot_symlinks_are_rejected_without_following_them(tmp_path: Path) -> None:
    harness = SyncHarness(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    (harness.snapshot / "secret-link").symlink_to(outside)
    with pytest.raises((ValueError, ValidationError)):
        harness.run()
    assert outside.read_bytes() == b"untouched"
    assert (harness.snapshot / "secret-link").is_symlink()


def test_disappearance_during_download_is_an_aggregate_discovery_skip(tmp_path: Path) -> None:
    harness = SyncHarness(tmp_path)
    harness.client.saved = [public_image("DISAPPEARED")]
    harness.client.download_failure = UnavailablePostError("post disappeared")
    report = harness.run()
    assert report.unavailable_skip_count == 1
    assert report.new_count == 0
    assert "DISAPPEARED" not in repr(report)
    assert "DISAPPEARED" not in harness.snapshot_text()
    assert list((harness.snapshot / "media").iterdir()) == []


@pytest.mark.parametrize("when", ["after_backup", "after_install"])
def test_swap_fsync_interruption_rolls_back_both_directory_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    from sync import engine

    harness = SyncHarness(tmp_path)
    harness.seed(1)
    harness.client.saved = [public_image("NEWPUBLIC")]
    before = harness.snapshot_bytes()
    fsync = engine._fsync_directory
    interrupted = False

    def interrupt_commit(directory: Path) -> None:
        nonlocal interrupted
        backup = tmp_path / ".snapshot.backup"
        reached = backup.exists() and harness.snapshot.exists() == (when == "after_install")
        if directory == tmp_path and reached and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        fsync(directory)

    monkeypatch.setattr(engine, "_fsync_directory", interrupt_commit)
    with pytest.raises(KeyboardInterrupt):
        harness.run()
    assert interrupted
    assert harness.snapshot_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["removals.txt", "snapshot"]
