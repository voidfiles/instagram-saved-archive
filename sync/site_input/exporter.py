"""Export canonical published metadata and referenced assets into a clean directory."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from sync.archive.models import Manifest, SyncState
from sync.archive.store import SnapshotStore


def resolve_plain_path(path: Path, *, must_exist: bool = True) -> Path:
    """Resolve a path only after rejecting symlinks in all existing components."""
    path = path.absolute()
    if any(component.is_symlink() for component in (path, *path.parents)):
        raise ValueError("path components must not be symlinks")
    return path.resolve(strict=must_exist)


def validate_snapshot(snapshot: Path) -> tuple[Manifest, SyncState]:
    """Load a real snapshot directory and validate all referenced bytes."""
    snapshot = resolve_plain_path(snapshot)
    if not snapshot.is_dir():
        raise ValueError("snapshot must be a directory")
    # Check before loading: metadata recovery must never traverse a symlink.
    _check_tree(snapshot)
    store = SnapshotStore(snapshot)
    manifest, state = store.load()
    store.validate_files(manifest)
    return manifest, state


def export_site_input(snapshot: Path, destination: Path) -> None:
    """Replace destination with both validated documents and referenced media only."""
    snapshot = resolve_plain_path(snapshot)
    destination = resolve_plain_path(destination, must_exist=False)
    if snapshot.is_relative_to(destination) or destination.is_relative_to(snapshot):
        raise ValueError("snapshot and destination must not overlap")
    if destination.exists():
        if not destination.is_dir():
            raise ValueError("destination must be a directory")
        _check_tree(destination)
    manifest, state = validate_snapshot(snapshot)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.export-", dir=destination.parent))
    backup: Path | None = None
    installed = False
    try:
        store = SnapshotStore(stage)
        store.initialize()
        for post in manifest.posts:
            for media in post.media:
                for asset in (media.asset, media.preview):
                    target = stage / asset.asset_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = resolve_plain_path(snapshot / asset.asset_path)
                    shutil.copyfile(source, target, follow_symlinks=False)
        store.write_atomic(manifest, state)
        validate_snapshot(stage)
        if destination.exists():
            backup = Path(
                tempfile.mkdtemp(prefix=f".{destination.name}.old-", dir=destination.parent)
            )
            backup.rmdir()
            destination.rename(backup)
        try:
            stage.rename(destination)
            installed = True
        except OSError:
            if backup is not None:
                backup.rename(destination)
            raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if installed and backup is not None and backup.exists():
            shutil.rmtree(backup)


def _check_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("snapshot and destination must contain only regular files/directories")
