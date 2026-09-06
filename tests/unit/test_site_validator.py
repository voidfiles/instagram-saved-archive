"""Validation tests for finished static-site artifacts."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_archive_store import _manifest_for_files, _state, _write_files

from sync.archive.budget import BudgetStatus
from sync.archive.store import SnapshotStore
from sync.site.validator import validate_site_output


@pytest.fixture
def site_root(tmp_path: Path) -> Path:
    root = tmp_path / "site"
    root.mkdir()
    archive = root / "archive"
    store = SnapshotStore(archive)
    store.initialize()
    files = {
        "media/IMAGE0001/00.webp": b"primary bytes",
        "media/IMAGE0001/00-thumb.webp": b"preview bytes",
    }
    _write_files(archive, files)
    store.write_atomic(_manifest_for_files(files), _state())
    (root / "styles.css").write_text("body { color: black; }", encoding="utf-8")
    (root / "index.html").write_text(
        '<link rel="stylesheet" href="styles.css">'
        '<img src="archive/media/IMAGE0001/00.webp">'
        '<a href="https://www.instagram.com/p/IMAGE0001/">source</a>',
        encoding="utf-8",
    )
    return root


def test_validator_accepts_a_complete_static_artifact(site_root: Path) -> None:
    """Break caught: required documents or a valid archive are not checked before publishing."""
    result = validate_site_output(site_root)

    assert isinstance(result, BudgetStatus)
    assert result.level == "ok"


def test_validator_rejects_missing_index(site_root: Path) -> None:
    """Break caught: a publication lacking its entry document is accepted."""
    (site_root / "index.html").unlink()

    with pytest.raises(ValueError, match="index.html is required"):
        validate_site_output(site_root)


def test_validator_rejects_missing_stylesheet(site_root: Path) -> None:
    """Break caught: a publication lacking its required stylesheet is accepted."""
    (site_root / "styles.css").unlink()

    with pytest.raises(ValueError, match="styles.css is required"):
        validate_site_output(site_root)


def test_validator_rejects_symlinks_anywhere_in_the_artifact(site_root: Path) -> None:
    """Break caught: a symlink introduces bytes not covered by artifact validation."""
    external = site_root.parent / "external"
    external.write_text("not part of the artifact", encoding="utf-8")
    (site_root / "linked-file").symlink_to(external)

    with pytest.raises(ValueError, match="symlink is not allowed"):
        validate_site_output(site_root)


def test_validator_rejects_scripted_output(site_root: Path) -> None:
    """Break caught: executable script content reaches the published artifact."""
    (site_root / "index.html").write_text("<script>bad()</script>", encoding="utf-8")

    with pytest.raises(ValueError, match="scripts are not allowed"):
        validate_site_output(site_root)


def test_validator_rejects_event_handler_attributes(site_root: Path) -> None:
    """Break caught: HTML event attributes make a nominally static page executable."""
    (site_root / "index.html").write_text('<img onclick="bad()">', encoding="utf-8")

    with pytest.raises(ValueError, match="event attributes are not allowed"):
        validate_site_output(site_root)


def test_validator_rejects_external_media_urls(site_root: Path) -> None:
    """Break caught: media fetches data from an origin outside the verified archive."""
    (site_root / "index.html").write_text(
        '<img src="https://cdn.example.test/photo.webp">', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="external references are not allowed"):
        validate_site_output(site_root)


@pytest.mark.parametrize(
    "reference",
    [
        "archive/media/IMAGE0001/missing.webp",
        "../archive/media/IMAGE0001/00.webp",
        "archive%2Fmedia%2FIMAGE0001%2F00.webp",
        r"archive\\media\\IMAGE0001\\00.webp",
    ],
)
def test_validator_rejects_missing_or_unsafe_local_media_references(
    site_root: Path, reference: str
) -> None:
    """Break caught: an asset URL can escape the artifact or refer to nonexistent bytes."""
    (site_root / "index.html").write_text(f'<img src="{reference}">', encoding="utf-8")

    with pytest.raises(ValueError, match="local reference"):
        validate_site_output(site_root)
