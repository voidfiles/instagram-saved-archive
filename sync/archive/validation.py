"""Validation for archive records that may have been built outside the loader."""

from __future__ import annotations

from datetime import UTC, datetime


def parse_utc(value: str) -> datetime:
    """Parse an ISO 8601 timestamp, accepting only a UTC offset."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("timestamp must be ISO 8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(None):
        raise ValueError("timestamp must use UTC")
    return parsed.astimezone(UTC)


def validate_manifest(manifest: object) -> None:
    """Raise ``ValueError`` when a typed manifest violates archive invariants."""
    from . import models

    if not isinstance(manifest, models.Manifest):
        raise ValueError("manifest must be a Manifest")
    if manifest.schema_version != models.SCHEMA_VERSION:
        raise ValueError(f"unsupported manifest schema version: {manifest.schema_version!r}")
    if not isinstance(manifest.posts, tuple):
        raise ValueError("manifest posts must be a tuple")

    expected_order = tuple(sorted(manifest.posts, key=lambda post: (-post.archived_at.timestamp(), post.shortcode)))
    if manifest.posts != expected_order:
        raise ValueError("manifest posts must use deterministic archive order")

    shortcodes: set[str] = set()
    asset_paths: set[str] = set()
    for post in manifest.posts:
        _validate_post(post, shortcodes, asset_paths)


def _validate_post(post: object, shortcodes: set[str], asset_paths: set[str]) -> None:
    from . import models

    if not isinstance(post, models.ArchivePost):
        raise ValueError("manifest posts must be ArchivePost records")
    shortcode = models._require_shortcode(post.shortcode, "post.shortcode")
    if shortcode in shortcodes:
        raise ValueError(f"duplicate shortcode: {shortcode}")
    shortcodes.add(shortcode)
    models._require_nonempty_string(post.creator_username, "post.creator_username")
    models._require_positive_int(post.creator_id, "post.creator_id")
    models._require_source_url(post.source_url, "post.source_url")
    if post.caption != models._normalize_caption(post.caption, "post.caption"):
        raise ValueError("post.caption must use normalized line endings and NFC Unicode")
    models._require_datetime_utc(post.published_at, "post.published_at")
    models._require_datetime_utc(post.archived_at, "post.archived_at")
    models._require_datetime_utc(post.verified_at, "post.verified_at")
    if not isinstance(post.media_type, models.MediaType):
        raise ValueError("post.media_type is invalid")
    if post.publication_state is not models.PublicationState.PUBLISHED or post.unpublished_reason is not None:
        raise ValueError("only published posts without an unpublished reason may be archived")
    if not isinstance(post.media, tuple) or not post.media:
        raise ValueError("post.media must be a non-empty tuple")
    if post.media_type is models.MediaType.CAROUSEL and len(post.media) < 2:
        raise ValueError("carousel posts require at least two media records")
    if post.media_type is not models.MediaType.CAROUSEL and len(post.media) != 1:
        raise ValueError("non-carousel posts require exactly one media record")

    positions: set[int] = set()
    for media in post.media:
        _validate_media(media, positions, asset_paths)
    if positions != set(range(len(post.media))):
        raise ValueError("media positions must be contiguous and zero-based")
    if post.media_type is models.MediaType.IMAGE and post.media[0].kind is not models.MediaKind.IMAGE:
        raise ValueError("image posts must contain image media")
    if post.media_type is models.MediaType.VIDEO and post.media[0].kind is not models.MediaKind.VIDEO:
        raise ValueError("video posts must contain video media")


def _validate_media(media: object, positions: set[int], asset_paths: set[str]) -> None:
    from . import models

    if not isinstance(media, models.MediaRecord):
        raise ValueError("post media must be MediaRecord records")
    position = models._require_nonnegative_int(media.position, "media.position")
    if position in positions:
        raise ValueError(f"duplicate media position: {position}")
    positions.add(position)
    if not isinstance(media.kind, models.MediaKind):
        raise ValueError("media.kind is invalid")
    _validate_asset(media.asset, asset_paths)
    _validate_asset(media.preview, asset_paths)
    if media.preview.mime_type != "image/webp":
        raise ValueError("media previews must be WebP assets")
    if media.kind is models.MediaKind.VIDEO:
        models._require_positive_float(media.duration_seconds, "media.duration_seconds")
    elif media.duration_seconds is not None:
        raise ValueError("image media cannot have a duration")


def _validate_asset(asset: object, asset_paths: set[str]) -> None:
    from . import models

    if not isinstance(asset, models.AssetRecord):
        raise ValueError("media assets must be AssetRecord records")
    path = models._require_asset_path(asset.asset_path, "asset.asset_path")
    if path in asset_paths:
        raise ValueError(f"duplicate media path: {path}")
    asset_paths.add(path)
    models._require_nonempty_string(asset.mime_type, "asset.mime_type")
    models._require_positive_int(asset.width, "asset.width")
    models._require_positive_int(asset.height, "asset.height")
    models._require_positive_int(asset.byte_size, "asset.byte_size")
    models._require_sha256(asset.sha256, "asset.sha256")
