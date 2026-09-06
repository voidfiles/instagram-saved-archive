"""Explicit archive-removal behavior tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from sync.archive.models import Manifest, load_manifest
from sync.archive.removals import apply_removals, parse_removals


@pytest.fixture
def sample_manifest() -> Manifest:
    """Two posts in archive order, sufficient to exercise stable removal results."""

    def asset(path: str, digest: str) -> dict[str, object]:
        return {
            "asset_path": path,
            "mime_type": "image/webp",
            "width": 1,
            "height": 1,
            "byte_size": 1,
            "sha256": digest * 64,
        }

    def post(shortcode: str, archived_at: str) -> dict[str, object]:
        return {
            "shortcode": shortcode,
            "creator_username": "creator",
            "creator_id": 1,
            "source_url": f"https://www.instagram.com/p/{shortcode}/",
            "caption": "post",
            "published_at": "2025-01-01T00:00:00Z",
            "archived_at": archived_at,
            "verified_at": "2025-01-03T00:00:00Z",
            "media_type": "image",
            "publication_state": "published",
            "unpublished_reason": None,
            "media": [
                {
                    "position": 0,
                    "kind": "image",
                    "asset": asset(f"media/{shortcode}/00.webp", "a"),
                    "preview": asset(f"media/{shortcode}/00-thumb.webp", "b"),
                }
            ],
        }

    return load_manifest(
        {
            "schema_version": 1,
            "posts": [
                post("IMAGE0001", "2025-01-03T00:00:00Z"),
                post("CAROUSEL1", "2025-01-02T00:00:00Z"),
            ],
        }
    )


def test_parse_removals_ignores_comments_blanks_and_duplicates(tmp_path: Path) -> None:
    """Break caught: operator annotations, whitespace, or repeated requests alter removals."""
    removals = tmp_path / "removals.txt"
    removals.write_text(
        "\n# rights request\n IMAGE0001 \n\nCAROUSEL1\nIMAGE0001\n", encoding="utf-8"
    )

    assert parse_removals(removals) == frozenset({"IMAGE0001", "CAROUSEL1"})


@pytest.mark.parametrize(
    "content", ["bad shortcode!\n", "has space\n", "# not a comment\nvalid/invalid\n"]
)
def test_parse_removals_rejects_invalid_shortcode_lines(tmp_path: Path, content: str) -> None:
    """Break caught: invalid removal input is silently interpreted as a valid shortcode."""
    removals = tmp_path / "removals.txt"
    removals.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError):
        parse_removals(removals)


def test_explicit_removal_is_idempotent(sample_manifest: Manifest, tmp_path: Path) -> None:
    """Break caught: applying the same removal twice reports or changes a post again."""
    removals = tmp_path / "removals.txt"
    removals.write_text("# rights request\nCAROUSEL1\nCAROUSEL1\n", encoding="utf-8")

    once, deleted = apply_removals(sample_manifest, parse_removals(removals))
    twice, deleted_again = apply_removals(once, parse_removals(removals))

    assert [post.shortcode for post in once.posts] == ["IMAGE0001"]
    assert deleted == ("CAROUSEL1",)
    assert twice == once
    assert deleted_again == ()


def test_removals_return_media_directory_names_in_manifest_order(sample_manifest: Manifest) -> None:
    """Break caught: callers receive a non-deterministic or incomplete media-directory deletion list."""
    remaining, deleted = apply_removals(sample_manifest, frozenset({"CAROUSEL1", "IMAGE0001"}))

    assert remaining.posts == ()
    assert deleted == ("IMAGE0001", "CAROUSEL1")
