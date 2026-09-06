"""Static archive rendering tests."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
from pathlib import Path

import pytest

from sync.archive.budget import BudgetStatus
from sync.archive.models import load_manifest, load_sync_state
from sync.archive.store import SnapshotStore
from sync.site import build_site, renderer


class _MarkupInspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.start_tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.start_tags.append((tag, dict(attrs)))


def _asset(path: str, content: bytes, mime_type: str) -> dict[str, object]:
    return {
        "asset_path": path,
        "mime_type": mime_type,
        "width": 640,
        "height": 480,
        "byte_size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    files = {
        'media/IMAGE0001/00 café & "cover".webp': b"full image",
        "media/IMAGE0001/00 preview.webp": b"image preview",
        "media/VIDEO0001/00.mp4": b"video",
        "media/VIDEO0001/00 poster.webp": b"video poster",
        "media/MIXED0001/00.webp": b"carousel image",
        "media/MIXED0001/00 preview.webp": b"carousel image preview",
        "media/MIXED0001/01.mp4": b"carousel video",
        "media/MIXED0001/01 preview.webp": b"carousel video poster",
    }
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def media(position: int, kind: str, asset_path: str, preview_path: str) -> dict[str, object]:
        item: dict[str, object] = {
            "position": position,
            "kind": kind,
            "asset": _asset(
                asset_path,
                files[asset_path],
                "video/mp4" if kind == "video" else "image/webp",
            ),
            "preview": _asset(preview_path, files[preview_path], "image/webp"),
        }
        if kind == "video":
            item["duration_seconds"] = 1.5
        return item

    common = {
        "creator_id": 1,
        "published_at": "2026-08-01T12:00:00Z",
        "verified_at": "2026-09-04T12:00:00Z",
        "publication_state": "published",
        "unpublished_reason": None,
    }
    manifest = load_manifest(
        {
            "schema_version": 1,
            "posts": [
                {
                    **common,
                    "shortcode": "MIXED0001",
                    "creator_username": "mixed.creator",
                    "source_url": "https://www.instagram.com/p/MIXED0001/",
                    "caption": "Mixed media",
                    "archived_at": "2026-09-01T12:00:00Z",
                    "media_type": "carousel",
                    "media": [
                        media(
                            0,
                            "image",
                            "media/MIXED0001/00.webp",
                            "media/MIXED0001/00 preview.webp",
                        ),
                        media(
                            1,
                            "video",
                            "media/MIXED0001/01.mp4",
                            "media/MIXED0001/01 preview.webp",
                        ),
                    ],
                },
                {
                    **common,
                    "shortcode": "VIDEO0001",
                    "creator_username": "video.creator",
                    "source_url": "https://www.instagram.com/p/VIDEO0001/",
                    "caption": "A video",
                    "archived_at": "2026-09-02T12:00:00Z",
                    "media_type": "video",
                    "media": [
                        media(
                            0,
                            "video",
                            "media/VIDEO0001/00.mp4",
                            "media/VIDEO0001/00 poster.webp",
                        )
                    ],
                },
                {
                    **common,
                    "shortcode": "IMAGE0001",
                    "creator_username": "image.creator",
                    "source_url": "https://www.instagram.com/p/IMAGE0001/",
                    "caption": "Unsafe <tag> & \"double\" 'single' café",
                    "archived_at": "2026-09-03T12:00:00Z",
                    "media_type": "image",
                    "media": [
                        media(
                            0,
                            "image",
                            'media/IMAGE0001/00 café & "cover".webp',
                            "media/IMAGE0001/00 preview.webp",
                        )
                    ],
                },
            ],
        }
    )
    state = load_sync_state(
        {
            "schema_version": 1,
            "backfill_complete": True,
            "last_successful_sync_at": "2026-09-04T12:00:00Z",
            "last_complete_saved_feed_scan_at": "2026-09-04T12:00:00Z",
            "reconciliation_cursor": 0,
            "consecutive_known_threshold": 20,
            "archive_byte_size": 0,
        }
    )
    store = SnapshotStore(root)
    store.write_atomic(manifest, state)
    return root


def test_build_site_renders_all_media_without_javascript(archive: Path, tmp_path: Path) -> None:
    """Break caught: the static document omits content or introduces executable markup."""
    destination = tmp_path / "dist"

    result = build_site(archive, destination, title="A < Saved Archive")

    document = (destination / "index.html").read_text(encoding="utf-8")
    inspector = _MarkupInspector()
    inspector.feed(document)
    tags = inspector.start_tags
    assert isinstance(result, BudgetStatus)
    assert sorted(path.name for path in destination.iterdir()) == [
        "archive",
        "index.html",
        "styles.css",
    ]
    assert "A &lt; Saved Archive" in document
    assert ("a", {"class": "skip-link", "href": "#posts"}) in tags
    assert ("main", {"id": "posts"}) in tags
    assert sum(tag == "article" for tag, _ in tags) == 3
    assert document.index("@image.creator") < document.index("@video.creator")
    assert document.index("@video.creator") < document.index("@mixed.creator")
    assert "Unsafe &lt;tag&gt; &amp; &quot;double&quot; &#x27;single&#x27; café" in document
    assert 'datetime="2026-08-01T12:00:00Z"' in document
    assert 'datetime="2026-09-04T12:00:00Z"' in document

    anchors = [attrs for tag, attrs in tags if tag == "a"]
    assert {
        "href": "https://www.instagram.com/image.creator/",
        "rel": "noopener noreferrer",
    } in anchors
    assert {
        "href": "https://www.instagram.com/p/IMAGE0001/",
        "rel": "noopener noreferrer",
    } in anchors

    images = [attrs for tag, attrs in tags if tag == "img"]
    assert images
    assert all(image["loading"] == "lazy" for image in images)
    assert all(image["width"] == "640" and image["height"] == "480" for image in images)
    videos = [attrs for tag, attrs in tags if tag == "video"]
    assert len(videos) == 2
    assert all("controls" in video for video in videos)
    assert all(video["poster"].startswith("archive/") for video in videos)
    sources = [attrs for tag, attrs in tags if tag == "source"]
    assert len(sources) == 2
    assert all(source["src"].startswith("archive/") for source in sources)
    figures = [attrs for tag, attrs in tags if tag == "figure"]
    assert {figure.get("aria-label") for figure in figures} >= {"Item 1 of 2", "Item 2 of 2"}
    assert "<script" not in document.casefold()
    assert not any(name.casefold().startswith("on") for _, attrs in tags for name in attrs)


def test_build_site_percent_encodes_asset_urls_by_segment(archive: Path, tmp_path: Path) -> None:
    """Break caught: asset references expose raw unsafe characters or alter path boundaries."""
    destination = tmp_path / "dist"

    build_site(archive, destination)

    inspector = _MarkupInspector()
    inspector.feed((destination / "index.html").read_text(encoding="utf-8"))
    asset_urls = [
        value
        for tag, attrs in inspector.start_tags
        for name, value in attrs.items()
        if tag in {"img", "source", "video"} and name in {"poster", "src"} and value is not None
    ]
    assert "archive/media/IMAGE0001/00%20caf%C3%A9%20%26%20%22cover%22.webp" in asset_urls
    assert all("\\" not in url and "/../" not in url and "/./" not in url for url in asset_urls)
    assert all("café" not in url and '"' not in url and " " not in url for url in asset_urls)


def test_render_failure_preserves_existing_destination(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break caught: a staging failure destroys the previously published artifact."""
    destination = tmp_path / "dist"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("previous publication", encoding="utf-8")

    def fail_document_write(path: Path, document: str) -> None:
        assert path.parent.name.startswith(".dist.build-")
        assert document
        raise OSError("injected document write failure")

    monkeypatch.setattr(renderer, "_write_document", fail_document_write)

    with pytest.raises(OSError, match="injected document write failure"):
        build_site(archive, destination)

    assert sentinel.read_text(encoding="utf-8") == "previous publication"
    assert sorted(item.name for item in destination.iterdir()) == ["keep.txt"]


def test_install_and_rollback_rename_failures_preserve_recoverable_previous_site(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break caught: failed installation and rollback make the previous site unavailable."""
    destination = tmp_path / "dist"
    destination.mkdir()
    (destination / "keep.txt").write_text("previous publication", encoding="utf-8")
    original_rename = Path.rename
    destination_rename_attempts = 0

    def fail_destination_renames(path: Path, target: Path) -> Path:
        nonlocal destination_rename_attempts
        if target == destination:
            destination_rename_attempts += 1
            raise OSError("injected install or rollback failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_destination_renames)

    with pytest.raises(OSError, match="injected install or rollback failure"):
        build_site(archive, destination)

    backups = list(tmp_path.glob(".dist.old-*/keep.txt"))
    assert destination_rename_attempts == 2
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "previous publication"
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "previous publication"
