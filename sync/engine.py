"""Transactional saved-feed discovery and rotating source reconciliation."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from sync.archive.budget import check_budget
from sync.archive.models import (
    ArchivePost,
    Manifest,
    MediaKind,
    MediaType,
    PublicationState,
    SyncState,
    _normalize_caption,
)
from sync.archive.removals import apply_removals, parse_removals
from sync.archive.store import SnapshotStore
from sync.instagram.errors import SizeError, UnavailablePostError, ValidationError
from sync.instagram.models import PublicPost, VerificationStatus
from sync.instagram.protocol import InstagramClient
from sync.media.processor import PostMediaProcessor
from sync.reporting import SyncReport

__all__ = ["SyncEngine", "SyncOptions", "SyncReport", "recover_snapshot"]


@dataclass(frozen=True, slots=True)
class SyncOptions:
    max_new_posts: int = 50
    full_scan: bool = False
    known_threshold: int = 20
    reconcile_limit: int = 20

    def __post_init__(self) -> None:
        for value in (self.max_new_posts, self.known_threshold, self.reconcile_limit):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValidationError("sync limits must be integers")
        if self.known_threshold < 1 or self.reconcile_limit < 1:
            raise ValidationError("known threshold and reconciliation limit must be positive")
        if not isinstance(self.full_scan, bool):
            raise ValidationError("full scan must be a boolean")
        object.__setattr__(self, "max_new_posts", max(1, min(50, self.max_new_posts)))
        object.__setattr__(self, "reconcile_limit", min(20, self.reconcile_limit))


class SyncEngine:
    """Compose authenticated client, media processor, and snapshot store in one local transaction.

    The caller serializes runs for a given plain directory and validates client identity.
    Publication consumes the resulting validated export separately.
    """

    def __init__(
        self, client: InstagramClient, processor: PostMediaProcessor | None = None
    ) -> None:
        self.client = client
        self.processor = processor or PostMediaProcessor()

    def run(
        self, snapshot: Path, removals: Path, now: datetime, options: SyncOptions
    ) -> SyncReport:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValidationError("sync time must be timezone-aware")
        now = now.astimezone(UTC)
        snapshot = snapshot.absolute()
        recover_snapshot(snapshot)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        backup = snapshot.with_name(f".{snapshot.name}.backup")
        stage = Path(tempfile.mkdtemp(prefix=f".{snapshot.name}.staging-", dir=snapshot.parent))
        try:
            if snapshot.exists():
                shutil.copytree(snapshot, stage, dirs_exist_ok=True, symlinks=True)
            store = SnapshotStore(stage)
            store.initialize()
            manifest, state = store.load()
            store.validate_files(manifest)
            removed_codes = parse_removals(removals)
            original_order = _rotation(manifest, state.reconciliation_cursor)
            manifest, deleted = apply_removals(manifest, removed_codes)
            for code in deleted:
                _delete_media(stage, code)
            report = SyncReport(
                explicit_removal_count=len(deleted), backfill_complete=state.backfill_complete
            )
            manifest, report, exhausted = self._discover(
                stage, manifest, removed_codes, now, options, report
            )
            manifest, cursor, automatic_count = self._reconcile(
                stage, manifest, original_order, now, options.reconcile_limit
            )
            state = replace(
                state,
                backfill_complete=report.backfill_complete,
                last_successful_sync_at=now,
                last_complete_saved_feed_scan_at=(
                    now if exhausted else state.last_complete_saved_feed_scan_at
                ),
                reconciliation_cursor=cursor,
                consecutive_known_threshold=options.known_threshold,
            )
            store.validate_files(manifest)
            state = _write_sized_metadata(store, manifest, state)
            # Reload strict serialized records and recheck all referenced bytes before the swap.
            persisted, _ = store.load()
            store.validate_files(persisted)
            budget = check_budget(stage)
            if budget.level == "reject":
                raise SizeError("snapshot exceeds publication size budget", budget=budget)
            _fsync_tree(stage)
            _install_snapshot(stage, snapshot, backup)
            return replace(
                report,
                automatic_removal_count=automatic_count,
                snapshot_bytes=state.archive_byte_size,
                snapshot_budget=budget,
            )
        finally:
            # A published transaction owns cleanup, including resumable partial deletion.
            # Never bypass its durable phase if recovery or marker publication failed.
            if stage.exists() and not _swap_marker(snapshot).exists():
                shutil.rmtree(stage, ignore_errors=True)

    def _discover(
        self,
        stage: Path,
        manifest: Manifest,
        removals: frozenset[str],
        now: datetime,
        options: SyncOptions,
        report: SyncReport,
    ) -> tuple[Manifest, SyncReport, bool]:
        posts = {post.shortcode: post for post in manifest.posts}
        consecutive_known = 0
        attempted = 0
        exhausted = False
        for candidate in self.client.iter_saved():
            try:
                post = self.client.materialize(candidate)
            except UnavailablePostError:
                consecutive_known = 0
                report = replace(report, unavailable_skip_count=report.unavailable_skip_count + 1)
                continue
            if post is None:
                consecutive_known = 0
                report = replace(report, private_skip_count=report.private_skip_count + 1)
                continue
            if post.shortcode in posts or post.shortcode in removals:
                consecutive_known += 1
                report = replace(report, known_count=report.known_count + 1)
                if (
                    report.backfill_complete
                    and not options.full_scan
                    and consecutive_known >= options.known_threshold
                ):
                    break
                continue
            consecutive_known = 0
            attempted += 1
            try:
                archived = self._ingest(stage, post, now)
            except (UnavailablePostError, ValidationError):
                # The processor cleans all item output before raising. A fresh source check
                # is required: an unavailable media URL is not evidence that the post
                # disappeared, and only a confirmed public shortcode may be reported.
                status = self.client.verify(post.shortcode)
                if status is VerificationStatus.PUBLIC:
                    report = replace(
                        report,
                        media_failure_shortcodes=(*report.media_failure_shortcodes, post.shortcode),
                    )
                elif status is VerificationStatus.PRIVATE:
                    report = replace(report, private_skip_count=report.private_skip_count + 1)
                elif status is VerificationStatus.UNAVAILABLE:
                    report = replace(
                        report, unavailable_skip_count=report.unavailable_skip_count + 1
                    )
                else:
                    raise ValidationError(
                        "source verification returned an invalid status"
                    ) from None
            else:
                posts[post.shortcode] = archived
                report = replace(report, new_count=report.new_count + 1)
            if attempted >= options.max_new_posts:
                break
        else:
            exhausted = True
            report = replace(report, backfill_complete=True)
        return _manifest(tuple(posts.values())), report, exhausted

    def _ingest(self, stage: Path, post: PublicPost, now: datetime) -> ArchivePost:
        media = self.processor.process(post, self.client.download, stage / "media")
        media_type = (
            MediaType.CAROUSEL
            if len(media) > 1
            else MediaType.VIDEO
            if media[0].kind is MediaKind.VIDEO
            else MediaType.IMAGE
        )
        return ArchivePost(
            shortcode=post.shortcode,
            creator_username=post.creator_username,
            creator_id=post.creator_id,
            source_url=post.source_url,
            caption=_normalize_caption(post.caption, "post.caption"),
            published_at=post.published_at,
            archived_at=now,
            verified_at=now,
            media_type=media_type,
            publication_state=PublicationState.PUBLISHED,
            unpublished_reason=None,
            media=media,
        )

    def _reconcile(
        self,
        stage: Path,
        manifest: Manifest,
        original_order: tuple[str, ...],
        now: datetime,
        limit: int,
    ) -> tuple[Manifest, int, int]:
        posts = {post.shortcode: post for post in manifest.posts}
        if not posts:
            return manifest, 0, 0
        codes = tuple(posts)
        anchor = next((code for code in original_order if code in posts), codes[0])
        start = codes.index(anchor)
        order = codes[start:] + codes[:start]
        selected = order[:limit]
        # Collect the entire batch before changing records or deleting any staged media.
        statuses = [(code, self.client.verify(code)) for code in selected]
        removed = 0
        for code, status in statuses:
            if status is VerificationStatus.PUBLIC:
                posts[code] = replace(posts[code], verified_at=now)
            elif status in (VerificationStatus.PRIVATE, VerificationStatus.UNAVAILABLE):
                del posts[code]
                _delete_media(stage, code)
                removed += 1
            else:
                raise ValidationError("source verification returned an invalid status")
        result = _manifest(tuple(posts.values()))
        next_order = order[len(selected) :] + order[: len(selected)]
        next_code = next((code for code in next_order if code in posts), None)
        cursor = (
            tuple(post.shortcode for post in result.posts).index(next_code)
            if next_code is not None
            else 0
        )
        return result, cursor, removed


def _manifest(posts: tuple[ArchivePost, ...]) -> Manifest:
    return Manifest(
        1, tuple(sorted(posts, key=lambda post: (-post.archived_at.timestamp(), post.shortcode)))
    )


def _rotation(manifest: Manifest, cursor: int) -> tuple[str, ...]:
    codes = tuple(post.shortcode for post in manifest.posts)
    if not codes:
        return ()
    cursor %= len(codes)
    return codes[cursor:] + codes[:cursor]


def _delete_media(stage: Path, shortcode: str) -> None:
    directory = stage / "media" / shortcode
    if directory.exists():
        shutil.rmtree(directory)


def _write_sized_metadata(store: SnapshotStore, manifest: Manifest, state: SyncState) -> SyncState:
    # The byte count includes its own decimal representation; settle that fixed point.
    for _ in range(20):
        store.write_atomic(manifest, state)
        actual = store.size_bytes()
        if actual == state.archive_byte_size:
            return state
        state = replace(state, archive_byte_size=actual)
    raise ValidationError("snapshot size accounting did not converge")


def recover_snapshot(snapshot: Path) -> None:
    """Recover a prior local swap before validating or accessing Instagram.

    Callers must serialize access. An absent snapshot can have an owned backup;
    the existing transaction recovery validates every name before mutating it.
    """
    snapshot = snapshot.absolute()
    if any(component.is_symlink() for component in (snapshot, *snapshot.parents)):
        raise ValidationError("snapshot path components must not be symlinks")
    _recover_swap(snapshot, snapshot.with_name(f".{snapshot.name}.backup"))


def _recover_swap(snapshot: Path, backup: Path) -> None:
    transaction = _read_swap_transaction(snapshot)
    if transaction is not None:
        _recover_owned_swap(snapshot, backup, transaction)
        return
    if backup.is_symlink() or (backup.exists() and not backup.is_dir()):
        raise ValidationError("snapshot backup must be a plain directory")
    if not backup.exists():
        return
    recovery_store = SnapshotStore(backup)
    recovery_manifest, _ = recovery_store.load()
    recovery_store.validate_files(recovery_manifest)
    if not snapshot.exists():
        os.replace(backup, snapshot)
        _fsync_directory(snapshot.parent)
        return
    # Both names mean installation finished. Never discard the recovery copy unless
    # the installed snapshot passes strict metadata and asset validation.
    store = SnapshotStore(snapshot)
    manifest, _ = store.load()
    store.validate_files(manifest)
    shutil.rmtree(backup)
    _fsync_directory(snapshot.parent)


def _install_snapshot(stage: Path, snapshot: Path, backup: Path) -> None:
    had_snapshot = snapshot.exists()
    transaction = _prepare_swap_transaction(snapshot, stage) if had_snapshot else None
    try:
        if had_snapshot:
            os.replace(snapshot, backup)
            _fsync_directory(snapshot.parent)
        os.replace(stage, snapshot)
        _fsync_directory(snapshot.parent)
    except BaseException:
        if transaction is not None:
            _recover_owned_swap(snapshot, backup, transaction, rollback=True)
            raise
        # A rename can complete before raising (for example on KeyboardInterrupt).
        # Infer installation from directory state, never a flag assigned after rename.
        # Validate both recognized snapshots before moving either recovery name.
        if backup.exists():
            _validate_snapshot(backup)
        if not stage.exists() and snapshot.exists():
            _validate_snapshot(snapshot)
            os.replace(snapshot, stage)
        if backup.exists():
            os.replace(backup, snapshot)
        _fsync_directory(snapshot.parent)
        raise
    # Commit is durable. Best-effort cleanup cannot turn it into a failed run;
    # any remaining backup is validated and collected on the next invocation.
    if had_snapshot:
        assert transaction is not None
        try:
            _recover_owned_swap(snapshot, backup, transaction)
        except OSError:
            # A durable installed snapshot remains committed; retain ownership proof
            # so the next invocation can complete interrupted cleanup safely.
            pass


@dataclass(frozen=True, slots=True)
class _SwapTransaction:
    stage_name: str
    original_identity: tuple[int, int]
    stage_identity: tuple[int, int]
    phase: str = "prepared"


def _swap_marker(snapshot: Path) -> Path:
    return snapshot.with_name(f".{snapshot.name}.swap-transaction.json")


def _directory_identity(path: Path) -> tuple[int, int]:
    if path.is_symlink() or not path.is_dir():
        raise ValidationError("transaction directory must be a plain directory")
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def _prepare_swap_transaction(snapshot: Path, stage: Path) -> _SwapTransaction:
    transaction = _SwapTransaction(
        stage.name, _directory_identity(snapshot), _directory_identity(stage)
    )
    marker = _swap_marker(snapshot)
    if marker.exists() or marker.is_symlink():
        raise ValidationError("swap transaction marker already exists")
    _publish_swap_transaction(snapshot, transaction)
    return transaction


def _publish_swap_transaction(snapshot: Path, transaction: _SwapTransaction) -> None:
    # A crash can leave an unpublished temp without durable ownership proof. Startup
    # ignores such siblings; it must never glob-delete them or parse them as markers.
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{snapshot.name}.swap-marker-", dir=snapshot.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "snapshot_name": snapshot.name,
                    "stage_name": transaction.stage_name,
                    "original_identity": transaction.original_identity,
                    "stage_identity": transaction.stage_identity,
                    "phase": transaction.phase,
                },
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, _swap_marker(snapshot))
        _fsync_directory(snapshot.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_swap_transaction(snapshot: Path) -> _SwapTransaction | None:
    marker = _swap_marker(snapshot)
    if marker.is_symlink():
        raise ValidationError("swap transaction marker must not be a symlink")
    if not marker.exists():
        return None
    data: object = json.loads(marker.read_text(encoding="utf-8"))
    fields = {
        "snapshot_name",
        "stage_name",
        "original_identity",
        "stage_identity",
    }
    if not isinstance(data, dict) or set(data) not in (fields, fields | {"phase"}):
        raise ValidationError("invalid swap transaction marker")
    phase = data.get("phase", "prepared")
    if not isinstance(phase, str) or phase not in ("prepared", "cleanup_stage", "cleanup_backup"):
        raise ValidationError("invalid swap transaction phase")
    stage_name = data["stage_name"]
    if (
        data["snapshot_name"] != snapshot.name
        or not isinstance(stage_name, str)
        or not stage_name.startswith(f".{snapshot.name}.staging-")
        or Path(stage_name).name != stage_name
    ):
        raise ValidationError("swap transaction marker is outside its scope")
    return _SwapTransaction(
        stage_name,
        _parse_identity(data["original_identity"]),
        _parse_identity(data["stage_identity"]),
        phase,
    )


def _parse_identity(value: object) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(not isinstance(part, int) or isinstance(part, bool) or part < 0 for part in value)
    ):
        raise ValidationError("invalid swap directory identity")
    return int(value[0]), int(value[1])


def _recover_owned_swap(
    snapshot: Path, backup: Path, transaction: _SwapTransaction, *, rollback: bool = False
) -> None:
    stage = snapshot.parent / transaction.stage_name
    has_backup = backup.exists() or backup.is_symlink()
    has_stage = stage.exists() or stage.is_symlink()
    has_snapshot = snapshot.exists() or snapshot.is_symlink()
    if has_backup and _directory_identity(backup) != transaction.original_identity:
        raise ValidationError("backup does not belong to the swap transaction")
    if has_stage and _directory_identity(stage) != transaction.stage_identity:
        raise ValidationError("staging directory does not belong to the swap transaction")
    installed = False
    if has_snapshot:
        identity = _directory_identity(snapshot)
        installed = identity == transaction.stage_identity
        if installed:
            if has_stage:
                raise ValidationError("swap transaction contains duplicate staging directories")
            _validate_snapshot(snapshot)
        elif identity != transaction.original_identity or has_backup:
            raise ValidationError("snapshot does not belong to the swap transaction")
    elif not has_backup:
        raise ValidationError("swap transaction has no recoverable snapshot")
    if transaction.phase == "cleanup_stage":
        if not has_snapshot or installed or has_backup:
            raise ValidationError("staging cleanup has no retained original snapshot")
    elif transaction.phase == "cleanup_backup":
        if not installed or has_stage:
            raise ValidationError("backup cleanup has no validated installed snapshot")
    elif has_stage:
        _validate_snapshot(stage)
    # Every existing name has been checked before any move or deletion.
    if installed and rollback:
        if not has_backup:
            raise ValidationError("swap transaction has no original backup")
        os.replace(snapshot, stage)
        has_snapshot = False
        has_stage = True
    if has_backup:
        if has_snapshot:
            if transaction.phase != "cleanup_backup":
                _publish_swap_transaction(snapshot, replace(transaction, phase="cleanup_backup"))
            shutil.rmtree(backup)
        else:
            os.replace(backup, snapshot)
    if has_stage:
        if transaction.phase != "cleanup_stage":
            _publish_swap_transaction(snapshot, replace(transaction, phase="cleanup_stage"))
        shutil.rmtree(stage)
    _fsync_directory(snapshot.parent)
    _swap_marker(snapshot).unlink()
    _fsync_directory(snapshot.parent)


def _validate_snapshot(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("snapshot must be a plain directory")
    store = SnapshotStore(root)
    manifest, _ = store.load()
    store.validate_files(manifest)


def _fsync_tree(root: Path) -> None:
    directories = [root]
    for path in root.rglob("*"):
        if path.is_dir():
            directories.append(path)
        else:
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    for directory in reversed(directories):
        _fsync_directory(directory)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
