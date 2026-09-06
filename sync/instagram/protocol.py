"""Instagram interface consumed by synchronization orchestration."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from .models import PublicPost, SavedCandidate, SourceMedia, VerificationStatus


class InstagramClient(Protocol):
    def validate_identity(self, username: str) -> None: ...

    def iter_saved(self) -> Iterator[SavedCandidate]: ...

    def materialize(self, candidate: SavedCandidate) -> PublicPost | None: ...

    def download(self, source: SourceMedia, destination: Path) -> None: ...

    def verify(self, shortcode: str) -> VerificationStatus: ...
