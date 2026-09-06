"""Public values exposed by the Instagram boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class MediaKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


class VerificationStatus(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SavedCandidate:
    """Opaque saved-feed item that is safe to pass but not to print."""

    token: object = field(repr=False)


@dataclass(frozen=True, slots=True)
class SourceMedia:
    position: int
    kind: MediaKind
    url: str


@dataclass(frozen=True, slots=True)
class PublicPost:
    shortcode: str
    creator_username: str
    creator_id: int
    source_url: str
    caption: str
    published_at: datetime
    media: tuple[SourceMedia, ...]
