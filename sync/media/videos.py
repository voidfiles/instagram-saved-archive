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
        width = _positive_int(video.get("width"))
        height = _positive_int(video.get("height"))
        duration = _positive_float(video.get("duration", file_format.get("duration")))
        codec_name = _optional_string(video.get("codec_name"))
        pixel_format = _optional_string(video.get("pix_fmt"))
        audio_codec = _optional_string(audio.get("codec_name")) if audio is not None else None
        audio_bit_rate = (
            _optional_positive_int(audio.get("bit_rate")) if audio is not None else None
        )
        return VideoProbe(
            width=width,
            height=height,
            duration_seconds=duration,
            has_audio=audio is not None,
            codec_name=codec_name,
            pixel_format=pixel_format,
            audio_codec_name=audio_codec,
            audio_bit_rate=audio_bit_rate,
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


def build_ffmpeg_command(probe: VideoProbe, source: Path, output: Path) -> list[str]:
    """Build the FFmpeg 6+ argument vector for one contained H.264 transcode."""
    dimensions = fit_video(probe.width, probe.height)
    command = [
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
        "-vf",
        f"scale={dimensions.width}:{dimensions.height}:flags=lanczos",
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
        ["-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", str(output)]
    )
    return command


def process_video(source: Path, primary: Path, poster: Path) -> MediaRecord:
    """Transcode, post-probe, and create a validated WebP poster for one video."""
    poster_source = poster.with_name(f".{poster.name}.png")
    outputs = (primary, poster, poster_source)
    try:
        if source in outputs or primary == poster:
            raise ValidationError("video input and output paths must be distinct")
        source_probe = probe_video(source)
        primary.parent.mkdir(parents=True, exist_ok=True)
        _run(build_ffmpeg_command(source_probe, source, primary), _ENCODE_TIMEOUT_SECONDS)
        _validate_video_size(primary)
        encoded = probe_video(primary)
        expected_dimensions = fit_video(source_probe.width, source_probe.height)
        if (
            (encoded.width, encoded.height) != expected_dimensions.as_tuple()
            or encoded.codec_name != "h264"
            or encoded.pixel_format != "yuv420p"
            or encoded.has_audio is not source_probe.has_audio
            or (encoded.has_audio and encoded.audio_codec_name != "aac")
        ):
            raise ValidationError("generated MP4 failed validation")
        _extract_poster(primary, poster_source)
        with Image.open(poster_source) as frame:
            frame.load()
            rgb = frame.convert("RGB")
        try:
            poster_dimensions = fit_image(*rgb.size, 640)
            _save_webp(rgb, poster, poster_dimensions)
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
        _remove_outputs(outputs)
        raise
    except (OSError, ValueError):
        _remove_outputs(outputs)
        raise ValidationError("video processing failed") from None
    except BaseException:
        _remove_outputs(outputs)
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


def _remove_outputs(paths: tuple[Path, ...]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
