"""Explicit removal-list parsing and manifest filtering."""

from __future__ import annotations

import re
from pathlib import Path

from .models import Manifest
from .validation import validate_manifest

_SHORTCODE = re.compile(r"[A-Za-z0-9_-]+\Z")


def parse_removals(path: Path) -> frozenset[str]:
    """Return deduplicated shortcode removals, ignoring blank lines and comments."""
    removals: set[str] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if _SHORTCODE.fullmatch(line) is None:
            raise ValueError(f"invalid removal shortcode on line {line_number}")
        removals.add(line)
    return frozenset(removals)


def apply_removals(
    manifest: Manifest, removals: frozenset[str]
) -> tuple[Manifest, tuple[str, ...]]:
    """Drop explicitly removed posts and return their media-directory names in archive order."""
    validate_manifest(manifest)
    kept = tuple(post for post in manifest.posts if post.shortcode not in removals)
    deleted = tuple(post.shortcode for post in manifest.posts if post.shortcode in removals)
    return Manifest(schema_version=manifest.schema_version, posts=kept), deleted
