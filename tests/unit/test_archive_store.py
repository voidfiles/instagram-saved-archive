"""Filesystem snapshot store tests."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sync.archive.models import Manifest, SyncState, dump_manifest, dump_sync_state, load_manifest
from sync.archive.store import SnapshotStore


def _manifest_for_files(files: dict[str, bytes]) -> Manifest:
    """Build one valid image post whose recorded assets match ``files``."""
    primary = files["media/IMAGE0001/00.webp"]
    preview = files["media/IMAGE0001/00-thumb.webp"]
    return load_manifest(
        {
            "schema_version": 1,
            "posts": [
                {
                    "shortcode": "IMAGE0001",
                    "creator_username": "creator",
                    "creator_id": 1,
                    "source_url": "https://www.instagram.com/p/IMAGE0001/",
                    "caption": "A post",
                    "published_at": "2025-01-01T00:00:00Z",
                    "archived_at": "2025-01-02T00:00:00Z",
                    "verified_at": "2025-01-03T00:00:00Z",
                    "media_type": "image",
                    "publication_state": "published",
                    "unpublished_reason": None,
                    "media": [
                        {
                            "position": 0,
                            "kind": "image",
                            "asset": {
                                "asset_path": "media/IMAGE0001/00.webp",
                                "mime_type": "image/webp",
                                "width": 10,
                                "height": 10,
                                "byte_size": len(primary),
                                "sha256": hashlib.sha256(primary).hexdigest(),
                            },
                            "preview": {
                                "asset_path": "media/IMAGE0001/00-thumb.webp",
                                "mime_type": "image/webp",
                                "width": 5,
                                "height": 5,
                                "byte_size": len(preview),
                                "sha256": hashlib.sha256(preview).hexdigest(),
                            },
                        }
                    ],
                }
            ],
        }
    )


def _state() -> SyncState:
    return SyncState(
        schema_version=1,
        backfill_complete=False,
        last_successful_sync_at=None,
        last_complete_saved_feed_scan_at=None,
        reconciliation_cursor=0,
        consecutive_known_threshold=20,
        archive_byte_size=0,
    )


def _write_files(root: Path, files: dict[str, bytes]) -> None:
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def test_initialize_creates_an_empty_loadable_snapshot(tmp_path: Path) -> None:
    """Break caught: initialization omits a required snapshot document or empty media directory."""
    store = SnapshotStore(tmp_path / "snapshot")

    store.initialize()
    manifest, state = store.load()

    assert manifest.posts == ()
    assert state == _state()
    assert (store.root / "media").is_dir()


def test_write_atomic_uses_deterministic_json_with_one_trailing_newline(tmp_path: Path) -> None:
    """Break caught: serialization becomes unstable or leaves non-canonical trailing whitespace."""
    store = SnapshotStore(tmp_path / "snapshot")
    store.initialize()
    files = {
        "media/IMAGE0001/00.webp": b"primary bytes",
        "media/IMAGE0001/00-thumb.webp": b"preview bytes",
    }
    _write_files(store.root, files)
    manifest = _manifest_for_files(files)
    state = _state()

    store.write_atomic(manifest, state)

    for name, data in (
        ("manifest.json", dump_manifest(manifest)),
        ("sync-state.json", dump_sync_state(state)),
    ):
        text = (store.root / name).read_text(encoding="utf-8")
        assert text == json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        assert text.endswith("\n")
        assert not text.endswith("\n\n")


def test_repeated_atomic_writes_are_always_loadable_as_old_or_new_records(tmp_path: Path) -> None:
    """Break caught: a completed replacement can leave either JSON document truncated or malformed."""
    store = SnapshotStore(tmp_path / "snapshot")
    store.initialize()
    old_manifest, old_state = store.load()
    new_state = SyncState(
        schema_version=1,
        backfill_complete=True,
        last_successful_sync_at=datetime(2025, 1, 1, tzinfo=UTC),
        last_complete_saved_feed_scan_at=None,
        reconciliation_cursor=1,
        consecutive_known_threshold=20,
        archive_byte_size=123,
    )

    for manifest, state in ((old_manifest, old_state), (old_manifest, new_state)) * 3:
        store.write_atomic(manifest, state)
        assert store.load() == (manifest, state)


def test_write_atomic_restores_the_previous_pair_when_second_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break caught: a state-replacement failure leaves a new manifest with an old sync state."""
    store = SnapshotStore(tmp_path / "snapshot")
    store.initialize()
    old_manifest, old_state = store.load()
    old_manifest_bytes = (store.root / "manifest.json").read_bytes()
    old_state_bytes = (store.root / "sync-state.json").read_bytes()
    files = {
        "media/IMAGE0001/00.webp": b"primary bytes",
        "media/IMAGE0001/00-thumb.webp": b"preview bytes",
    }
    _write_files(store.root, files)
    state_path = store.root / "sync-state.json"
    failed = False
    original_replace = os.replace

    def fail_second_replace(source: str | Path, destination: str | Path) -> None:
        nonlocal failed
        if Path(destination) == state_path and not failed:
            failed = True
            raise OSError("injected second final replacement failure")
        original_replace(source, destination)

    monkeypatch.setattr("sync.archive.store.os.replace", fail_second_replace)

    with pytest.raises(OSError, match="injected second final replacement failure"):
        store.write_atomic(_manifest_for_files(files), replace(_state(), reconciliation_cursor=1))

    assert store.load() == (old_manifest, old_state)
    assert (store.root / "manifest.json").read_bytes() == old_manifest_bytes
    assert (store.root / "sync-state.json").read_bytes() == old_state_bytes


def test_fresh_store_recovers_a_terminated_write_after_manifest_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break caught: an abrupt stop exposes a mixed metadata pair after a restart."""
    store = SnapshotStore(tmp_path / "snapshot")
    store.initialize()
    files = {
        "media/IMAGE0001/00.webp": b"primary bytes",
        "media/IMAGE0001/00-thumb.webp": b"preview bytes",
    }
    _write_files(store.root, files)
    old_manifest = _manifest_for_files(files)
    old_state = _state()
    store.write_atomic(old_manifest, old_state)
    new_manifest = replace(old_manifest, posts=(replace(old_manifest.posts[0], caption="Updated"),))
    new_state = replace(old_state, reconciliation_cursor=1)
    manifest_path = store.root / "manifest.json"
    manifest_replacements = 0
    original_replace = os.replace

    def terminate_after_manifest_replacement(source: str | Path, destination: str | Path) -> None:
        nonlocal manifest_replacements
        original_replace(source, destination)
        if Path(destination) == manifest_path:
            manifest_replacements += 1
            if manifest_replacements == 1:
                raise SystemExit("simulated process termination after manifest replacement")

    monkeypatch.setattr("sync.archive.store.os.replace", terminate_after_manifest_replacement)

    with pytest.raises(SystemExit, match="simulated process termination"):
        store.write_atomic(new_manifest, new_state)

    assert (store.root / ".metadata-transaction.json").is_file()
    recovered = SnapshotStore(store.root)
    recovered_manifest, recovered_state = recovered.load()

    assert (recovered_manifest, recovered_state) in {
        (old_manifest, old_state),
        (new_manifest, new_state),
    }
    recovered.validate_files(recovered_manifest)
    assert {path.name for path in store.root.iterdir()} == {
        "manifest.json",
        "sync-state.json",
        "media",
    }


def test_recovery_rejects_final_metadata_names_in_a_corrupt_journal(tmp_path: Path) -> None:
    """Break caught: corrupt journal cleanup can delete final metadata files."""
    store = SnapshotStore(tmp_path / "snapshot")
    store.initialize()
    manifest_bytes = (store.root / "manifest.json").read_bytes()
    state_bytes = (store.root / "sync-state.json").read_bytes()
    journal = {
        "phase": "prepared",
        "old_manifest": base64.b64encode(manifest_bytes).decode("ascii"),
        "old_state": base64.b64encode(state_bytes).decode("ascii"),
        "new_manifest": base64.b64encode(manifest_bytes).decode("ascii"),
        "new_state": base64.b64encode(state_bytes).decode("ascii"),
        "temporary_files": ["manifest.json", "sync-state.json"],
        "version": 1,
    }
    (store.root / ".metadata-transaction.json").write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(ValueError, match="temporary file names"):
        SnapshotStore(store.root).load()

    assert (store.root / "manifest.json").read_bytes() == manifest_bytes
    assert (store.root / "sync-state.json").read_bytes() == state_bytes


def test_validate_files_accepts_recorded_files_and_reports_snapshot_size(tmp_path: Path) -> None:
    """Break caught: valid files fail validation or snapshot size ignores persisted files."""
    store = SnapshotStore(tmp_path / "snapshot")
    store.initialize()
    files = {
        "media/IMAGE0001/00.webp": b"primary bytes",
        "media/IMAGE0001/00-thumb.webp": b"preview bytes",
    }
    _write_files(store.root, files)
    manifest = _manifest_for_files(files)

    store.validate_files(manifest)

    assert store.size_bytes() == sum(
        path.stat().st_size for path in store.root.rglob("*") if path.is_file()
    )


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda root: (root / "media/IMAGE0001/00.webp").unlink(), id="missing"),
        pytest.param(
            lambda root: (root / "media/IMAGE0001/00.webp").write_bytes(b"changed"),
            id="hash-or-size",
        ),
        pytest.param(
            lambda root: (root / "media/stray.webp").write_bytes(b"unreferenced"), id="unreferenced"
        ),
    ],
)
def test_validate_files_rejects_missing_changed_or_unreferenced_media(
    tmp_path: Path, mutation: Callable[[Path], object]
) -> None:
    """Break caught: media validation accepts absent, altered, or untracked files."""
    store = SnapshotStore(tmp_path / "snapshot")
    store.initialize()
    files = {
        "media/IMAGE0001/00.webp": b"primary bytes",
        "media/IMAGE0001/00-thumb.webp": b"preview bytes",
    }
    _write_files(store.root, files)
    manifest = _manifest_for_files(files)

    mutation(store.root)

    with pytest.raises(ValueError):
        store.validate_files(manifest)


def test_validate_files_rejects_a_symlink_anywhere_under_media(tmp_path: Path) -> None:
    """Break caught: a snapshot may include symlinked media that escapes validation boundaries."""
    store = SnapshotStore(tmp_path / "snapshot")
    store.initialize()
    files = {
        "media/IMAGE0001/00.webp": b"primary bytes",
        "media/IMAGE0001/00-thumb.webp": b"preview bytes",
    }
    _write_files(store.root, files)
    manifest = _manifest_for_files(files)
    symlink = store.root / "media/IMAGE0001/link.webp"
    try:
        symlink.symlink_to(store.root / "media/IMAGE0001/00.webp")
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    with pytest.raises(ValueError):
        store.validate_files(manifest)


def test_validate_files_rejects_top_level_symlink_for_an_empty_snapshot(tmp_path: Path) -> None:
    """Break caught: an empty snapshot skips whole-tree symlink validation when media is absent."""
    store = SnapshotStore(tmp_path / "snapshot")
    store.initialize()
    (store.root / "media").rmdir()
    target = tmp_path / "target.txt"
    target.write_text("outside snapshot", encoding="utf-8")
    link = store.root / "top-level-link"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    with pytest.raises(ValueError):
        store.validate_files(store.load()[0])
