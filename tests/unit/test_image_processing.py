from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image

from sync.archive.models import MAX_GENERATED_FILE_BYTES, MediaKind
from sync.instagram.errors import SizeError, ValidationError
from sync.media.images import fit_image, process_image


@pytest.mark.parametrize(
    ("source", "limit", "expected"),
    [
        ((3200, 1800), 1600, (1600, 900)),
        ((600, 900), 1600, (600, 900)),
        ((800, 800), 640, (640, 640)),
        ((321, 123), 1600, (321, 123)),
    ],
)
def test_fit_image_never_upscales(
    source: tuple[int, int], limit: int, expected: tuple[int, int]
) -> None:
    """Break caught: geometry distorts an aspect ratio or enlarges a small source."""
    assert fit_image(*source, limit).as_tuple() == expected


@pytest.mark.parametrize("width,height,limit", [(0, 10, 10), (10, 0, 10), (10, 10, 0)])
def test_fit_image_rejects_zero_sized_geometry(width: int, height: int, limit: int) -> None:
    """Break caught: invalid dimensions reach Pillow as a zero-sized output."""
    with pytest.raises(ValidationError):
        fit_image(width, height, limit)


def test_process_image_writes_decodable_stripped_webp_renditions(tmp_path: Path) -> None:
    """Break caught: image output is unbounded, undecodable, upscaled, or retains metadata."""
    source_path = tmp_path / "source.jpg"
    with Image.new("RGB", (2000, 1000), (10, 80, 160)) as image:
        exif = Image.Exif()
        exif[0x010E] = "private source description"
        image.save(
            source_path, format="JPEG", quality=95, exif=exif, icc_profile=b"private-profile"
        )
    output = tmp_path / "IMAGE1"
    output.mkdir()
    primary = output / "00.webp"
    preview = output / "00-thumb.webp"

    record = process_image(source_path, primary, preview)

    assert record.position == 0
    assert record.kind is MediaKind.IMAGE
    assert record.duration_seconds is None
    assert record.asset.asset_path == "media/IMAGE1/00.webp"
    assert record.preview.asset_path == "media/IMAGE1/00-thumb.webp"
    assert (record.asset.width, record.asset.height) == (1600, 800)
    assert (record.preview.width, record.preview.height) == (640, 320)
    for path, asset, expected_size in (
        (primary, record.asset, (1600, 800)),
        (preview, record.preview, (640, 320)),
    ):
        with Image.open(path) as decoded:
            decoded.load()
            assert decoded.format == "WEBP"
            assert decoded.size == expected_size
            assert not decoded.getexif()
            assert "icc_profile" not in decoded.info
            assert "xmp" not in decoded.info
        assert asset.mime_type == "image/webp"
        assert asset.byte_size == path.stat().st_size
        assert asset.byte_size > 0
        assert asset.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_process_image_applies_exif_orientation_before_fitting(tmp_path: Path) -> None:
    """Break caught: portrait/landscape sizing uses encoded pixels before EXIF rotation."""
    source_path = tmp_path / "rotated.jpg"
    with Image.new("RGB", (100, 200), "red") as image:
        exif = Image.Exif()
        exif[0x0112] = 6
        image.save(source_path, exif=exif)
    output = tmp_path / "ROTATED1"
    output.mkdir()

    record = process_image(source_path, output / "04.webp", output / "04-thumb.webp")

    assert record.position == 4
    assert (record.asset.width, record.asset.height) == (200, 100)
    assert (record.preview.width, record.preview.height) == (200, 100)
    with Image.open(output / "04.webp") as decoded:
        assert decoded.size == (200, 100)
        assert not decoded.getexif()


@pytest.mark.parametrize("fixture", ["animated", "unsupported", "malformed", "empty"])
def test_process_image_rejects_unsupported_or_invalid_sources(tmp_path: Path, fixture: str) -> None:
    """Break caught: a non-still or malformed download is admitted to the manifest."""
    source_path = tmp_path / "source"
    if fixture == "animated":
        frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
        frames[0].save(source_path, format="GIF", save_all=True, append_images=frames[1:])
        for frame in frames:
            frame.close()
    elif fixture == "unsupported":
        with Image.new("RGB", (8, 8), "green") as image:
            image.save(source_path, format="BMP")
    elif fixture == "malformed":
        source_path.write_bytes(b"not an image")
    else:
        source_path.touch()
    output = tmp_path / "BADIMAGE"
    output.mkdir()

    with pytest.raises(ValidationError):
        process_image(source_path, output / "00.webp", output / "00-thumb.webp")

    assert tuple(output.iterdir()) == ()


def test_process_image_refuses_a_generated_file_over_95_mib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break caught: post-encode validation records a generated file above the hard ceiling."""
    source_path = tmp_path / "source.png"
    with Image.effect_noise((512, 512), 100) as image:
        image.convert("RGB").save(source_path)
    output = tmp_path / "TOOLARGE"
    output.mkdir()
    primary = output / "00.webp"
    preview = output / "00-thumb.webp"
    monkeypatch.setattr("sync.media.images.MAX_GENERATED_FILE_BYTES", 1)

    with pytest.raises(SizeError, match="95 MiB"):
        process_image(source_path, primary, preview)

    assert MAX_GENERATED_FILE_BYTES == 95 * 1024 * 1024
    assert not primary.exists()
    assert not preview.exists()


def test_process_image_path_collision_preserves_caller_owned_files(tmp_path: Path) -> None:
    """Break caught: preflight rejection deletes a colliding source or existing output."""
    source = tmp_path / "00.webp"
    with Image.new("RGB", (24, 16), "navy") as image:
        image.save(source, format="WEBP")
    preview = tmp_path / "00-thumb.webp"
    preview.write_bytes(b"caller-owned-preview")
    source_before = source.read_bytes()
    preview_before = preview.read_bytes()

    with pytest.raises(ValidationError):
        process_image(source, source, preview)

    assert source.read_bytes() == source_before
    assert preview.read_bytes() == preview_before
