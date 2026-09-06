"""Generate a deterministic, offline archive with real image and video assets."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw

from sync.archive.models import load_manifest, load_sync_state
from sync.archive.store import SnapshotStore


def build_sample_archive(output: Path) -> None:
    """Create 15 public-source sample records, with fixed dates and generated media."""
    store = SnapshotStore(output)
    store.initialize()
    posts = []
    epoch = datetime(2026, 9, 1, 12, tzinfo=UTC)
    palette = [(222, 187, 143), (140, 169, 148), (147, 170, 192)]
    video_bytes: bytes | None = None
    for index in range(15):
        shortcode = f"SAMPLE{index:04d}"
        directory = output / "media" / shortcode
        directory.mkdir(parents=True, exist_ok=True)
        kinds = [["image"], ["video"], ["image", "video", "image"]][index % 3]
        media = []
        for position, kind in enumerate(kinds):
            width, height = {0: (480, 640), 3: (320, 960)}.get(index, (640, 480))
            preview_width, preview_height = width // 2, height // 2
            scene = Image.new("RGB", (width, height), palette[index % len(palette)])
            drawing = ImageDraw.Draw(scene)
            drawing.ellipse((170, 80, 470, 380), fill=(246, 238, 221))
            drawing.text((24, 24), f"Saved / {index + 1:02d} / {position + 1}", fill=(38, 44, 42))
            preview_path = directory / f"{position}-preview.webp"
            scene.resize((preview_width, preview_height)).save(preview_path, "WEBP", quality=80)
            asset_path = directory / f"{position}.{'webp' if kind == 'image' else 'mp4'}"
            if kind == "image":
                scene.save(asset_path, "WEBP", quality=85)
            elif video_bytes is None:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        "color=c=0x8ca994:s=640x480:r=12",
                        "-t",
                        "1",
                        "-an",
                        "-c:v",
                        "libx264",
                        "-pix_fmt",
                        "yuv420p",
                        "-threads",
                        "1",
                        "-fflags",
                        "+bitexact",
                        "-flags:v",
                        "+bitexact",
                        "-map_metadata",
                        "-1",
                        "-movflags",
                        "+faststart",
                        str(asset_path),
                    ],
                    check=True,
                )
                video_bytes = asset_path.read_bytes()
            else:
                asset_path.write_bytes(video_bytes)

            def asset(path: Path, width: int, height: int, mime: str) -> dict[str, object]:
                data = path.read_bytes()
                return {
                    "asset_path": path.relative_to(output).as_posix(),
                    "mime_type": mime,
                    "width": width,
                    "height": height,
                    "byte_size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }

            item = {
                "position": position,
                "kind": kind,
                "asset": asset(
                    asset_path, width, height, "image/webp" if kind == "image" else "video/mp4"
                ),
                "preview": asset(preview_path, preview_width, preview_height, "image/webp"),
            }
            if kind == "video":
                item["duration_seconds"] = 1.0
            media.append(item)
        caption = f"Mountain light, café mornings, and quiet places. 日本語 🌿 — study {index + 1}."
        if index == 0:
            caption += "\n</script><script>alert(1)</script> & < > \u2028 \u2029"
        if index == 2:
            caption += "\n" + "A small detail worth returning to. " * 20
        posts.append(
            {
                "shortcode": shortcode,
                "creator_username": ["field.notes", "moving.light", "slow.studies"][index % 3],
                "creator_id": index % 3 + 1,
                "source_url": f"https://www.instagram.com/p/{shortcode}/",
                "caption": caption,
                "published_at": (epoch - timedelta(days=30 - index))
                .isoformat()
                .replace("+00:00", "Z"),
                "archived_at": (epoch - timedelta(hours=index)).isoformat().replace("+00:00", "Z"),
                "verified_at": epoch.isoformat().replace("+00:00", "Z"),
                "media_type": "carousel" if len(kinds) > 1 else kinds[0],
                "publication_state": "published",
                "unpublished_reason": None,
                "media": media,
            }
        )
    manifest = load_manifest({"schema_version": 1, "posts": posts})
    state = load_sync_state(
        {
            "schema_version": 1,
            "backfill_complete": True,
            "last_successful_sync_at": "2026-09-01T12:00:00Z",
            "last_complete_saved_feed_scan_at": "2026-09-01T12:00:00Z",
            "reconciliation_cursor": 0,
            "consecutive_known_threshold": 20,
            "archive_byte_size": 0,
        }
    )
    store.write_atomic(manifest, state)
    # The byte count includes metadata; converge when its own digit count stabilizes.
    while state.archive_byte_size != store.size_bytes():
        state = replace(state, archive_byte_size=store.size_bytes())
        store.write_atomic(manifest, state)
    store.validate_files(manifest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    build_sample_archive(parser.parse_args().output)
