"""All-or-nothing media processing for a complete Instagram post."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from sync.archive.models import MediaRecord
from sync.instagram.errors import ValidationError
from sync.instagram.models import MediaKind, PublicPost, SourceMedia

from .images import process_image
from .videos import process_video

_SHORTCODE = re.compile(r"[A-Za-z0-9_-]+\Z")
DownloadMedia = Callable[[SourceMedia, Path], None]


class PostMediaProcessor:
    """Stage every item for one post and expose the directory with one rename."""

    def process(
        self,
        post: PublicPost,
        download: DownloadMedia,
        temporary_root: Path,
    ) -> tuple[MediaRecord, ...]:
        if not _SHORTCODE.fullmatch(post.shortcode):
            raise ValidationError("post shortcode is invalid")
        if not post.media:
            raise ValidationError("post media must not be empty")
        if tuple(item.position for item in post.media) != tuple(range(len(post.media))):
            raise ValidationError(
                "post media positions must be ordered, contiguous, and zero-based"
            )
        temporary_root.mkdir(parents=True, exist_ok=True)
        final_directory = temporary_root / post.shortcode
        if final_directory.exists():
            raise FileExistsError(final_directory)
        working_root = Path(tempfile.mkdtemp(prefix=f".{post.shortcode}.", dir=temporary_root))
        staged_directory = working_root / post.shortcode
        staged_directory.mkdir()
        committed = False
        try:
            records: list[MediaRecord] = []
            for item in post.media:
                source_path = working_root / f"source-{item.position:02d}"
                download(item, source_path)
                try:
                    records.append(self._process_item(item, source_path, staged_directory))
                finally:
                    source_path.unlink(missing_ok=True)
            os.replace(staged_directory, final_directory)
            committed = True
            working_root.rmdir()
            return tuple(records)
        except BaseException:
            if not committed:
                shutil.rmtree(working_root, ignore_errors=True)
            raise

    @staticmethod
    def _process_item(item: SourceMedia, source_path: Path, staged_directory: Path) -> MediaRecord:
        stem = f"{item.position:02d}"
        if item.kind is MediaKind.IMAGE:
            return process_image(
                source_path,
                staged_directory / f"{stem}.webp",
                staged_directory / f"{stem}-thumb.webp",
            )
        if item.kind is MediaKind.VIDEO:
            return process_video(
                source_path,
                staged_directory / f"{stem}.mp4",
                staged_directory / f"{stem}-poster.webp",
            )
        raise ValidationError("source media kind is invalid")
