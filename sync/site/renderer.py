"""Render a validated archive snapshot as a static, no-JavaScript site."""

from __future__ import annotations

import html
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from sync.archive.budget import BudgetStatus
from sync.archive.models import ArchivePost, Manifest, MediaKind, MediaRecord, SyncState
from sync.archive.store import SnapshotStore
from sync.site.styles import DEFAULT_CSS
from sync.site.validator import validate_site_output
from sync.site_input import export_site_input
from sync.site_input.exporter import resolve_plain_path


def build_site(archive: Path, destination: Path, *, title: str = "Saved") -> BudgetStatus:
    """Atomically replace ``destination`` with a validated static archive site."""
    destination = resolve_plain_path(destination, must_exist=False)
    if destination.exists() and not destination.is_dir():
        raise ValueError("destination must be a directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.build-", dir=destination.parent))
    backup: Path | None = None
    installed = False
    try:
        exported_archive = stage / "archive"
        export_site_input(archive, exported_archive)
        manifest, state = SnapshotStore(exported_archive).load()
        (stage / "styles.css").write_text(DEFAULT_CSS, encoding="utf-8")
        _write_document(stage / "index.html", _render_document(manifest, state, title))
        status = validate_site_output(stage)

        if destination.exists():
            backup = Path(
                tempfile.mkdtemp(prefix=f".{destination.name}.old-", dir=destination.parent)
            )
            backup.rmdir()
            destination.rename(backup)
        try:
            stage.rename(destination)
            installed = True
        except OSError as install_error:
            if backup is not None:
                try:
                    backup.rename(destination)
                except OSError:
                    try:
                        shutil.copytree(backup, destination)
                    except OSError as copy_error:
                        copy_error.add_note(f"Previous site remains at {backup}")
                        raise
                    install_error.add_note(
                        f"Rollback rename failed; previous site was copied from {backup}"
                    )
            raise
        return status
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if installed and backup is not None and backup.exists():
            shutil.rmtree(backup)


def _asset_url(asset_path: str) -> str:
    """Return an archive-relative URL with each canonical path segment quoted."""
    if "\\" in asset_path:
        raise ValueError("asset path must use POSIX separators")
    segments = asset_path.split("/")
    if (
        len(segments) < 3
        or segments[0] != "media"
        or any(
            not segment
            or segment in {".", ".."}
            or "\x00" in segment
            or any(ord(character) < 32 or ord(character) == 127 for character in segment)
            for segment in segments
        )
    ):
        raise ValueError("asset path must be canonical beneath media")
    return "archive/" + "/".join(quote(segment, safe="") for segment in segments)


def _render_media(post: ArchivePost) -> str:
    total = len(post.media)
    figures = []
    for media in post.media:
        label = f"Item {media.position + 1} of {total}"
        media_markup = _render_media_item(post, media, label)
        figure_label = f' aria-label="{_escape(label)}"' if total > 1 else ""
        figures.append(f"<figure{figure_label}>\n{media_markup}\n</figure>")
    return '<div class="media">\n' + "\n".join(figures) + "\n</div>"


def _render_media_item(post: ArchivePost, media: MediaRecord, label: str) -> str:
    width = _escape(str(media.asset.width))
    height = _escape(str(media.asset.height))
    description = _escape(f"{label}, saved from @{post.creator_username}")
    if media.kind is MediaKind.IMAGE:
        source = _escape(_asset_url(media.asset.asset_path))
        return (
            f'<img src="{source}" alt="{description}" width="{width}" height="{height}" '
            'loading="lazy" decoding="async">'
        )
    source = _escape(_asset_url(media.asset.asset_path))
    poster = _escape(_asset_url(media.preview.asset_path))
    mime_type = _escape(media.asset.mime_type)
    return (
        f'<video controls preload="metadata" poster="{poster}" width="{width}" '
        f'height="{height}" aria-label="{description}">\n'
        f'  <source src="{source}" type="{mime_type}">\n'
        "</video>"
    )


def _render_post(post: ArchivePost) -> str:
    creator = _escape(post.creator_username)
    creator_url = _escape(f"https://www.instagram.com/{quote(post.creator_username, safe='')}/")
    source_url = _escape(post.source_url)
    published_at = _escape(_iso_datetime(post.published_at))
    caption = _escape(post.caption)
    shortcode = _escape(post.shortcode)
    return f"""\
<article class="post" id="post-{shortcode}">
  <header class="post-header">
    <h2><a href="{creator_url}" rel="noopener noreferrer">@{creator}</a></h2>
    <a href="{source_url}" rel="noopener noreferrer">View original</a>
    <time datetime="{published_at}">{published_at}</time>
  </header>
  {_render_media(post)}
  <p class="caption">{caption}</p>
</article>"""


def _render_document(manifest: Manifest, state: SyncState, title: str) -> str:
    escaped_title = _escape(title)
    post_count = len(manifest.posts)
    count_text = _escape(f"{post_count} saved post{'s' if post_count != 1 else ''}")
    if state.last_successful_sync_at is None:
        status = f'<p class="archive-status">{count_text}. Not yet synced.</p>'
    else:
        synced_at = _escape(_iso_datetime(state.last_successful_sync_at))
        status = (
            f'<p class="archive-status">{count_text}. Last synced '
            f'<time datetime="{synced_at}">{synced_at}</time>.</p>'
        )
    posts = "\n".join(_render_post(post) for post in manifest.posts)
    return f"""\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <a class="skip-link" href="#posts">Skip to posts</a>
  <header class="page-header">
    <h1>{escaped_title}</h1>
    {status}
  </header>
  <main id="posts">
{posts}
  </main>
</body>
</html>
"""


def _write_document(path: Path, document: str) -> None:
    path.write_text(document, encoding="utf-8")


def _iso_datetime(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _escape(value: str) -> str:
    return html.escape(value, quote=True)


__all__ = ["build_site"]
