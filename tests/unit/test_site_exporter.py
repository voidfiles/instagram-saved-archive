"""The export boundary rejects invalid input and replaces stale site data."""

import importlib
import json
from pathlib import Path

import pytest
from test_archive_store import _manifest_for_files, _state, _write_files

from sync.archive.models import dump_manifest, dump_sync_state
from sync.archive.store import SnapshotStore


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "snapshot"
    store = SnapshotStore(root)
    store.initialize()
    files = {
        "media/IMAGE0001/00.webp": b"image bytes",
        "media/IMAGE0001/00-thumb.webp": b"preview bytes",
    }
    _write_files(root, files)
    store.write_atomic(_manifest_for_files(files), _state())
    return root


def exporter():
    try:
        return importlib.import_module("sync.site_input.exporter").export_site_input
    except ModuleNotFoundError:
        pytest.fail("validated site export has not been implemented")


def test_export_replaces_stale_data_and_preserves_only_validated_paths(snapshot: Path) -> None:
    destination = snapshot.parent / "site"
    destination.mkdir()
    (destination / "stale-private.json").write_text("private")
    (snapshot / "instagram.session").write_text("secret")
    exporter()(snapshot, destination)
    assert sorted(
        str(p.relative_to(destination)) for p in destination.rglob("*") if p.is_file()
    ) == [
        "manifest.json",
        "media/IMAGE0001/00-thumb.webp",
        "media/IMAGE0001/00.webp",
        "sync-state.json",
    ]
    manifest, state = SnapshotStore(snapshot).load()
    assert json.loads((destination / "manifest.json").read_text()) == dump_manifest(manifest)
    assert json.loads((destination / "sync-state.json").read_text()) == dump_sync_state(state)
    assert (destination / "media/IMAGE0001/00.webp").read_bytes() == b"image bytes"
    SnapshotStore(destination).validate_files(manifest)


@pytest.mark.parametrize("corruption", ["unpublished", "unreferenced", "hash", "symlink"])
def test_invalid_source_preserves_existing_destination(snapshot: Path, corruption: str) -> None:
    destination = snapshot.parent / "site"
    destination.mkdir()
    sentinel = destination / "existing"
    sentinel.write_text("keep")
    if corruption == "unpublished":
        path = snapshot / "manifest.json"
        data = json.loads(path.read_text())
        data["posts"][0]["publication_state"] = "unpublished"
        path.write_text(json.dumps(data))
    elif corruption == "unreferenced":
        (snapshot / "media/private.webp").write_bytes(b"private")
    elif corruption == "hash":
        (snapshot / "media/IMAGE0001/00.webp").write_bytes(b"wrong bytes")
    else:
        (snapshot / "extra-link").symlink_to(sentinel)
    with pytest.raises(ValueError):
        exporter()(snapshot, destination)
    assert sentinel.read_text() == "keep"


@pytest.mark.parametrize("kind", ["root", "parent", "child"])
def test_export_rejects_destination_symlinks(snapshot: Path, kind: str) -> None:
    external = snapshot.parent / "external"
    external.mkdir()
    (external / "keep").write_text("keep")
    destination = snapshot.parent / "site"
    if kind == "root":
        destination.symlink_to(external, target_is_directory=True)
    elif kind == "parent":
        alias = snapshot.parent / "alias"
        alias.symlink_to(external, target_is_directory=True)
        destination = alias / "site"
    else:
        destination.mkdir()
        (destination / "link").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError):
        exporter()(snapshot, destination)
    assert (external / "keep").read_text() == "keep"


@pytest.mark.parametrize("relation", ["same", "inside", "ancestor"])
def test_export_rejects_overlapping_paths(snapshot: Path, relation: str) -> None:
    destination = {"same": snapshot, "inside": snapshot / "site", "ancestor": snapshot.parent}[
        relation
    ]
    before = (snapshot / "manifest.json").read_bytes()
    with pytest.raises(ValueError):
        exporter()(snapshot, destination)
    assert (snapshot / "manifest.json").read_bytes() == before


def test_failed_export_install_and_restore_preserves_previous_copy(
    snapshot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = snapshot.parent / "site"
    destination.mkdir()
    (destination / "keep").write_text("previous publication")
    original = Path.rename

    def fail_install_and_restore(path: Path, target: Path) -> Path:
        if target == destination:
            raise OSError("simulated install and restore failures")
        return original(path, target)

    monkeypatch.setattr(Path, "rename", fail_install_and_restore)
    with pytest.raises(OSError):
        exporter()(snapshot, destination)
    survivors = list(snapshot.parent.glob(".site.old-*/keep"))
    assert len(survivors) == 1
    assert survivors[0].read_text() == "previous publication"


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        "instagram.session",
        "media/OTHER/00.webp",
        "./media/IMAGE0001/00.webp",
        "media/IMAGE0001/00.mp4",
    ],
)
def test_referenced_unsafe_asset_is_rejected_before_export_copy(snapshot: Path, path: str) -> None:
    """Break caught: an attacker-controlled manifest copies Git config or session bytes."""
    document = snapshot / "manifest.json"
    data = json.loads(document.read_text())
    asset = data["posts"][0]["media"][0]["asset"]
    original = snapshot / asset["asset_path"]
    target = snapshot / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target != original:
        original.rename(target)
    asset["asset_path"] = path
    document.write_text(json.dumps(data))
    destination = snapshot.parent / "site"
    destination.mkdir()
    (destination / "keep").write_bytes(b"previous export")

    with pytest.raises(ValueError):
        exporter()(snapshot, destination)

    assert [(item.name, item.read_bytes()) for item in destination.iterdir()] == [
        ("keep", b"previous export")
    ]
