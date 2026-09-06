from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from sync.archive.models import AssetRecord, MediaKind, MediaRecord
from sync.instagram.errors import SizeError, TransientTransportError, ValidationError
from sync.instagram.models import MediaKind as SourceMediaKind
from sync.instagram.models import PublicPost, SourceMedia
from sync.media.processor import PostMediaProcessor
from sync.media.videos import probe_video, process_video


@pytest.fixture(scope="module", autouse=True)
def require_ffmpeg_6() -> None:
    completed = subprocess.run(
        ["ffmpeg", "-version"], check=True, capture_output=True, text=True, timeout=10
    )
    major = int(completed.stdout.splitlines()[0].split()[2].split(".")[0])
    assert major >= 6


@pytest.mark.parametrize(
    ("portrait", "with_audio", "source_size", "expected_size"),
    [
        (False, True, (1920, 1080), (1280, 720)),
        (True, False, (1080, 1440), (720, 960)),
    ],
)
def test_process_video_transcodes_and_probes_real_outputs(
    tmp_path: Path,
    portrait: bool,
    with_audio: bool,
    source_size: tuple[int, int],
    expected_size: tuple[int, int],
) -> None:
    """Break caught: a real encode has wrong bounds/profile/audio or invalid poster metadata."""
    source = tmp_path / "source.mp4"
    _make_video(source, source_size, with_audio=with_audio)
    shortcode = "PORTRAIT1" if portrait else "LANDSCAPE1"
    output = tmp_path / shortcode
    output.mkdir()
    primary = output / "00.mp4"
    poster = output / "00-poster.webp"

    record = process_video(source, primary, poster)

    encoded = probe_video(primary)
    assert (encoded.width, encoded.height) == expected_size
    assert encoded.width % 2 == encoded.height % 2 == 0
    assert encoded.codec_name == "h264"
    assert encoded.pixel_format == "yuv420p"
    assert encoded.has_audio is with_audio
    if with_audio:
        assert encoded.audio_codec_name == "aac"
        assert encoded.audio_bit_rate is not None
        assert 110_000 <= encoded.audio_bit_rate <= 140_000
    else:
        assert encoded.audio_codec_name is None
        assert encoded.audio_bit_rate is None
    assert 1.8 <= encoded.duration_seconds <= 2.2
    assert primary.read_bytes().find(b"moov") < primary.read_bytes().find(b"mdat")
    with Image.open(poster) as decoded:
        decoded.load()
        assert decoded.format == "WEBP"
        assert max(decoded.size) <= 640
        expected_poster = (640, 360) if not portrait else (480, 640)
        assert decoded.size == expected_poster
        assert not decoded.getexif()
    assert record.position == 0
    assert record.kind is MediaKind.VIDEO
    assert record.duration_seconds == encoded.duration_seconds
    _assert_asset(record.asset, primary, "video/mp4", expected_size)
    _assert_asset(record.preview, poster, "image/webp", (640, 360) if not portrait else (480, 640))


def test_probe_video_rejects_empty_and_malformed_files(tmp_path: Path) -> None:
    """Break caught: bad FFprobe input is converted into a manifest-facing video record."""
    for name, content in (("empty.mp4", b""), ("malformed.mp4", b"not video")):
        path = tmp_path / name
        path.write_bytes(content)
        with pytest.raises(ValidationError):
            probe_video(path)


@pytest.mark.parametrize(
    ("fixture", "expected_output", "expected_poster"),
    [
        ("rotated", (360, 640), (360, 640)),
        ("anamorphic", (768, 576), (640, 480)),
    ],
)
def test_process_video_normalizes_display_geometry(
    tmp_path: Path,
    fixture: str,
    expected_output: tuple[int, int],
    expected_poster: tuple[int, int],
) -> None:
    """Break caught: rotation or non-square pixels distort the MP4 and its poster."""
    source = tmp_path / f"{fixture}.mp4"
    if fixture == "rotated":
        coded = tmp_path / "coded.mp4"
        _make_video(coded, (640, 360), with_audio=False)
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-display_rotation:v:0",
                "90",
                "-i",
                str(coded),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(source),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
    else:
        _make_video(source, (720, 576), with_audio=False, video_filter="setsar=16/15")
    source_probe = probe_video(source)
    assert (source_probe.width, source_probe.height) == expected_output
    output = tmp_path / fixture.upper()
    output.mkdir()
    primary = output / "00.mp4"
    poster = output / "00-poster.webp"

    record = process_video(source, primary, poster)

    encoded = probe_video(primary)
    assert (encoded.width, encoded.height) == expected_output
    assert encoded.sample_aspect_ratio == (1, 1)
    assert encoded.rotation_degrees == 0
    with Image.open(poster) as decoded:
        decoded.load()
        assert decoded.size == expected_poster
    assert (record.asset.width, record.asset.height) == expected_output
    assert (record.preview.width, record.preview.height) == expected_poster


def test_process_video_rejects_truncated_input_and_removes_partial_outputs(tmp_path: Path) -> None:
    """Break caught: FFmpeg logs lost input packets but returns a shortened successful archive."""
    source = tmp_path / "truncated.mp4"
    _make_video(source, (640, 360), with_audio=False, pattern=True)
    with source.open("r+b") as stream:
        stream.truncate(source.stat().st_size * 9 // 10)
    output = tmp_path / "TRUNCATED"
    output.mkdir()

    with pytest.raises(ValidationError):
        process_video(source, output / "00.mp4", output / "00-poster.webp")

    assert source.exists()
    assert tuple(output.iterdir()) == ()


def test_process_video_path_collision_preserves_caller_owned_files(tmp_path: Path) -> None:
    """Break caught: preflight rejection deletes a colliding source or existing poster."""
    source = tmp_path / "00.mp4"
    _make_video(source, (320, 240), with_audio=False)
    poster = tmp_path / "00-poster.webp"
    poster.write_bytes(b"caller-owned-poster")
    source_before = source.read_bytes()
    poster_before = poster.read_bytes()

    with pytest.raises(ValidationError):
        process_video(source, source, poster)

    assert source.read_bytes() == source_before
    assert poster.read_bytes() == poster_before


def test_process_video_refuses_generated_output_over_95_mib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break caught: post-probe validation admits an MP4 above the repository-file ceiling."""
    source = tmp_path / "source.mp4"
    _make_video(source, (320, 240), with_audio=False)
    output = tmp_path / "TOOLARGE"
    output.mkdir()
    monkeypatch.setattr("sync.media.videos.MAX_GENERATED_FILE_BYTES", 1)

    with pytest.raises(SizeError, match="95 MiB"):
        process_video(source, output / "00.mp4", output / "00-poster.webp")

    assert tuple(output.iterdir()) == ()


def test_post_processor_returns_ordered_mixed_media_only_after_atomic_rename(
    tmp_path: Path,
) -> None:
    """Break caught: a successful post exposes wrong names/order or retains source downloads."""
    post = _post(
        "MIXED1",
        (
            SourceMedia(0, SourceMediaKind.IMAGE, "https://cdn.example/image.jpg"),
            SourceMedia(1, SourceMediaKind.VIDEO, "https://cdn.example/video.mp4"),
        ),
    )
    media_root = tmp_path / "media"
    media_root.mkdir()

    def download(source: SourceMedia, destination: Path) -> None:
        if source.kind is SourceMediaKind.IMAGE:
            with Image.new("RGB", (900, 600), "purple") as image:
                image.save(destination, format="JPEG")
        else:
            _make_video(destination, (320, 240), with_audio=False)

    records = PostMediaProcessor().process(post, download, media_root)

    assert tuple(record.position for record in records) == (0, 1)
    assert tuple(record.kind for record in records) == (MediaKind.IMAGE, MediaKind.VIDEO)
    assert tuple(sorted(path.name for path in (media_root / "MIXED1").iterdir())) == (
        "00-thumb.webp",
        "00.webp",
        "01-poster.webp",
        "01.mp4",
    )
    assert [record.asset.asset_path for record in records] == [
        "media/MIXED1/00.webp",
        "media/MIXED1/01.mp4",
    ]


def test_interrupted_carousel_leaves_no_post_directory_or_manifest_records(
    tmp_path: Path,
) -> None:
    """Break caught: a partial carousel becomes visible when a later download is interrupted."""
    post = _post(
        "INTERRUPTED1",
        tuple(
            SourceMedia(index, SourceMediaKind.IMAGE, f"https://cdn.example/{index}.jpg")
            for index in range(3)
        ),
    )
    media_root = tmp_path / "media"
    media_root.mkdir()
    manifest_records: list[MediaRecord] = []

    def download(source: SourceMedia, destination: Path) -> None:
        if source.position == 1:
            raise TransientTransportError("temporary failure")
        with Image.new("RGB", (32, 24), "orange") as image:
            image.save(destination, format="JPEG")

    with pytest.raises(TransientTransportError):
        manifest_records.extend(PostMediaProcessor().process(post, download, media_root))

    assert manifest_records == []
    assert not (media_root / "INTERRUPTED1").exists()
    assert tuple(media_root.iterdir()) == ()


def _make_video(
    path: Path,
    size: tuple[int, int],
    *,
    with_audio: bool,
    video_filter: str | None = None,
    pattern: bool = False,
) -> None:
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        (
            f"testsrc2=s={size[0]}x{size[1]}:r=25:d=2"
            if pattern
            else f"color=c=teal:s={size[0]}x{size[1]}:r=25:d=2"
        ),
    ]
    if with_audio:
        command.extend(["-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=2"])
    if video_filter is not None:
        command.extend(["-vf", video_filter])
    command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p"])
    if with_audio:
        command.extend(["-c:a", "aac", "-b:a", "128k", "-shortest"])
    else:
        command.append("-an")
    command.extend(["-movflags", "+faststart", "-f", "mp4", str(path)])
    subprocess.run(command, check=True, capture_output=True, timeout=60)


def _assert_asset(
    asset: AssetRecord, path: Path, mime_type: str, dimensions: tuple[int, int]
) -> None:
    assert asset.asset_path == f"media/{path.parent.name}/{path.name}"
    assert asset.mime_type == mime_type
    assert (asset.width, asset.height) == dimensions
    assert asset.byte_size == path.stat().st_size
    assert asset.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def _post(shortcode: str, media: tuple[SourceMedia, ...]) -> PublicPost:
    return PublicPost(
        shortcode=shortcode,
        creator_username="creator",
        creator_id=17,
        source_url=f"https://www.instagram.com/p/{shortcode}/",
        caption="caption",
        published_at=datetime(2025, 1, 2, tzinfo=UTC),
        media=media,
    )
