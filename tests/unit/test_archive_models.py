"""Archive schema conversion tests."""

import importlib
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest


@pytest.fixture
def valid_manifest_dict() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "posts": [
            {
                "shortcode": "IMAGE0001",
                "creator_username": "second_creator",
                "creator_id": 202,
                "source_url": "https://www.instagram.com/p/IMAGE0001/",
                "caption": "A single image",
                "published_at": "2025-01-02T03:04:05Z",
                "archived_at": "2025-01-03T03:04:05Z",
                "verified_at": "2025-01-04T03:04:05Z",
                "media_type": "image",
                "publication_state": "published",
                "unpublished_reason": None,
                "media": [
                    {
                        "position": 0,
                        "kind": "image",
                        "asset": {
                            "asset_path": "media/IMAGE0001/00.webp",
                            "mime_type": "image/webp",
                            "width": 1000,
                            "height": 750,
                            "byte_size": 2000,
                            "sha256": "a" * 64,
                        },
                        "preview": {
                            "asset_path": "media/IMAGE0001/00-thumb.webp",
                            "mime_type": "image/webp",
                            "width": 640,
                            "height": 480,
                            "byte_size": 1000,
                            "sha256": "b" * 64,
                        },
                    }
                ],
            },
            {
                "shortcode": "CAROUSEL1",
                "creator_username": "first_creator",
                "creator_id": 101,
                "source_url": "https://www.instagram.com/p/CAROUSEL1/",
                "caption": "Cafe\u0301\r\nMenu\u0301\rToday",
                "published_at": "2025-02-02T03:04:05Z",
                "archived_at": "2025-02-03T03:04:05Z",
                "verified_at": "2025-02-04T03:04:05Z",
                "media_type": "carousel",
                "publication_state": "published",
                "unpublished_reason": None,
                "media": [
                    {
                        "position": 0,
                        "kind": "image",
                        "asset": {
                            "asset_path": "media/CAROUSEL1/00.webp",
                            "mime_type": "image/webp",
                            "width": 1200,
                            "height": 900,
                            "byte_size": 3000,
                            "sha256": "c" * 64,
                        },
                        "preview": {
                            "asset_path": "media/CAROUSEL1/00-thumb.webp",
                            "mime_type": "image/webp",
                            "width": 640,
                            "height": 480,
                            "byte_size": 1200,
                            "sha256": "d" * 64,
                        },
                    },
                    {
                        "position": 1,
                        "kind": "video",
                        "asset": {
                            "asset_path": "media/CAROUSEL1/01.mp4",
                            "mime_type": "video/mp4",
                            "width": 720,
                            "height": 1280,
                            "byte_size": 4000,
                            "sha256": "e" * 64,
                        },
                        "preview": {
                            "asset_path": "media/CAROUSEL1/01-poster.webp",
                            "mime_type": "image/webp",
                            "width": 360,
                            "height": 640,
                            "byte_size": 1500,
                            "sha256": "f" * 64,
                        },
                        "duration_seconds": 12.5,
                    },
                ],
            },
        ],
    }


def test_load_manifest_preserves_order_and_renditions(valid_manifest_dict: dict[str, Any]) -> None:
    """Break caught: loader stops sorting posts or loses media preview records."""
    models = importlib.import_module("sync.archive.models")

    manifest = models.load_manifest(valid_manifest_dict)

    assert [post.shortcode for post in manifest.posts] == ["CAROUSEL1", "IMAGE0001"]
    assert manifest.posts[0].media[1].position == 1
    assert manifest.posts[0].media[1].preview.asset_path.endswith("01-poster.webp")
    assert manifest.posts[0].caption == "Caf\u00e9\nMen\u00fa\nToday"
    assert manifest.posts[0].published_at == datetime(2025, 2, 2, 3, 4, 5, tzinfo=UTC)


def test_manifest_round_trip_uses_canonical_utc_and_omits_image_duration(
    valid_manifest_dict: dict[str, Any],
) -> None:
    """Break caught: serializer changes typed data or emits unsupported image duration."""
    models = importlib.import_module("sync.archive.models")

    dumped = models.dump_manifest(models.load_manifest(valid_manifest_dict))

    first_post = dumped["posts"][0]
    assert isinstance(first_post, dict)
    assert first_post["published_at"] == "2025-02-02T03:04:05Z"
    first_media = first_post["media"]
    assert isinstance(first_media, list)
    assert "duration_seconds" not in first_media[0]
    assert first_media[1]["duration_seconds"] == 12.5


def test_validate_manifest_rejects_a_programmatically_added_non_normalized_caption(
    valid_manifest_dict: dict[str, Any],
) -> None:
    """Break caught: public validation permits a typed record with a non-NFC caption."""
    models = importlib.import_module("sync.archive.models")
    validation = importlib.import_module("sync.archive.validation")
    manifest = models.load_manifest(valid_manifest_dict)
    invalid_post = replace(manifest.posts[0], caption="Cafe\u0301")
    invalid_manifest = models.Manifest(
        schema_version=1,
        posts=(invalid_post, *manifest.posts[1:]),
    )

    with pytest.raises(ValueError):
        validation.validate_manifest(invalid_manifest)


def test_validate_manifest_rejects_an_oversized_programmatic_preview(
    valid_manifest_dict: dict[str, Any],
) -> None:
    """Break caught: direct validation permits an asset larger than 95 MiB."""
    models = importlib.import_module("sync.archive.models")
    validation = importlib.import_module("sync.archive.validation")
    manifest = models.load_manifest(valid_manifest_dict)
    oversized_preview = replace(manifest.posts[0].media[0].preview, byte_size=99_614_721)
    oversized_media = replace(manifest.posts[0].media[0], preview=oversized_preview)
    invalid_post = replace(manifest.posts[0], media=(oversized_media, *manifest.posts[0].media[1:]))
    invalid_manifest = replace(manifest, posts=(invalid_post, *manifest.posts[1:]))

    with pytest.raises(ValueError):
        validation.validate_manifest(invalid_manifest)


def test_validate_manifest_rejects_a_non_integer_schema_version(
    valid_manifest_dict: dict[str, Any],
) -> None:
    """Break caught: direct validation accepts a float schema version equal to one."""
    models = importlib.import_module("sync.archive.models")
    validation = importlib.import_module("sync.archive.validation")
    invalid_manifest = replace(models.load_manifest(valid_manifest_dict), schema_version=1.0)

    with pytest.raises(ValueError):
        validation.validate_manifest(invalid_manifest)


@pytest.mark.parametrize("malformation", ["member", "archived_at", "shortcode"])
def test_programmatic_manifest_validates_members_before_sorting(
    valid_manifest_dict: dict[str, Any], malformation: str
) -> None:
    """Break caught: ordering raises AttributeError/TypeError before member validation."""
    models = importlib.import_module("sync.archive.models")
    manifest = models.load_manifest(valid_manifest_dict)
    post = manifest.posts[0]
    invalid = (
        object()
        if malformation == "member"
        else replace(
            manifest.posts[1],
            **{"archived_at": post.archived_at, malformation: None},
        )
    )
    malformed = replace(manifest, posts=(post, invalid))
    reason = {
        "member": "ArchivePost",
        "archived_at": "post.archived_at",
        "shortcode": "post.shortcode",
    }[malformation]
    with pytest.raises(ValueError, match=reason):
        models.dump_manifest(malformed)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda data: data["posts"].append(deepcopy(data["posts"][0])), "duplicate shortcodes"),
        (
            lambda data: data["posts"][1]["media"].__setitem__(
                1, {**data["posts"][1]["media"][1], "position": 0}
            ),
            "duplicate media positions",
        ),
        (lambda data: data.__setitem__("schema_version", 2), "unknown future schema"),
        (lambda data: data.__setitem__("schema_version", 1.0), "non-integer schema version"),
        (
            lambda data: data["posts"][0].__setitem__("published_at", "2025-01-02T03:04:05-05:00"),
            "non-UTC timestamp",
        ),
        (
            lambda data: data["posts"][0]["media"][0]["asset"].__setitem__(
                "asset_path", "../escape.webp"
            ),
            "parent traversal",
        ),
        (
            lambda data: data["posts"][0]["media"][0]["asset"].__setitem__(
                "asset_path", "/absolute.webp"
            ),
            "absolute path",
        ),
        (
            lambda data: data["posts"][0]["media"][0]["asset"].__setitem__(
                "sha256", "not-a-digest"
            ),
            "invalid SHA-256",
        ),
        (
            lambda data: data["posts"][0]["media"][0]["asset"].__setitem__("byte_size", 99_614_721),
            "primary asset larger than 95 MiB",
        ),
        (
            lambda data: data["posts"][0]["media"][0]["preview"].__setitem__(
                "byte_size", 99_614_721
            ),
            "preview asset larger than 95 MiB",
        ),
        (
            lambda data: data["posts"][1]["media"][1].pop("duration_seconds"),
            "video without duration",
        ),
        (
            lambda data: data["posts"][0]["media"][0].__setitem__("duration_seconds", 3.0),
            "image with duration",
        ),
        (
            lambda data: data["posts"][0]["media"][0].__setitem__("duration_seconds", None),
            "image with a null duration field",
        ),
    ],
)
def test_load_manifest_rejects_invalid_archive_data(
    valid_manifest_dict: dict[str, Any],
    mutation: Callable[[dict[str, Any]], None],
    reason: str,
) -> None:
    """Break caught: loader accepts a specified invalid archive invariant."""
    models = importlib.import_module("sync.archive.models")
    invalid_manifest = deepcopy(valid_manifest_dict)
    mutation(invalid_manifest)

    with pytest.raises(ValueError):
        models.load_manifest(invalid_manifest)


def test_load_manifest_rejects_unknown_nested_keys(valid_manifest_dict: dict[str, Any]) -> None:
    """Break caught: loader silently discards an unrecognized nested schema key."""
    models = importlib.import_module("sync.archive.models")
    invalid_manifest = deepcopy(valid_manifest_dict)
    invalid_manifest["posts"][0]["unexpected"] = "discard me"

    with pytest.raises(ValueError):
        models.load_manifest(invalid_manifest)


@pytest.mark.parametrize("rendition", ["asset", "preview"])
@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        "instagram.session",
        "media/OTHER/00.webp",
        "./media/IMAGE0001/00.webp",
        "media/IMAGE0001/./00.webp",
        "media//IMAGE0001/00.webp",
        "media/IMAGE0001/00.webp/",
        "media/IMAGE0001/.git/config.webp",
        "media/IMAGE0001/secret\n.webp",
        "media/IMAGE0001/00.mp4",
        "media/IMAGE0001/00.WEBP",
    ],
)
def test_asset_namespace_and_extension_are_enforced_for_loaded_and_typed_records(
    valid_manifest_dict: dict[str, Any], path: str, rendition: str
) -> None:
    """Break caught: a referenced rendition can escape its canonical owner/media namespace."""
    models = importlib.import_module("sync.archive.models")
    valid = models.load_manifest(valid_manifest_dict)
    valid_manifest_dict["posts"][0]["media"][0][rendition]["asset_path"] = path
    with pytest.raises(ValueError):
        models.load_manifest(valid_manifest_dict)
    post = valid.posts[1]
    media = post.media[0]
    asset = replace(getattr(media, rendition), asset_path=path)
    invalid = replace(
        valid, posts=(valid.posts[0], replace(post, media=(replace(media, **{rendition: asset}),)))
    )
    with pytest.raises(ValueError):
        models.dump_manifest(invalid)


@pytest.mark.parametrize("kind", ["image", "video"])
def test_primary_rendition_mime_and_extension_must_match_media_kind(
    valid_manifest_dict: dict[str, Any], kind: str
) -> None:
    """Break caught: matching extension/MIME alone permits a video rendition on image media."""
    media = valid_manifest_dict["posts"][1]["media"][1 if kind == "video" else 0]
    media["asset"]["mime_type"] = "image/webp" if kind == "video" else "video/mp4"
    media["asset"]["asset_path"] = (
        "media/CAROUSEL1/wrong.webp" if kind == "video" else "media/CAROUSEL1/wrong.mp4"
    )
    with pytest.raises(ValueError):
        importlib.import_module("sync.archive.models").load_manifest(valid_manifest_dict)


@pytest.mark.parametrize("mime_type", [[], {}])
def test_typed_rendition_rejects_non_string_mime_before_extension_lookup(
    valid_manifest_dict: dict[str, Any], mime_type: object
) -> None:
    """Break caught: rendition lookup bypasses MIME validation with an unhashable value."""
    models = importlib.import_module("sync.archive.models")
    manifest = models.load_manifest(valid_manifest_dict)
    post = manifest.posts[1]
    media = post.media[0]
    invalid = replace(media, asset=replace(media.asset, mime_type=mime_type))
    with pytest.raises(ValueError, match="mime_type"):
        models.dump_manifest(
            replace(manifest, posts=(manifest.posts[0], replace(post, media=(invalid,))))
        )


def test_sync_state_round_trip_is_strict() -> None:
    """Break caught: sync-state conversion accepts unknown keys or changes stored values."""
    models = importlib.import_module("sync.archive.models")
    state_data: dict[str, object] = {
        "schema_version": 1,
        "backfill_complete": False,
        "last_successful_sync_at": "2025-04-05T06:07:08Z",
        "last_complete_saved_feed_scan_at": "2025-04-04T06:07:08Z",
        "reconciliation_cursor": 17,
        "consecutive_known_threshold": 20,
        "archive_byte_size": 123456,
    }

    state = models.load_sync_state(state_data)

    assert state.reconciliation_cursor == 17
    assert models.dump_sync_state(state)["last_successful_sync_at"] == "2025-04-05T06:07:08Z"
    state_data["unknown"] = True
    with pytest.raises(ValueError):
        models.load_sync_state(state_data)
