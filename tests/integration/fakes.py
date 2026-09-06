"""Protocol-level Instagram fake; storage and image processing remain real."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

from sync.archive.models import ArchivePost, Manifest, MediaType, PublicationState
from sync.archive.store import SnapshotStore
from sync.instagram.models import (
    MediaKind,
    PublicPost,
    SavedCandidate,
    SourceMedia,
    VerificationStatus,
)
from sync.media.processor import PostMediaProcessor

if TYPE_CHECKING:
    from sync.engine import SyncOptions
    from sync.reporting import SyncReport

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
BEFORE = NOW - timedelta(days=1)


class FakeInstagramClient:
    def __init__(self) -> None:
        self.saved: list[SavedCandidate] = []
        self.statuses: dict[str, VerificationStatus | Exception] = {}
        self.materialized = 0
        self.downloads = 0
        self.verified: list[str] = []
        self.download_failure: Exception | None = None
        self.feed_failure: Exception | None = None
        self.invalid_image = False

    def validate_identity(self, username: str) -> None:
        pass

    def iter_saved(self) -> Iterator[SavedCandidate]:
        yield from self.saved
        if self.feed_failure is not None:
            raise self.feed_failure

    def materialize(self, candidate: SavedCandidate) -> PublicPost | None:
        self.materialized += 1
        if isinstance(candidate.token, Exception):
            raise candidate.token
        if isinstance(candidate.token, PublicPost):
            return candidate.token
        return None

    def download(self, source: SourceMedia, destination: Path) -> None:
        self.downloads += 1
        if self.download_failure is not None:
            destination.write_bytes(b"partial download")
            raise self.download_failure
        if self.invalid_image:
            destination.write_bytes(b"invalid image")
        else:
            Image.new("RGB", (8, 6), "blue").save(destination, format="PNG")

    def verify(self, shortcode: str) -> VerificationStatus:
        self.verified.append(shortcode)
        status = self.statuses.get(shortcode, VerificationStatus.PUBLIC)
        if isinstance(status, Exception):
            raise status
        return status


def public_image(shortcode: str) -> SavedCandidate:
    return SavedCandidate(
        PublicPost(
            shortcode=shortcode,
            creator_username="public_owner",
            creator_id=1,
            source_url=f"https://www.instagram.com/p/{shortcode}/",
            caption="Public caption",
            published_at=BEFORE,
            media=(SourceMedia(0, MediaKind.IMAGE, "https://cdn.example/image"),),
        )
    )


class SyncHarness:
    def __init__(self, root: Path) -> None:
        self.snapshot = root / "snapshot"
        self.removals = root / "removals.txt"
        self.removals.write_text("", encoding="utf-8")
        self.client = FakeInstagramClient()
        self.processor = PostMediaProcessor()
        SnapshotStore(self.snapshot).initialize()

    def seed(self, count: int, *, complete: bool = False, cursor: int = 0) -> None:
        store = SnapshotStore(self.snapshot)
        _, state = store.load()
        posts: list[ArchivePost] = []
        for index in range(count):
            candidate = public_image(f"OLD{index:03}")
            post = candidate.token
            assert isinstance(post, PublicPost)
            media = self.processor.process(post, self.client.download, self.snapshot / "media")
            posts.append(
                ArchivePost(
                    shortcode=post.shortcode,
                    creator_username=post.creator_username,
                    creator_id=post.creator_id,
                    source_url=post.source_url,
                    caption=post.caption,
                    published_at=post.published_at,
                    archived_at=BEFORE,
                    verified_at=BEFORE,
                    media_type=MediaType.IMAGE,
                    publication_state=PublicationState.PUBLISHED,
                    unpublished_reason=None,
                    media=media,
                )
            )
        store.write_atomic(
            Manifest(1, tuple(posts)),
            replace(state, backfill_complete=complete, reconciliation_cursor=cursor),
        )
        self.client.downloads = 0

    def sequence(self, count: int) -> list[SavedCandidate]:
        return [public_image(f"OLD{index:03}") for index in range(count)]

    def run(self, options: SyncOptions | None = None) -> SyncReport:
        from sync.engine import SyncEngine, SyncOptions

        return SyncEngine(self.client, self.processor).run(
            self.snapshot, self.removals, NOW, options or SyncOptions()
        )

    def snapshot_bytes(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.snapshot).as_posix(): path.read_bytes()
            for path in self.snapshot.rglob("*")
            if path.is_file()
        }

    def snapshot_text(self) -> str:
        return "\n".join(path.read_text(encoding="utf-8") for path in self.snapshot.glob("*.json"))
