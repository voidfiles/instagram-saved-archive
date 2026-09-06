from __future__ import annotations

from pathlib import Path

import pytest

from sync.instagram.errors import ValidationError
from sync.media.videos import VideoProbe, build_ffmpeg_command, fit_video


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ((1920, 1080), (1280, 720)),
        ((1080, 1920), (720, 1280)),
        ((1000, 1000), (720, 720)),
        ((640, 360), (640, 360)),
        ((639, 479), (638, 478)),
    ],
)
def test_fit_video_contains_even_dimensions_without_upscaling(
    source: tuple[int, int], expected: tuple[int, int]
) -> None:
    """Break caught: video output exceeds its orientation box, pads, or has odd dimensions."""
    dimensions = fit_video(*source)
    assert dimensions.as_tuple() == expected
    assert dimensions.width % 2 == 0
    assert dimensions.height % 2 == 0


@pytest.mark.parametrize("width,height", [(0, 10), (10, 0), (1, 1)])
def test_fit_video_rejects_geometry_that_cannot_produce_even_pixels(
    width: int, height: int
) -> None:
    """Break caught: invalid input produces an unusable H.264 frame size."""
    with pytest.raises(ValidationError):
        fit_video(width, height)


@pytest.mark.parametrize("has_audio", [False, True])
def test_ffmpeg_command_encodes_the_required_video_profile(tmp_path: Path, has_audio: bool) -> None:
    """Break caught: the transcoder loses the web profile, quality, or progressive-playback flags."""
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    probe = VideoProbe(width=1920, height=1080, duration_seconds=2.0, has_audio=has_audio)

    command = build_ffmpeg_command(probe, source, output)

    assert command[:7] == [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-n",
        "-i",
    ]
    assert command[7] == str(source)
    assert _option(command, "-vf") == "scale=1280:720:flags=lanczos"
    assert _option(command, "-c:v") == "libx264"
    assert _option(command, "-preset") == "medium"
    assert _option(command, "-crf") == "24"
    assert _option(command, "-pix_fmt") == "yuv420p"
    assert _option(command, "-movflags") == "+faststart"
    assert command[-1] == str(output)
    if has_audio:
        assert _option(command, "-c:a") == "aac"
        assert _option(command, "-b:a") == "128k"
        assert "-an" not in command
    else:
        assert "-an" in command
        assert "-c:a" not in command


def _option(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]
