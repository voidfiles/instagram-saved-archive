"""Durable filesystem storage for archive snapshots."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
TRANSACTION_FILENAME = ".metadata-transaction.json"
_HASH_BLOCK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _MetadataTransaction:
    phase: Literal["prepared", "committed"]
    old_manifest: bytes | None
    old_state: bytes | None
    new_manifest: bytes
    new_state: bytes
    temporary_files: tuple[str, str]


class SnapshotStore:
    """Read, write, and validate the on-disk representation of one snapshot."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def initialize(self) -> None:
        """Create the required empty snapshot layout when it does not already exist."""
        self._ensure_root()
        self._recover_pending_transaction()
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
        self._recover_pending_transaction()
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
        """Persist metadata with recovery for an interruption between final replacements."""
        self._ensure_root()
        self._recover_pending_transaction()
        validate_manifest(manifest)
        manifest_path = self.root / MANIFEST_FILENAME
        state_path = self.root / SYNC_STATE_FILENAME
        journal_path = self.root / TRANSACTION_FILENAME
        new_manifest_data = _canonical_json(dump_manifest(manifest)).encode("utf-8")
        new_state_data = _canonical_json(dump_sync_state(state)).encode("utf-8")
        new_manifest: Path | None = None
        new_state: Path | None = None
        try:
            new_manifest = _write_temporary(manifest_path, new_manifest_data)
            new_state = _write_temporary(state_path, new_state_data)
            assert new_manifest is not None
            assert new_state is not None
            transaction = _MetadataTransaction(
                phase="prepared",
                old_manifest=_read_document(manifest_path),
                old_state=_read_document(state_path),
                new_manifest=new_manifest_data,
                new_state=new_state_data,
                temporary_files=(new_manifest.name, new_state.name),
            )
        except BaseException:
            if new_manifest is not None:
                new_manifest.unlink(missing_ok=True)
            if new_state is not None:
                new_state.unlink(missing_ok=True)
            raise
        assert new_manifest is not None
        assert new_state is not None
        journal_durable = False
        try:
            _write_journal(journal_path, transaction)
            journal_durable = True
            os.replace(new_manifest, manifest_path)
            os.replace(new_state, state_path)
            _fsync_directory(self.root)
            _write_journal(journal_path, _with_phase(transaction, "committed"))
            _complete_transaction(journal_path, transaction)
        except OSError:
            if journal_durable or journal_path.exists():
                self._recover_pending_transaction()
            else:
                new_manifest.unlink(missing_ok=True)
                new_state.unlink(missing_ok=True)
            raise
        except BaseException:
            if not journal_path.exists():
                new_manifest.unlink(missing_ok=True)
                new_state.unlink(missing_ok=True)
            raise

    def validate_files(self, manifest: Manifest) -> None:
        """Validate every referenced asset and reject unexpected media files or symlinks."""
        validate_manifest(manifest)
        self._ensure_root()
        self._recover_pending_transaction()
        root = self.root.resolve(strict=True)
        snapshot_files = tuple(_walk_files(root))
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
        for path in snapshot_files:
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
        self._recover_pending_transaction()
        return sum(path.stat().st_size for path in _walk_files(self.root.resolve(strict=True)))

    def _ensure_root(self) -> None:
        if self.root.is_symlink():
            raise ValueError("snapshot root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ValueError("snapshot root must be a directory")

    def _recover_pending_transaction(self) -> None:
        journal_path = self.root / TRANSACTION_FILENAME
        if journal_path.is_symlink():
            raise ValueError("snapshot transaction journal must not be a symlink")
        if not journal_path.exists():
            return
        transaction = _load_transaction(journal_path)
        if transaction.phase == "prepared":
            _replace_document(self.root / MANIFEST_FILENAME, transaction.old_manifest)
            _replace_document(self.root / SYNC_STATE_FILENAME, transaction.old_state)
        else:
            _replace_document(self.root / MANIFEST_FILENAME, transaction.new_manifest)
            _replace_document(self.root / SYNC_STATE_FILENAME, transaction.new_state)
        _fsync_directory(self.root)
        _complete_transaction(journal_path, transaction)


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


def _write_temporary(destination: Path, content: str | bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary_path = Path(temporary_name)
    try:
        data = content.encode("utf-8") if isinstance(content, str) else content
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        return temporary_path
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _read_document(destination: Path) -> bytes | None:
    if destination.is_symlink():
        raise ValueError(f"snapshot document must be a real file: {destination.name}")
    if not destination.exists():
        return None
    if not destination.is_file():
        raise ValueError(f"snapshot document must be a real file: {destination.name}")
    return destination.read_bytes()


def _replace_document(destination: Path, content: bytes | None) -> None:
    if content is None:
        destination.unlink(missing_ok=True)
        return
    temporary = _write_temporary(destination, content)
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _with_phase(
    transaction: _MetadataTransaction, phase: Literal["prepared", "committed"]
) -> _MetadataTransaction:
    return _MetadataTransaction(
        phase=phase,
        old_manifest=transaction.old_manifest,
        old_state=transaction.old_state,
        new_manifest=transaction.new_manifest,
        new_state=transaction.new_state,
        temporary_files=transaction.temporary_files,
    )


def _write_journal(path: Path, transaction: _MetadataTransaction) -> None:
    temporary = _write_temporary(path, _canonical_json(_dump_transaction(transaction)))
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _dump_transaction(transaction: _MetadataTransaction) -> dict[str, object]:
    return {
        "phase": transaction.phase,
        "old_manifest": _encode_optional_bytes(transaction.old_manifest),
        "old_state": _encode_optional_bytes(transaction.old_state),
        "new_manifest": _encode_bytes(transaction.new_manifest),
        "new_state": _encode_bytes(transaction.new_state),
        "temporary_files": list(transaction.temporary_files),
        "version": 1,
    }


def _load_transaction(path: Path) -> _MetadataTransaction:
    if path.is_symlink() or not path.is_file():
        raise ValueError("snapshot transaction journal must be a real file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("snapshot transaction journal is invalid") from error
    if not isinstance(data, dict) or set(data) != {
        "phase",
        "old_manifest",
        "old_state",
        "new_manifest",
        "new_state",
        "temporary_files",
        "version",
    }:
        raise ValueError("snapshot transaction journal has an invalid shape")
    if data["version"] != 1 or data["phase"] not in {"prepared", "committed"}:
        raise ValueError("snapshot transaction journal has an invalid version or phase")
    temporary_files = data["temporary_files"]
    if (
        not isinstance(temporary_files, list)
        or len(temporary_files) != 2
        or any(not isinstance(item, str) or Path(item).name != item for item in temporary_files)
    ):
        raise ValueError("snapshot transaction journal has invalid temporary file names")
    phase: Literal["prepared", "committed"] = data["phase"]
    return _MetadataTransaction(
        phase=phase,
        old_manifest=_decode_optional_bytes(data["old_manifest"]),
        old_state=_decode_optional_bytes(data["old_state"]),
        new_manifest=_decode_bytes(data["new_manifest"]),
        new_state=_decode_bytes(data["new_state"]),
        temporary_files=(temporary_files[0], temporary_files[1]),
    )


def _encode_bytes(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _encode_optional_bytes(value: bytes | None) -> str | None:
    return None if value is None else _encode_bytes(value)


def _decode_bytes(value: object) -> bytes:
    if not isinstance(value, str):
        raise ValueError("snapshot transaction journal has invalid encoded data")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as error:
        raise ValueError("snapshot transaction journal has invalid encoded data") from error


def _decode_optional_bytes(value: object) -> bytes | None:
    return None if value is None else _decode_bytes(value)


def _complete_transaction(path: Path, transaction: _MetadataTransaction) -> None:
    for temporary_name in transaction.temporary_files:
        (path.parent / temporary_name).unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


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
