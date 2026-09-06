"""Transactional saved-feed discovery and rotating source reconciliation."""

from __future__ import annotations

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
)
from sync.archive.removals import apply_removals, parse_removals
from sync.archive.store import SnapshotStore
from sync.instagram.errors import SizeError, UnavailablePostError, ValidationError
from sync.instagram.models import PublicPost, VerificationStatus
from sync.instagram.protocol import InstagramClient
from sync.media.processor import PostMediaProcessor
from sync.reporting import SyncReport

__all__ = ["SyncEngine", "SyncOptions", "SyncReport"]


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
        if snapshot.is_symlink():
            raise ValidationError("snapshot must be a plain directory")
        snapshot = snapshot.absolute()
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        backup = snapshot.with_name(f".{snapshot.name}.backup")
        _recover_swap(snapshot, backup)
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
                raise SizeError("snapshot exceeds publication size budget")
            _fsync_tree(stage)
            _install_snapshot(stage, snapshot, backup)
            return replace(
                report,
                automatic_removal_count=automatic_count,
                snapshot_bytes=state.archive_byte_size,
            )
        finally:
            if stage.exists():
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
            except UnavailablePostError:
                report = replace(report, unavailable_skip_count=report.unavailable_skip_count + 1)
            except ValidationError:
                # The processor cleans all item output before raising. A fresh source check
                # is required before retaining a public shortcode in a private job summary.
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
            caption=post.caption,
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


def _recover_swap(snapshot: Path, backup: Path) -> None:
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
    installed = False
    try:
        if had_snapshot:
            os.replace(snapshot, backup)
            _fsync_directory(snapshot.parent)
        os.replace(stage, snapshot)
        installed = True
        _fsync_directory(snapshot.parent)
    except BaseException:
        if installed:
            os.replace(snapshot, stage)
        if backup.exists():
            os.replace(backup, snapshot)
        _fsync_directory(snapshot.parent)
        raise
    # Commit is durable. Best-effort cleanup cannot turn it into a failed run;
    # any remaining backup is validated and collected on the next invocation.
    if had_snapshot:
        shutil.rmtree(backup, ignore_errors=True)


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
