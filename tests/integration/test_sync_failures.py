from __future__ import annotations

import os
from pathlib import Path

import pytest

from sync.archive.models import Manifest
from sync.archive.store import SnapshotStore
from sync.instagram.errors import (
    LoginError,
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


@pytest.mark.parametrize("status", list(VerificationStatus))
def test_unavailable_download_requires_source_verification(
    tmp_path: Path, status: VerificationStatus
) -> None:
    harness = SyncHarness(tmp_path)
    harness.client.saved = [public_image("DISAPPEARED")]
    harness.client.download_failure = UnavailablePostError("media unavailable")
    harness.client.statuses["DISAPPEARED"] = status
    report = harness.run()
    assert harness.client.verified == ["DISAPPEARED"]
    assert report.new_count == 0
    if status is VerificationStatus.PUBLIC:
        assert report.media_failure_shortcodes == ("DISAPPEARED",)
        assert (report.private_skip_count, report.unavailable_skip_count) == (0, 0)
    else:
        assert "DISAPPEARED" not in repr(report)
        assert (report.private_skip_count, report.unavailable_skip_count) == (
            (1, 0) if status is VerificationStatus.PRIVATE else (0, 1)
        )
    assert "DISAPPEARED" not in harness.snapshot_text()
    assert list((harness.snapshot / "media").iterdir()) == []


@pytest.mark.parametrize("error", [TransientExhaustionError, LoginError])
def test_unavailable_download_with_verification_failure_aborts(
    tmp_path: Path, error: type[Exception]
) -> None:
    harness = SyncHarness(tmp_path)
    harness.seed(2)
    harness.removals.write_text("OLD000\n", encoding="utf-8")
    harness.client.saved = [public_image("DISAPPEARED")]
    harness.client.download_failure = UnavailablePostError("media unavailable")
    harness.client.statuses["DISAPPEARED"] = error("verification failed")
    before = harness.snapshot_bytes()
    with pytest.raises(error):
        harness.run()
    assert harness.client.verified == ["DISAPPEARED"]
    assert harness.snapshot_bytes() == before


@pytest.mark.parametrize("existing_snapshot", [True, False])
def test_install_rename_then_interrupt_restores_original_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_snapshot: bool
) -> None:
    import shutil

    harness = SyncHarness(tmp_path)
    harness.seed(1)
    before = harness.snapshot_bytes()
    if not existing_snapshot:
        shutil.rmtree(harness.snapshot)
    harness.client.saved = [public_image("NEWPUBLIC")]
    original = os.replace
    interrupted = False

    def rename_then_interrupt(source: str | Path, destination: str | Path) -> None:
        nonlocal interrupted
        original(source, destination)
        if (
            Path(destination) == harness.snapshot
            and ".staging-" in Path(source).name
            and not interrupted
        ):
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr("sync.engine.os.replace", rename_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        harness.run()
    assert interrupted
    if existing_snapshot:
        assert harness.snapshot_bytes() == before
        harness.client.feed_failure = RuntimeError("abort after recovery")
        with pytest.raises(RuntimeError):
            harness.run()
        assert harness.snapshot_bytes() == before
    else:
        assert not harness.snapshot.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == (
        ["removals.txt", "snapshot"] if existing_snapshot else ["removals.txt"]
    )


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


@pytest.mark.parametrize("when", ["rename", "fsync"])
def test_empty_original_is_restored_after_backup_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    import shutil

    from sync import engine

    harness = SyncHarness(tmp_path)
    shutil.rmtree(harness.snapshot)
    harness.snapshot.mkdir()
    original_inode = harness.snapshot.stat().st_ino
    backup = tmp_path / ".snapshot.backup"
    real_replace = os.replace
    real_fsync = engine._fsync_directory
    interrupted = False

    def interrupt_rename(source: str | Path, destination: str | Path) -> None:
        nonlocal interrupted
        real_replace(source, destination)
        if Path(destination) == backup and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    def interrupt_fsync(directory: Path) -> None:
        nonlocal interrupted
        real_fsync(directory)
        if directory == tmp_path and backup.exists() and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    if when == "rename":
        monkeypatch.setattr("sync.engine.os.replace", interrupt_rename)
    else:
        monkeypatch.setattr(engine, "_fsync_directory", interrupt_fsync)
    with pytest.raises(KeyboardInterrupt):
        harness.run()
    assert interrupted
    assert harness.snapshot.stat().st_ino == original_inode
    assert list(harness.snapshot.iterdir()) == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["removals.txt", "snapshot"]
    assert harness.run().backfill_complete


@pytest.mark.parametrize("when", ["before_backup", "after_backup", "after_install"])
def test_empty_original_hard_restart_uses_durable_transaction_ownership(
    tmp_path: Path, when: str
) -> None:
    import shutil
    import subprocess
    import sys
    import textwrap

    harness = SyncHarness(tmp_path)
    shutil.rmtree(harness.snapshot)
    harness.snapshot.mkdir()
    code = textwrap.dedent("""
        import os
        import sys
        from pathlib import Path
        from sync import engine
        from tests.integration.fakes import FakeInstagramClient, NOW

        root = Path(sys.argv[1])
        when = sys.argv[2]
        marker = root / '.snapshot.swap-transaction.json'
        real_replace = os.replace
        real_fsync = os.fsync
        synced = set()

        def fsync(fd):
            real_fsync(fd)
            stat = os.fstat(fd)
            synced.add((stat.st_dev, stat.st_ino))

        def replace(source, destination):
            if Path(destination) == root / '.snapshot.backup':
                if not marker.exists():
                    os._exit(78)
                for path in (marker, root):
                    stat = path.stat()
                    if (stat.st_dev, stat.st_ino) not in synced:
                        os._exit(79)
                if when == 'before_backup':
                    os._exit(77)
            real_replace(source, destination)
            if when == 'after_backup' and Path(destination) == root / '.snapshot.backup':
                os._exit(77)
            if when == 'after_install' and Path(destination) == root / 'snapshot':
                os._exit(77)

        engine.os.fsync = fsync
        engine.os.replace = replace
        engine.SyncEngine(FakeInstagramClient()).run(
            root / 'snapshot', root / 'removals.txt', NOW, engine.SyncOptions()
        )
    """)
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), when],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 77, child.stderr.decode()
    harness.client.feed_failure = RuntimeError("stop after recovery")
    with pytest.raises(RuntimeError, match="stop after recovery"):
        harness.run()
    if when != "after_install":
        assert list(harness.snapshot.iterdir()) == []
    else:
        store = SnapshotStore(harness.snapshot)
        store.validate_files(store.load()[0])
    assert sorted(path.name for path in tmp_path.iterdir()) == ["removals.txt", "snapshot"]
    harness.client.feed_failure = None
    assert harness.run().backfill_complete


@pytest.mark.parametrize("with_marker", [False, True])
def test_empty_recovery_refuses_unrecognized_backup_ownership(
    tmp_path: Path, with_marker: bool
) -> None:
    import json

    harness = SyncHarness(tmp_path)
    backup = tmp_path / ".snapshot.backup"
    backup.mkdir()
    original_inode = backup.stat().st_ino
    marker = tmp_path / ".snapshot.swap-transaction.json"
    if with_marker:
        marker.write_text(json.dumps({"stage": "../unrelated", "original_inode": 1}))
    before = harness.snapshot_bytes()
    with pytest.raises((ValueError, ValidationError)):
        harness.run()
    assert backup.stat().st_ino == original_inode
    assert list(backup.iterdir()) == []
    assert harness.snapshot_bytes() == before
    assert marker.exists() is with_marker


@pytest.mark.parametrize("phase", ["prepared", "cleanup_stage", "cleanup_backup"])
def test_swap_marker_cannot_claim_a_different_backup_directory(tmp_path: Path, phase: str) -> None:
    import json
    import shutil

    harness = SyncHarness(tmp_path)
    stage = tmp_path / ".snapshot.staging-owned"
    shutil.copytree(harness.snapshot, stage)
    original = tmp_path / "preserved-original"
    harness.snapshot.rename(original)
    backup = tmp_path / ".snapshot.backup"
    backup.mkdir()
    backup_inode = backup.stat().st_ino
    marker = tmp_path / ".snapshot.swap-transaction.json"
    marker.write_text(
        json.dumps(
            {
                "snapshot_name": "snapshot",
                "stage_name": stage.name,
                "original_identity": [original.stat().st_dev, original.stat().st_ino],
                "stage_identity": [stage.stat().st_dev, stage.stat().st_ino],
                "phase": phase,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="backup does not belong"):
        harness.run()
    assert backup.stat().st_ino == backup_inode
    assert list(backup.iterdir()) == []
    assert not harness.snapshot.exists()
    assert original.is_dir()
    assert stage.is_dir()
    assert marker.is_file()


@pytest.mark.parametrize("when", ["serialization", "before_publish", "after_publish"])
def test_marker_publication_crash_preserves_original_and_allows_restart(
    tmp_path: Path, when: str
) -> None:
    import subprocess
    import sys
    import textwrap

    harness = SyncHarness(tmp_path)
    harness.seed(1)
    before = harness.snapshot_bytes()
    unrelated = tmp_path / ".snapshot.swap-marker-caller-owned"
    unrelated.write_bytes(b"do not glob-delete this file")
    code = textwrap.dedent("""
        import os
        import sys
        from pathlib import Path
        from sync import engine
        from tests.integration.fakes import FakeInstagramClient, NOW

        root = Path(sys.argv[1])
        when = sys.argv[2]
        marker = root / '.snapshot.swap-transaction.json'
        real_dump = engine.json.dump
        real_replace = os.replace

        def dump(data, stream, *args, **kwargs):
            if when == 'serialization' and 'snapshot_name' in data:
                stream.write('{"snapshot_name":')
                stream.flush()
                os._exit(77)
            return real_dump(data, stream, *args, **kwargs)

        def replace(source, destination):
            if Path(destination) == marker and when == 'before_publish':
                os._exit(77)
            real_replace(source, destination)
            if Path(destination) == marker and when == 'after_publish':
                os._exit(77)

        engine.json.dump = dump
        engine.os.replace = replace
        engine.SyncEngine(FakeInstagramClient()).run(
            root / 'snapshot', root / 'removals.txt', NOW, engine.SyncOptions()
        )
    """)
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), when],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 77, child.stderr.decode()
    assert harness.snapshot_bytes() == before
    orphans = {
        path: path.read_bytes()
        for path in tmp_path.glob(".snapshot.swap-marker-*")
        if path != unrelated
    }
    harness.client.feed_failure = RuntimeError("stop after recovery")
    with pytest.raises(RuntimeError, match="stop after recovery"):
        harness.run()
    assert harness.snapshot_bytes() == before
    assert unrelated.read_bytes() == b"do not glob-delete this file"
    assert not (tmp_path / ".snapshot.swap-transaction.json").exists()
    for path, data in orphans.items():
        assert path.read_bytes() == data
        assert path.stat().st_mode & 0o777 == 0o600
    if when != "after_publish":
        assert len(orphans) == 1
    harness.client.feed_failure = None
    assert harness.run().backfill_complete
    assert set(tmp_path.glob(".snapshot.swap-marker-*")) == {*orphans, unrelated}


@pytest.mark.parametrize("target", ["stage", "backup"])
def test_partial_owned_cleanup_resumes_after_hard_exit(tmp_path: Path, target: str) -> None:
    import json
    import subprocess
    import sys
    import textwrap

    harness = SyncHarness(tmp_path)
    harness.seed(2)
    code = textwrap.dedent("""
        import json
        import os
        import sys
        from pathlib import Path
        from sync import engine
        from tests.integration.fakes import FakeInstagramClient, NOW, public_image

        root = Path(sys.argv[1])
        target = sys.argv[2]
        real_replace = os.replace
        real_rmtree = engine.shutil.rmtree
        real_fsync = os.fsync
        interrupted = False
        synced = set()

        def fsync(fd):
            real_fsync(fd)
            stat = os.fstat(fd)
            synced.add((stat.st_dev, stat.st_ino))

        def replace(source, destination):
            global interrupted
            real_replace(source, destination)
            if target == 'stage' and Path(destination) == root / '.snapshot.backup' and not interrupted:
                interrupted = True
                raise KeyboardInterrupt

        def rmtree(path, *args, **kwargs):
            path = Path(path)
            matches = (
                target == 'stage' and path.name.startswith('.snapshot.staging-')
                or target == 'backup' and path == root / '.snapshot.backup'
            )
            if matches:
                marker = root / '.snapshot.swap-transaction.json'
                if json.loads(marker.read_text())['phase'] != 'cleanup_' + target:
                    os._exit(78)
                for retained in (marker, root):
                    stat = retained.stat()
                    if (stat.st_dev, stat.st_ino) not in synced:
                        os._exit(79)
                (path / 'manifest.json').unlink()
                os._exit(77)
            return real_rmtree(path, *args, **kwargs)

        engine.os.replace = replace
        engine.os.fsync = fsync
        engine.shutil.rmtree = rmtree
        client = FakeInstagramClient()
        client.saved = [public_image('NEWPUBLIC')]
        engine.SyncEngine(client).run(
            root / 'snapshot', root / 'removals.txt', NOW, engine.SyncOptions()
        )
    """)
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), target],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 77, child.stderr.decode()
    marker = tmp_path / ".snapshot.swap-transaction.json"
    transaction = json.loads(marker.read_text(encoding="utf-8"))
    partial = tmp_path / (transaction["stage_name"] if target == "stage" else ".snapshot.backup")
    assert partial.is_dir()
    assert not (partial / "manifest.json").exists()
    retained = harness.snapshot_bytes()
    harness.client.feed_failure = RuntimeError("stop after recovery")
    with pytest.raises(RuntimeError, match="stop after recovery"):
        harness.run()
    assert harness.snapshot_bytes() == retained
    assert sorted(path.name for path in tmp_path.iterdir()) == ["removals.txt", "snapshot"]
    harness.client.feed_failure = None
    assert harness.run().backfill_complete


def test_failed_marker_serialization_cleans_only_owned_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import TextIO

    harness = SyncHarness(tmp_path)
    harness.seed(1)
    before = harness.snapshot_bytes()
    unrelated = tmp_path / ".snapshot.swap-marker-caller-owned"
    unrelated.write_bytes(b"preserve unrelated temp")

    def fail_dump(data: object, stream: TextIO) -> None:
        stream.write("{")
        stream.flush()
        raise RuntimeError("serialization interrupted")

    monkeypatch.setattr("sync.engine.json.dump", fail_dump)
    with pytest.raises(RuntimeError, match="serialization interrupted"):
        harness.run()
    assert harness.snapshot_bytes() == before
    assert unrelated.read_bytes() == b"preserve unrelated temp"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        ".snapshot.swap-marker-caller-owned",
        "removals.txt",
        "snapshot",
    ]
