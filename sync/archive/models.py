"""Versioned, strictly parsed records for an archive snapshot."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import cast
from urllib.parse import urlparse

from .validation import parse_utc

SCHEMA_VERSION = 1
MAX_GENERATED_FILE_BYTES = 95 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class MediaKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


class MediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    CAROUSEL = "carousel"


class PublicationState(StrEnum):
    PUBLISHED = "published"


@dataclass(frozen=True, slots=True)
class AssetRecord:
    asset_path: str
    mime_type: str
    width: int
    height: int
    byte_size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class MediaRecord:
    position: int
    kind: MediaKind
    asset: AssetRecord
    preview: AssetRecord
    duration_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ArchivePost:
    shortcode: str
    creator_username: str
    creator_id: int
    source_url: str
    caption: str
    published_at: datetime
    archived_at: datetime
    verified_at: datetime
    media_type: MediaType
    publication_state: PublicationState
    unpublished_reason: None
    media: tuple[MediaRecord, ...]


@dataclass(frozen=True, slots=True)
class Manifest:
    schema_version: int
    posts: tuple[ArchivePost, ...]


@dataclass(frozen=True, slots=True)
class SyncState:
    schema_version: int
    backfill_complete: bool
    last_successful_sync_at: datetime | None
    last_complete_saved_feed_scan_at: datetime | None
    reconciliation_cursor: int
    consecutive_known_threshold: int
    archive_byte_size: int


def load_manifest(data: object) -> Manifest:
    """Convert a JSON-shaped manifest into validated, typed records."""
    root = _strict_object(data, {"schema_version", "posts"}, "manifest")
    _require_schema_version(root, "manifest")
    posts_data = _require_list(root["posts"], "manifest.posts")
    posts = tuple(_load_post(item, f"manifest.posts[{index}]") for index, item in enumerate(posts_data))
    manifest = Manifest(schema_version=SCHEMA_VERSION, posts=_sorted_posts(posts))

    from .validation import validate_manifest

    validate_manifest(manifest)
    return manifest


def dump_manifest(manifest: Manifest) -> dict[str, object]:
    """Return a canonical JSON-shaped representation of a manifest."""
    from .validation import validate_manifest

    validate_manifest(manifest)
    return {
        "schema_version": SCHEMA_VERSION,
        "posts": [_dump_post(post) for post in manifest.posts],
    }


def load_sync_state(data: object) -> SyncState:
    """Convert a JSON-shaped sync state into validated, typed records."""
    keys = {
        "schema_version",
        "backfill_complete",
        "last_successful_sync_at",
        "last_complete_saved_feed_scan_at",
        "reconciliation_cursor",
        "consecutive_known_threshold",
        "archive_byte_size",
    }
    root = _strict_object(data, keys, "sync state")
    _require_schema_version(root, "sync state")
    state = SyncState(
        schema_version=SCHEMA_VERSION,
        backfill_complete=_require_bool(root["backfill_complete"], "sync state.backfill_complete"),
        last_successful_sync_at=_load_optional_utc(
            root["last_successful_sync_at"], "sync state.last_successful_sync_at"
        ),
        last_complete_saved_feed_scan_at=_load_optional_utc(
            root["last_complete_saved_feed_scan_at"], "sync state.last_complete_saved_feed_scan_at"
        ),
        reconciliation_cursor=_require_nonnegative_int(
            root["reconciliation_cursor"], "sync state.reconciliation_cursor"
        ),
        consecutive_known_threshold=_require_positive_int(
            root["consecutive_known_threshold"], "sync state.consecutive_known_threshold"
        ),
        archive_byte_size=_require_nonnegative_int(root["archive_byte_size"], "sync state.archive_byte_size"),
    )
    return state


def dump_sync_state(state: SyncState) -> dict[str, object]:
    """Return a canonical JSON-shaped representation of sync state."""
    if state.schema_version != SCHEMA_VERSION:
        raise ValueError(f"unsupported sync state schema version: {state.schema_version!r}")
    _require_bool(state.backfill_complete, "sync state.backfill_complete")
    _validate_optional_utc(state.last_successful_sync_at, "sync state.last_successful_sync_at")
    _validate_optional_utc(
        state.last_complete_saved_feed_scan_at, "sync state.last_complete_saved_feed_scan_at"
    )
    _require_nonnegative_int(state.reconciliation_cursor, "sync state.reconciliation_cursor")
    _require_positive_int(state.consecutive_known_threshold, "sync state.consecutive_known_threshold")
    _require_nonnegative_int(state.archive_byte_size, "sync state.archive_byte_size")
    return {
        "schema_version": SCHEMA_VERSION,
        "backfill_complete": state.backfill_complete,
        "last_successful_sync_at": _dump_optional_utc(state.last_successful_sync_at),
        "last_complete_saved_feed_scan_at": _dump_optional_utc(state.last_complete_saved_feed_scan_at),
        "reconciliation_cursor": state.reconciliation_cursor,
        "consecutive_known_threshold": state.consecutive_known_threshold,
        "archive_byte_size": state.archive_byte_size,
    }


def _load_post(data: object, context: str) -> ArchivePost:
    keys = {
        "shortcode",
        "creator_username",
        "creator_id",
        "source_url",
        "caption",
        "published_at",
        "archived_at",
        "verified_at",
        "media_type",
        "publication_state",
        "unpublished_reason",
        "media",
    }
    value = _strict_object(data, keys, context)
    _load_unpublished_reason(value["unpublished_reason"], f"{context}.unpublished_reason")
    media_data = _require_list(value["media"], f"{context}.media")
    media = tuple(_load_media(item, f"{context}.media[{index}]") for index, item in enumerate(media_data))
    return ArchivePost(
        shortcode=_require_shortcode(value["shortcode"], f"{context}.shortcode"),
        creator_username=_require_nonempty_string(value["creator_username"], f"{context}.creator_username"),
        creator_id=_require_positive_int(value["creator_id"], f"{context}.creator_id"),
        source_url=_require_source_url(value["source_url"], f"{context}.source_url"),
        caption=_normalize_caption(value["caption"], f"{context}.caption"),
        published_at=_require_utc(value["published_at"], f"{context}.published_at"),
        archived_at=_require_utc(value["archived_at"], f"{context}.archived_at"),
        verified_at=_require_utc(value["verified_at"], f"{context}.verified_at"),
        media_type=_require_media_type(value["media_type"], f"{context}.media_type"),
        publication_state=_require_publication_state(
            value["publication_state"], f"{context}.publication_state"
        ),
        unpublished_reason=None,
        media=media,
    )


def _load_media(data: object, context: str) -> MediaRecord:
    value = _strict_object(
        data, {"position", "kind", "asset", "preview", "duration_seconds"}, context, optional={"duration_seconds"}
    )
    kind = _require_media_kind(value["kind"], f"{context}.kind")
    duration = value.get("duration_seconds")
    if kind is MediaKind.VIDEO:
        duration_value = _require_positive_float(duration, f"{context}.duration_seconds")
    elif "duration_seconds" not in value:
        duration_value = None
    else:
        raise ValueError(f"{context}.duration_seconds is only allowed for video media")
    return MediaRecord(
        position=_require_nonnegative_int(value["position"], f"{context}.position"),
        kind=kind,
        asset=_load_asset(value["asset"], f"{context}.asset"),
        preview=_load_asset(value["preview"], f"{context}.preview"),
        duration_seconds=duration_value,
    )


def _load_asset(data: object, context: str) -> AssetRecord:
    value = _strict_object(
        data, {"asset_path", "mime_type", "width", "height", "byte_size", "sha256"}, context
    )
    return AssetRecord(
        asset_path=_require_asset_path(value["asset_path"], f"{context}.asset_path"),
        mime_type=_require_nonempty_string(value["mime_type"], f"{context}.mime_type"),
        width=_require_positive_int(value["width"], f"{context}.width"),
        height=_require_positive_int(value["height"], f"{context}.height"),
        byte_size=_require_asset_byte_size(value["byte_size"], f"{context}.byte_size"),
        sha256=_require_sha256(value["sha256"], f"{context}.sha256"),
    )


def _dump_post(post: ArchivePost) -> dict[str, object]:
    return {
        "shortcode": post.shortcode,
        "creator_username": post.creator_username,
        "creator_id": post.creator_id,
        "source_url": post.source_url,
        "caption": post.caption,
        "published_at": _dump_utc(post.published_at),
        "archived_at": _dump_utc(post.archived_at),
        "verified_at": _dump_utc(post.verified_at),
        "media_type": post.media_type.value,
        "publication_state": post.publication_state.value,
        "unpublished_reason": None,
        "media": [_dump_media(item) for item in post.media],
    }


def _dump_media(media: MediaRecord) -> dict[str, object]:
    value: dict[str, object] = {
        "position": media.position,
        "kind": media.kind.value,
        "asset": _dump_asset(media.asset),
        "preview": _dump_asset(media.preview),
    }
    if media.duration_seconds is not None:
        value["duration_seconds"] = media.duration_seconds
    return value


def _dump_asset(asset: AssetRecord) -> dict[str, object]:
    return {
        "asset_path": asset.asset_path,
        "mime_type": asset.mime_type,
        "width": asset.width,
        "height": asset.height,
        "byte_size": asset.byte_size,
        "sha256": asset.sha256,
    }


def _sorted_posts(posts: tuple[ArchivePost, ...]) -> tuple[ArchivePost, ...]:
    return tuple(sorted(posts, key=lambda post: (-post.archived_at.timestamp(), post.shortcode)))


def _strict_object(
    data: object, keys: set[str], context: str, *, optional: set[str] | None = None
) -> dict[str, object]:
    if not isinstance(data, dict) or any(not isinstance(key, str) for key in data):
        raise ValueError(f"{context} must be an object")
    optional = optional or set()
    actual = set(data)
    unknown = actual - keys
    missing = keys - actual - optional
    if unknown:
        raise ValueError(f"{context} has unknown keys: {sorted(unknown)!r}")
    if missing:
        raise ValueError(f"{context} is missing keys: {sorted(missing)!r}")
    return cast(dict[str, object], data)


def _require_schema_version(data: dict[str, object], context: str) -> None:
    version = data["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != SCHEMA_VERSION:
        raise ValueError(f"unsupported {context} schema version: {version!r}")


def _require_list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a list")
    return value


def _require_nonempty_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _require_positive_int(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{context} must be a positive integer")
    return value


def _require_nonnegative_int(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{context} must be a non-negative integer")
    return value


def _require_asset_byte_size(value: object, context: str) -> int:
    byte_size = _require_positive_int(value, context)
    if byte_size > MAX_GENERATED_FILE_BYTES:
        raise ValueError(f"{context} must not exceed 95 MiB")
    return byte_size


def _require_bool(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{context} must be a boolean")
    return value


def _load_unpublished_reason(value: object, context: str) -> None:
    if value is not None:
        raise ValueError(f"{context} must be null")


def _require_positive_float(value: object, context: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{context} must be a positive finite number")
    return float(value)


def _require_utc(value: object, context: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{context} must be a UTC ISO 8601 string")
    try:
        return parse_utc(value)
    except ValueError as error:
        raise ValueError(f"{context} must be a UTC ISO 8601 string") from error


def _load_optional_utc(value: object, context: str) -> datetime | None:
    if value is None:
        return None
    return _require_utc(value, context)


def _validate_optional_utc(value: datetime | None, context: str) -> None:
    if value is not None:
        _require_datetime_utc(value, context)


def _require_datetime_utc(value: object, context: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != UTC.utcoffset(None):
        raise ValueError(f"{context} must be a UTC datetime")
    return value.astimezone(UTC)


def _dump_utc(value: datetime) -> str:
    utc_value = _require_datetime_utc(value, "timestamp")
    return utc_value.isoformat().replace("+00:00", "Z")


def _dump_optional_utc(value: datetime | None) -> str | None:
    return None if value is None else _dump_utc(value)


def _normalize_caption(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{context} must be a string")
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _require_shortcode(value: object, context: str) -> str:
    shortcode = _require_nonempty_string(value, context)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", shortcode):
        raise ValueError(f"{context} contains invalid characters")
    return shortcode


def _require_source_url(value: object, context: str) -> str:
    url = _require_nonempty_string(value, context)
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{context} must be an absolute HTTPS URL")
    return url


def _require_asset_path(value: object, context: str) -> str:
    path = _require_nonempty_string(value, context)
    if "\\" in path:
        raise ValueError(f"{context} must use POSIX separators")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ValueError(f"{context} must stay inside the snapshot root")
    return path


def _require_sha256(value: object, context: str) -> str:
    digest = _require_nonempty_string(value, context)
    if _SHA256.fullmatch(digest) is None:
        raise ValueError(f"{context} must be a lowercase SHA-256 digest")
    return digest


def _require_media_kind(value: object, context: str) -> MediaKind:
    if not isinstance(value, str):
        raise ValueError(f"{context} must be image or video")
    try:
        return MediaKind(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{context} must be image or video") from error


def _require_media_type(value: object, context: str) -> MediaType:
    if not isinstance(value, str):
        raise ValueError(f"{context} must be image, video, or carousel")
    try:
        return MediaType(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{context} must be image, video, or carousel") from error


def _require_publication_state(value: object, context: str) -> PublicationState:
    if not isinstance(value, str):
        raise ValueError(f"{context} must be published")
    try:
        return PublicationState(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{context} must be published") from error
