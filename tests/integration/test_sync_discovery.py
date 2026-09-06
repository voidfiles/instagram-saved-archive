from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sync.archive.store import SnapshotStore
from sync.instagram.errors import UnavailablePostError
from sync.instagram.models import PublicPost, SavedCandidate
from tests.integration.fakes import NOW, SyncHarness, public_image


def test_fresh_run_limits_to_fifty_and_completes_only_on_exhaustion(tmp_path: Path) -> None:
    harness = SyncHarness(tmp_path)
    harness.client.saved = [public_image(f"NEW{i:03}") for i in range(51)]
    first = harness.run()
    assert first.new_count == 50
    assert not first.backfill_complete
    assert harness.client.materialized == 50
    assert SnapshotStore(harness.snapshot).load()[1].last_complete_saved_feed_scan_at is None
    second = harness.run()
    assert second.new_count == 1
    assert second.known_count == 50
    assert second.backfill_complete
    assert SnapshotStore(harness.snapshot).load()[1].last_complete_saved_feed_scan_at == NOW


def test_exact_limit_does_not_claim_exhaustion(tmp_path: Path) -> None:
    from sync.engine import SyncOptions

    harness = SyncHarness(tmp_path)
    harness.client.saved = [public_image("ONLY")]
    assert not harness.run(SyncOptions(max_new_posts=1)).backfill_complete
    assert harness.run().backfill_complete


@pytest.mark.parametrize("complete", [False, True])
def test_known_threshold_applies_only_after_backfill(tmp_path: Path, complete: bool) -> None:
    harness = SyncHarness(tmp_path)
    harness.seed(25, complete=complete)
    harness.client.saved = harness.sequence(25) + [public_image("NEWPUBLIC")]
    report = harness.run()
    assert report.new_count == (0 if complete else 1)
    assert report.known_count == (20 if complete else 25)
    assert harness.client.downloads == (0 if complete else 1)


def test_private_candidate_resets_known_counter_without_persisting_metadata(tmp_path: Path) -> None:
    harness = SyncHarness(tmp_path)
    harness.seed(19, complete=True)
    harness.client.saved = (
        harness.sequence(19)
        + [SavedCandidate({"shortcode": "SECRET1", "owner": "private_owner"})]
        + harness.sequence(19)
        + [public_image("NEWPUBLIC")]
    )
    report = harness.run()
    assert report.new_count == 1
    assert report.private_skip_count == 1
    assert report.known_count == 38
    for value in ("SECRET1", "private_owner"):
        assert value not in harness.snapshot_text()
        assert value not in repr(report)
        assert all(value not in name for name in harness.snapshot_bytes())


def test_explicit_removals_precede_discovery_and_count_as_known(tmp_path: Path) -> None:
    harness = SyncHarness(tmp_path)
    harness.seed(20, complete=True)
    harness.removals.write_text("\n".join(f"OLD{i:03}" for i in range(20)), encoding="utf-8")
    harness.client.saved = harness.sequence(20) + [public_image("NOTREACHED")]
    report = harness.run()
    assert report.explicit_removal_count == 20
    assert report.known_count == 20
    assert report.new_count == 0
    assert SnapshotStore(harness.snapshot).load()[0].posts == ()
    assert list((harness.snapshot / "media").iterdir()) == []
    assert "OLD000" not in harness.snapshot_text()
    assert harness.client.downloads == 0
    assert harness.client.verified == []


def test_duplicate_candidates_and_repeated_runs_do_not_redownload(tmp_path: Path) -> None:
    harness = SyncHarness(tmp_path)
    harness.client.saved = [public_image("DUPLICATE")] * 3
    assert harness.run().new_count == 1
    before = harness.snapshot_bytes()
    report = harness.run()
    assert report.new_count == 0
    assert report.known_count == 3
    assert harness.client.downloads == 1
    assert harness.snapshot_bytes() == before


@pytest.mark.parametrize(
    ("caption", "expected"),
    [
        ("  Cafe\u0301 👩‍👩‍👧 #tag  ", "  Café 👩‍👩‍👧 #tag  "),
        ("First\r\n\r\nSecond\rThird\n", "First\n\nSecond\nThird\n"),
    ],
)
def test_ingestion_normalizes_caption_and_repeat_sync_stays_duplicate_free(
    tmp_path: Path, caption: str, expected: str
) -> None:
    """Break caught: raw caption normalization fails final validation after ingestion."""
    harness = SyncHarness(tmp_path)
    post = public_image("CAPTION").token
    assert isinstance(post, PublicPost)
    harness.client.saved = [SavedCandidate(replace(post, caption=caption))] * 2

    assert harness.run().new_count == 1
    before = harness.snapshot_bytes()
    posts = SnapshotStore(harness.snapshot).load()[0].posts
    assert len(posts) == 1
    assert posts[0].caption == expected
    report = harness.run()
    assert (report.new_count, report.known_count) == (0, 2)
    assert harness.client.downloads == 1
    assert harness.snapshot_bytes() == before


def test_full_scan_overrides_known_threshold(tmp_path: Path) -> None:
    from sync.engine import SyncOptions

    harness = SyncHarness(tmp_path)
    harness.seed(25, complete=True)
    harness.client.saved = harness.sequence(25) + [public_image("NEWPUBLIC")]
    report = harness.run(SyncOptions(full_scan=True))
    assert report.new_count == 1
    assert report.known_count == 25


@pytest.mark.parametrize(("requested", "expected"), [(-3, 1), (0, 1), (2, 2), (100, 50)])
def test_manual_batch_limits_are_clamped(tmp_path: Path, requested: int, expected: int) -> None:
    from sync.engine import SyncOptions

    harness = SyncHarness(tmp_path)
    harness.client.saved = [public_image(f"NEW{i:03}") for i in range(51)]
    assert harness.run(SyncOptions(max_new_posts=requested)).new_count == expected


def test_private_and_unavailable_candidates_do_not_consume_eligible_limit(tmp_path: Path) -> None:
    from sync.engine import SyncOptions

    harness = SyncHarness(tmp_path)
    harness.client.saved = [
        SavedCandidate("secret-private"),
        SavedCandidate(UnavailablePostError("not public")),
        public_image("PUBLIC"),
    ]
    report = harness.run(SyncOptions(max_new_posts=1))
    assert (report.new_count, report.private_skip_count, report.unavailable_skip_count) == (1, 1, 1)


def test_snapshot_size_includes_its_own_serialized_state(tmp_path: Path) -> None:
    harness = SyncHarness(tmp_path)
    harness.client.saved = [public_image("PUBLIC")]
    report = harness.run()
    actual = sum(len(data) for data in harness.snapshot_bytes().values())
    state = SnapshotStore(harness.snapshot).load()[1]
    assert report.snapshot_bytes == state.archive_byte_size == actual
    assert state.last_successful_sync_at == NOW
