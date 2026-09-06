"""Validate the static files emitted for public publication."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sync.archive.budget import BudgetStatus, check_budget
from sync.site_input.exporter import resolve_plain_path, validate_snapshot

_REQUIRED_FILES = ("index.html", "styles.css")
_REFERENCE_ATTRIBUTES = frozenset({"href", "poster", "src"})


@dataclass(frozen=True, slots=True)
class _Reference:
    attribute: str
    value: str


class _ArtifactParser(HTMLParser):
    """Collect artifact references while rejecting executable markup."""

    def __init__(self) -> None:
        super().__init__()
        self.references: list[_Reference] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            raise ValueError("scripts are not allowed")
        for name, value in attrs:
            if name.startswith("on"):
                raise ValueError("event attributes are not allowed")
            if name in _REFERENCE_ATTRIBUTES:
                if value is None:
                    raise ValueError("reference attributes must have a value")
                self.references.append(_Reference(attribute=name, value=value))


def validate_site_output(root: Path) -> BudgetStatus:
    """Return artifact budget status after enforcing static-publication invariants."""
    root = resolve_plain_path(root)
    if not root.is_dir():
        raise ValueError("site output root must be a directory")
    _reject_special_files(root)
    validate_snapshot(root / "archive")
    _require_regular_files(root)
    parser = _ArtifactParser()
    parser.feed((root / "index.html").read_text(encoding="utf-8"))
    parser.close()
    _validate_references(root, parser.references)
    return check_budget(root)


def _reject_special_files(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"symlink is not allowed: {path.relative_to(root).as_posix()}")
        if not path.is_file() and not path.is_dir():
            raise ValueError(
                f"site output contains a non-regular file: {path.relative_to(root).as_posix()}"
            )


def _require_regular_files(root: Path) -> None:
    for name in _REQUIRED_FILES:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"{name} is required and must be a regular file")


def _validate_references(root: Path, references: list[_Reference]) -> None:
    for reference in references:
        try:
            parsed = urlsplit(reference.value)
        except ValueError as error:
            raise ValueError("reference URL is invalid") from error
        if reference.value.startswith("#"):
            continue
        if parsed.scheme or parsed.netloc:
            _validate_external_reference(reference, parsed.scheme, parsed.netloc)
        else:
            _validate_local_reference(root, parsed.path)


def _validate_external_reference(reference: _Reference, scheme: str, netloc: str) -> None:
    if reference.attribute == "href" and scheme == "https" and netloc == "www.instagram.com":
        return
    raise ValueError("external references are not allowed")


def _validate_local_reference(root: Path, path: str) -> None:
    segments = _decode_local_path(path)
    if segments == ("styles.css",):
        target = root / "styles.css"
    elif len(segments) > 1 and segments[0] == "archive":
        target = root.joinpath(*segments)
    else:
        raise ValueError("local reference must target styles.css or an archive file")
    if target.is_symlink() or not target.is_file():
        raise ValueError("local reference must target an existing regular file")
    if not target.resolve(strict=True).is_relative_to(root):
        raise ValueError("local reference escapes the site output")


def _decode_local_path(path: str) -> tuple[str, ...]:
    if not path or path.startswith("/") or "\\" in path:
        raise ValueError("local reference path is unsafe")
    segments = tuple(unquote(segment) for segment in path.split("/"))
    if any(
        not segment
        or segment in {".", ".."}
        or "/" in segment
        or "\\" in segment
        or "\x00" in segment
        for segment in segments
    ):
        raise ValueError("local reference path is unsafe")
    return segments
