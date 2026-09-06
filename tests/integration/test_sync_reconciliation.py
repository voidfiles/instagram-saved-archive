from __future__ import annotations

from pathlib import Path

import pytest

from sync.archive.store import SnapshotStore
from sync.instagram.errors import LoginError, ThrottleError, TransientExhaustionError
from sync.instagram.models import VerificationStatus
from tests.integration.fakes import BEFORE, NOW, SyncHarness


def test_reconciliation_rotates_twenty_and_wraps_without_unsaved_removal(tmp_path: Path) -> None:
    harness = SyncHarness(tmp_path)
    harness.seed(45, complete=True)
    for expected_cursor in (20, 40, 15):
        report = harness.run()
        assert report.automatic_removal_count == 0
        manifest, state = SnapshotStore(harness.snapshot).load()
        assert len(manifest.posts) == 45
        assert state.reconciliation_cursor == expected_cursor
    assert harness.client.verified == (
        [f"OLD{i:03}" for i in range(45)] + [f"OLD{i:03}" for i in range(15)]
    )
    assert all(post.verified_at == NOW for post in manifest.posts)


def test_bounded_reconciliation_refreshes_only_selected_posts(tmp_path: Path) -> None:
    harness = SyncHarness(tmp_path)
    harness.seed(25)
    harness.run()
    posts = SnapshotStore(harness.snapshot).load()[0].posts
    assert all(post.verified_at == NOW for post in posts[:20])
    assert all(post.verified_at == BEFORE for post in posts[20:])


def test_fewer_than_twenty_posts_are_verified_once(tmp_path: Path) -> None:
    harness = SyncHarness(tmp_path)
    harness.seed(3, cursor=2)
    harness.run()
    assert harness.client.verified == ["OLD002", "OLD000", "OLD001"]
    assert SnapshotStore(harness.snapshot).load()[1].reconciliation_cursor == 2


def test_private_and_missing_posts_remove_media_and_keep_next_cursor(tmp_path: Path) -> None:
    from sync.engine import SyncOptions

    harness = SyncHarness(tmp_path)
    harness.seed(5, cursor=1)
    harness.client.statuses = {
        "OLD001": VerificationStatus.PRIVATE,
        "OLD002": VerificationStatus.UNAVAILABLE,
    }
    report = harness.run(SyncOptions(reconcile_limit=2))
    manifest, state = SnapshotStore(harness.snapshot).load()
    assert report.automatic_removal_count == 2
    assert [post.shortcode for post in manifest.posts] == ["OLD000", "OLD003", "OLD004"]
    assert state.reconciliation_cursor == 1
    for shortcode in ("OLD001", "OLD002"):
        assert not (harness.snapshot / "media" / shortcode).exists()
        assert shortcode not in harness.snapshot_text()
        assert shortcode not in repr(report)
    harness.client.verified.clear()
    harness.run(SyncOptions(reconcile_limit=2))
    assert harness.client.verified == ["OLD003", "OLD004"]


def test_explicit_deletion_preserves_pending_cursor_identity(tmp_path: Path) -> None:
    from sync.engine import SyncOptions

    harness = SyncHarness(tmp_path)
    harness.seed(5, cursor=3)
    harness.removals.write_text("OLD000\nOLD003\n", encoding="utf-8")
    harness.run(SyncOptions(reconcile_limit=1))
    assert harness.client.verified == ["OLD004"]
    assert SnapshotStore(harness.snapshot).load()[1].reconciliation_cursor == 0


@pytest.mark.parametrize("error", [TransientExhaustionError, LoginError, ThrottleError])
def test_verification_failure_rolls_back_prior_deletions_and_cursor(
    tmp_path: Path, error: type[Exception]
) -> None:
    harness = SyncHarness(tmp_path)
    harness.seed(3)
    harness.client.statuses = {"OLD000": VerificationStatus.PRIVATE, "OLD001": error("secret")}
    before = harness.snapshot_bytes()
    with pytest.raises(error):
        harness.run()
    assert harness.snapshot_bytes() == before
