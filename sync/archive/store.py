"""Durable filesystem storage for archive snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

from .models import (
    SCHEMA_VERSION,
    Manifest,
    SyncState,
    dump_manifest,
    dump_sync_state,
    load_manifest,
    load_sync_state,
)
from .validation import validate_manifest

MANIFEST_FILENAME = "manifest.json"
SYNC_STATE_FILENAME = "sync-state.json"
MEDIA_DIRECTORY = "media"
_HASH_BLOCK_SIZE = 1024 * 1024


class SnapshotStore:
    """Read, write, and validate the on-disk representation of one snapshot."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def initialize(self) -> None:
        """Create the required empty snapshot layout when it does not already exist."""
        self._ensure_root()
        (self.root / MEDIA_DIRECTORY).mkdir(exist_ok=True)
        manifest_path = self.root / MANIFEST_FILENAME
        state_path = self.root / SYNC_STATE_FILENAME
        if not manifest_path.exists() and not state_path.exists():
            self.write_atomic(
                Manifest(schema_version=SCHEMA_VERSION, posts=()), _empty_sync_state()
            )
        elif not manifest_path.exists() or not state_path.exists():
            raise ValueError("snapshot must contain both manifest.json and sync-state.json")

    def load(self) -> tuple[Manifest, SyncState]:
        """Load strict typed records from both required snapshot documents."""
        self._ensure_root()
        try:
            manifest_data: object = json.loads(
                (self.root / MANIFEST_FILENAME).read_text(encoding="utf-8")
            )
            state_data: object = json.loads(
                (self.root / SYNC_STATE_FILENAME).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("snapshot metadata is missing or invalid JSON") from error
        return load_manifest(manifest_data), load_sync_state(state_data)

    def write_atomic(self, manifest: Manifest, state: SyncState) -> None:
        """Persist canonical metadata by replacing each fsynced document atomically."""
        self._ensure_root()
        validate_manifest(manifest)
        manifest_text = _canonical_json(dump_manifest(manifest))
        state_text = _canonical_json(dump_sync_state(state))
        _atomic_write(self.root / MANIFEST_FILENAME, manifest_text)
        _atomic_write(self.root / SYNC_STATE_FILENAME, state_text)

    def validate_files(self, manifest: Manifest) -> None:
        """Validate every referenced asset and reject unexpected media files or symlinks."""
        validate_manifest(manifest)
        self._ensure_root()
        root = self.root.resolve(strict=True)
        expected_paths: set[Path] = set()
        for asset_path, byte_size, sha256 in _assets(manifest):
            path = root / asset_path
            _reject_symlink_components(root, path)
            try:
                resolved = path.resolve(strict=True)
            except OSError as error:
                raise ValueError(f"referenced asset is missing: {asset_path}") from error
            if not resolved.is_relative_to(root):
                raise ValueError(f"referenced asset escapes snapshot root: {asset_path}")
            if not resolved.is_file():
                raise ValueError(f"referenced asset is not a file: {asset_path}")
            actual_size = resolved.stat().st_size
            if actual_size != byte_size:
                raise ValueError(f"asset size does not match manifest: {asset_path}")
            if _sha256_file(resolved) != sha256:
                raise ValueError(f"asset hash does not match manifest: {asset_path}")
            expected_paths.add(resolved.relative_to(root))

        media_root = root / MEDIA_DIRECTORY
        if not media_root.exists():
            if expected_paths:
                raise ValueError("media directory is missing")
            return
        if media_root.is_symlink() or not media_root.is_dir():
            raise ValueError("media directory must be a real directory")
        for path in _walk_files(root):
            relative = path.relative_to(root)
            if relative.parts[0] == MEDIA_DIRECTORY and relative not in expected_paths:
                raise ValueError(f"unreferenced media file: {relative.as_posix()}")

    def size_bytes(self) -> int:
        """Return the aggregate size of regular snapshot files without following symlinks."""
        if self.root.is_symlink():
            raise ValueError("snapshot root must not be a symlink")
        if not self.root.exists():
            return 0
        self._ensure_root()
        return sum(path.stat().st_size for path in _walk_files(self.root.resolve(strict=True)))

    def _ensure_root(self) -> None:
        if self.root.is_symlink():
            raise ValueError("snapshot root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ValueError("snapshot root must be a directory")


def _empty_sync_state() -> SyncState:
    return SyncState(
        schema_version=SCHEMA_VERSION,
        backfill_complete=False,
        last_successful_sync_at=None,
        last_complete_saved_feed_scan_at=None,
        reconciliation_cursor=0,
        consecutive_known_threshold=20,
        archive_byte_size=0,
    )


def _canonical_json(data: dict[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _atomic_write(destination: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _assets(manifest: Manifest) -> Iterator[tuple[Path, int, str]]:
    for post in manifest.posts:
        for media in post.media:
            yield Path(media.asset.asset_path), media.asset.byte_size, media.asset.sha256
            yield Path(media.preview.asset_path), media.preview.byte_size, media.preview.sha256


def _reject_symlink_components(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError("referenced asset escapes snapshot root") from error
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"symlinked asset path is not allowed: {relative.as_posix()}")


def _walk_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ValueError(f"symlink is not allowed: {path.relative_to(root).as_posix()}")
        if path.is_file():
            yield path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(_HASH_BLOCK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()
