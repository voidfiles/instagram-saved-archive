"""EXIF-aware, metadata-free WebP image processing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from sync.archive.models import MAX_GENERATED_FILE_BYTES, AssetRecord, MediaKind, MediaRecord
from sync.instagram.errors import SizeError, ValidationError

_SUPPORTED_SOURCE_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
_HASH_BLOCK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Dimensions:
    width: int
    height: int

    def as_tuple(self) -> tuple[int, int]:
        return self.width, self.height


def fit_image(width: int, height: int, longest_edge: int) -> Dimensions:
    """Fit positive image dimensions within one edge limit without upscaling."""
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in (width, height, longest_edge)
    ):
        raise ValidationError("image dimensions and limit must be positive integers")
    scale = min(1.0, longest_edge / max(width, height))
    return Dimensions(max(1, round(width * scale)), max(1, round(height * scale)))


def process_image(source: Path, primary: Path, preview: Path) -> MediaRecord:
    """Create and validate primary and thumbnail WebP renditions for one still image."""
    outputs = (primary, preview)
    _validate_output_ownership(source, outputs)
    created: set[Path] = set()
    try:
        with Image.open(source) as opened:
            opened.load()
            if (
                opened.format not in _SUPPORTED_SOURCE_FORMATS
                or getattr(opened, "n_frames", 1) != 1
            ):
                raise ValidationError("image source format is unsupported")
            oriented = ImageOps.exif_transpose(opened)
            converted = oriented.convert("RGB")
        try:
            primary_dimensions = fit_image(*converted.size, 1600)
            preview_dimensions = fit_image(*converted.size, 640)
            try:
                _save_webp(converted, primary, primary_dimensions)
            finally:
                _claim_created(primary, created)
            try:
                _save_webp(converted, preview, preview_dimensions)
            finally:
                _claim_created(preview, created)
        finally:
            converted.close()
        primary_asset = _asset_record(primary, "image/webp", primary_dimensions)
        preview_asset = _asset_record(preview, "image/webp", preview_dimensions)
        return MediaRecord(
            position=_position_from_primary(primary),
            kind=MediaKind.IMAGE,
            asset=primary_asset,
            preview=preview_asset,
        )
    except (ValidationError, SizeError):
        _remove_outputs(created)
        raise
    except (OSError, UnidentifiedImageError, ValueError):
        _remove_outputs(created)
        raise ValidationError("image processing failed") from None
    except BaseException:
        _remove_outputs(created)
        raise


def _save_webp(image: Image.Image, output: Path, dimensions: Dimensions) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    rendition = image
    if image.size != dimensions.as_tuple():
        rendition = image.resize(dimensions.as_tuple(), Image.Resampling.LANCZOS)
    try:
        rendition.save(
            output,
            format="WEBP",
            quality=82,
            method=6,
            exif=b"",
            icc_profile=b"",
            xmp=b"",
        )
    finally:
        if rendition is not image:
            rendition.close()
    _validate_webp(output, dimensions)


def _validate_webp(path: Path, dimensions: Dimensions) -> None:
    _validate_size(path)
    try:
        with Image.open(path) as decoded:
            decoded.load()
            if (
                decoded.format != "WEBP"
                or decoded.size != dimensions.as_tuple()
                or getattr(decoded, "n_frames", 1) != 1
            ):
                raise ValidationError("generated WebP failed validation")
            if decoded.getexif() or any(
                name in decoded.info for name in ("icc_profile", "exif", "xmp")
            ):
                raise ValidationError("generated WebP contains metadata")
    except (OSError, UnidentifiedImageError):
        raise ValidationError("generated WebP failed validation") from None


def _asset_record(path: Path, mime_type: str, dimensions: Dimensions) -> AssetRecord:
    _validate_size(path)
    return AssetRecord(
        asset_path=_manifest_asset_path(path),
        mime_type=mime_type,
        width=dimensions.width,
        height=dimensions.height,
        byte_size=path.stat().st_size,
        sha256=_sha256(path),
    )


def _validate_size(path: Path) -> None:
    try:
        byte_size = path.stat().st_size
    except OSError:
        raise ValidationError("generated media file is missing") from None
    if byte_size <= 0:
        raise ValidationError("generated media file is empty")
    if byte_size > MAX_GENERATED_FILE_BYTES:
        raise SizeError("generated media file must not exceed 95 MiB")


def _manifest_asset_path(path: Path) -> str:
    if not path.name or not path.parent.name or path.name in {".", ".."}:
        raise ValidationError("generated media path is invalid")
    return f"media/{path.parent.name}/{path.name}"


def _position_from_primary(primary: Path) -> int:
    try:
        position = int(primary.stem)
    except ValueError:
        raise ValidationError("primary media filename must be a numeric position") from None
    if position < 0:
        raise ValidationError("primary media position must not be negative")
    return position


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_HASH_BLOCK_SIZE):
            digest.update(block)
    return digest.hexdigest()


def _validate_output_ownership(source: Path, outputs: tuple[Path, ...]) -> None:
    resolved = (source.resolve(strict=False), *(path.resolve(strict=False) for path in outputs))
    if len(set(resolved)) != len(resolved):
        raise ValidationError("media input and output paths must be distinct")
    for path in outputs:
        if path.exists():
            raise FileExistsError(path)


def _claim_created(path: Path, created: set[Path]) -> None:
    if path.exists():
        created.add(path)


def _remove_outputs(paths: set[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
