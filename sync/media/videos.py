"""FFprobe inspection and deterministic web-oriented MP4 processing."""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from sync.archive.models import MAX_GENERATED_FILE_BYTES, MediaKind, MediaRecord
from sync.instagram.errors import SizeError, ValidationError

from .images import (
    Dimensions,
    _asset_record,
    _position_from_primary,
    _save_webp,
    _validate_output_ownership,
    _validate_size,
    fit_image,
)

_PROBE_TIMEOUT_SECONDS = 30
_ENCODE_TIMEOUT_SECONDS = 300


@dataclass(frozen=True, slots=True)
class VideoProbe:
    width: int
    height: int
    duration_seconds: float
    has_audio: bool
    codec_name: str | None = None
    pixel_format: str | None = None
    audio_codec_name: str | None = None
    audio_bit_rate: int | None = None
    sample_aspect_ratio: tuple[int, int] = (1, 1)
    rotation_degrees: int = 0


def probe_video(path: Path) -> VideoProbe:
    """Read the first video/audio streams through bounded FFprobe JSON output."""
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise ValueError
        streams = payload.get("streams")
        file_format = payload.get("format")
        if not isinstance(streams, list) or not isinstance(file_format, dict):
            raise ValueError
        video = next(
            (
                stream
                for stream in streams
                if isinstance(stream, dict) and stream.get("codec_type") == "video"
            ),
            None,
        )
        audio = next(
            (
                stream
                for stream in streams
                if isinstance(stream, dict) and stream.get("codec_type") == "audio"
            ),
            None,
        )
        if video is None:
            raise ValueError
        coded_width = _positive_int(video.get("width"))
        coded_height = _positive_int(video.get("height"))
        sample_aspect_ratio = _sample_aspect_ratio(video.get("sample_aspect_ratio"))
        rotation_degrees = _rotation_degrees(video)
        dimensions = normalize_video_geometry(
            coded_width,
            coded_height,
            sample_aspect_ratio,
            rotation_degrees,
        )
        duration = _positive_float(video.get("duration", file_format.get("duration")))
        codec_name = _optional_string(video.get("codec_name"))
        pixel_format = _optional_string(video.get("pix_fmt"))
        audio_codec = _optional_string(audio.get("codec_name")) if audio is not None else None
        audio_bit_rate = (
            _optional_positive_int(audio.get("bit_rate")) if audio is not None else None
        )
        return VideoProbe(
            width=dimensions.width,
            height=dimensions.height,
            duration_seconds=duration,
            has_audio=audio is not None,
            codec_name=codec_name,
            pixel_format=pixel_format,
            audio_codec_name=audio_codec,
            audio_bit_rate=audio_bit_rate,
            sample_aspect_ratio=sample_aspect_ratio,
            rotation_degrees=rotation_degrees,
        )
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        raise ValidationError("video probe failed") from None


def fit_video(width: int, height: int) -> Dimensions:
    """Fit a video inside its orientation-specific box using even dimensions."""
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in (width, height)
    ):
        raise ValidationError("video dimensions must be positive integers")
    maximum_width, maximum_height = (1280, 720) if width >= height else (720, 1280)
    scale = min(1.0, maximum_width / width, maximum_height / height)
    fitted_width = math.floor(width * scale) // 2 * 2
    fitted_height = math.floor(height * scale) // 2 * 2
    if fitted_width <= 0 or fitted_height <= 0:
        raise ValidationError("video dimensions cannot be represented as even pixels")
    return Dimensions(fitted_width, fitted_height)


def normalize_video_geometry(
    width: int,
    height: int,
    sample_aspect_ratio: str | tuple[int, int],
    rotation_degrees: int,
) -> Dimensions:
    """Convert coded geometry to square-pixel display geometry before fitting."""
    coded = _positive_dimensions(width, height)
    ratio = (
        Dimensions(*_sample_aspect_ratio(sample_aspect_ratio))
        if isinstance(sample_aspect_ratio, str)
        else _positive_dimensions(*sample_aspect_ratio)
    )
    if not isinstance(rotation_degrees, int) or isinstance(rotation_degrees, bool):
        raise ValidationError("video rotation must be an integer")
    rotation = rotation_degrees % 360
    if rotation not in {0, 90, 180, 270}:
        raise ValidationError("video rotation must be a multiple of 90 degrees")
    square_width = max(1, round(coded.width * ratio.width / ratio.height))
    display = Dimensions(square_width, coded.height)
    if rotation in {90, 270}:
        return Dimensions(display.height, display.width)
    return display


def build_ffmpeg_command(probe: VideoProbe, source: Path, output: Path) -> list[str]:
    """Build the FFmpeg 6+ argument vector for one contained H.264 transcode."""
    dimensions = fit_video(probe.width, probe.height)
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",
        "-err_detect",
        "explode",
        "-autorotate",
        "1",
        "-n",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-vf",
        f"scale={dimensions.width}:{dimensions.height}:flags=lanczos,setsar=1",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "24",
        "-pix_fmt",
        "yuv420p",
    ]
    if probe.has_audio:
        command.extend(["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"])
    else:
        command.append("-an")
    command.extend(
        [
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-metadata:s:v:0",
            "rotate=0",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return command


def process_video(source: Path, primary: Path, poster: Path) -> MediaRecord:
    """Transcode, post-probe, and create a validated WebP poster for one video."""
    poster_source = poster.with_name(f".{poster.name}.png")
    outputs = (primary, poster, poster_source)
    _validate_output_ownership(source, outputs)
    created: set[Path] = set()
    try:
        source_probe = probe_video(source)
        primary.parent.mkdir(parents=True, exist_ok=True)
        try:
            _run(build_ffmpeg_command(source_probe, source, primary), _ENCODE_TIMEOUT_SECONDS)
        finally:
            _claim_created(primary, created)
        _validate_video_size(primary)
        encoded = probe_video(primary)
        expected_dimensions = fit_video(source_probe.width, source_probe.height)
        if (
            (encoded.width, encoded.height) != expected_dimensions.as_tuple()
            or encoded.codec_name != "h264"
            or encoded.pixel_format != "yuv420p"
            or encoded.sample_aspect_ratio != (1, 1)
            or encoded.rotation_degrees != 0
            or encoded.has_audio is not source_probe.has_audio
            or (encoded.has_audio and encoded.audio_codec_name != "aac")
        ):
            raise ValidationError("generated MP4 failed validation")
        try:
            _extract_poster(primary, poster_source)
        finally:
            _claim_created(poster_source, created)
        with Image.open(poster_source) as frame:
            frame.load()
            rgb = frame.convert("RGB")
        try:
            poster_dimensions = fit_image(*rgb.size, 640)
            try:
                _save_webp(rgb, poster, poster_dimensions)
            finally:
                _claim_created(poster, created)
        finally:
            rgb.close()
        poster_source.unlink(missing_ok=True)
        primary_asset = _asset_record(primary, "video/mp4", expected_dimensions)
        poster_asset = _asset_record(poster, "image/webp", poster_dimensions)
        return MediaRecord(
            position=_position_from_primary(primary),
            kind=MediaKind.VIDEO,
            asset=primary_asset,
            preview=poster_asset,
            duration_seconds=encoded.duration_seconds,
        )
    except (ValidationError, SizeError):
        _remove_outputs(created)
        raise
    except (OSError, ValueError):
        _remove_outputs(created)
        raise ValidationError("video processing failed") from None
    except BaseException:
        _remove_outputs(created)
        raise


def _extract_poster(source: Path, output: Path) -> None:
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            str(output),
        ],
        _PROBE_TIMEOUT_SECONDS,
    )


def _validate_video_size(path: Path) -> None:
    _validate_size(path)
    if path.stat().st_size > MAX_GENERATED_FILE_BYTES:
        raise SizeError("generated media file must not exceed 95 MiB")


def _run(command: list[str], timeout: int) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ValidationError("video encoding failed") from None


def _positive_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError
    return value


def _positive_dimensions(width: object, height: object) -> Dimensions:
    return Dimensions(_positive_int(width), _positive_int(height))


def _sample_aspect_ratio(value: object) -> tuple[int, int]:
    if value in {None, "N/A"}:
        return 1, 1
    if not isinstance(value, str):
        if (
            isinstance(value, tuple)
            and len(value) == 2
            and all(isinstance(item, int) for item in value)
        ):
            numerator, denominator = value
        else:
            raise ValueError
    else:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError
        numerator, denominator = (int(part) for part in parts)
    if numerator <= 0 or denominator <= 0:
        raise ValueError
    divisor = math.gcd(numerator, denominator)
    return numerator // divisor, denominator // divisor


def _rotation_degrees(video: dict[object, object]) -> int:
    side_data = video.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data:
            if isinstance(item, dict) and "rotation" in item:
                return _normalized_rotation(item["rotation"])
    tags = video.get("tags")
    if isinstance(tags, dict) and "rotate" in tags:
        return _normalized_rotation(tags["rotate"])
    return 0


def _normalized_rotation(value: object) -> int:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValueError
    converted = float(value)
    rounded = round(converted)
    if not math.isfinite(converted) or not math.isclose(converted, rounded, abs_tol=0.01):
        raise ValueError
    normalized = rounded % 360
    if normalized not in {0, 90, 180, 270}:
        raise ValueError
    return normalized


def _positive_float(value: object) -> float:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValueError
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise ValueError
    return converted


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_positive_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError
    converted = int(value)
    if converted <= 0:
        raise ValueError
    return converted


def _claim_created(path: Path, created: set[Path]) -> None:
    if path.exists():
        created.add(path)


def _remove_outputs(paths: set[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
