"""Decode and rewrite user-uploaded chart images before they reach storage."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

# A picture out of an Apple photo library is HEIC, and Pillow cannot read
# that format on its own. The reader is an optional companion package:
# where it is absent HEIC is refused by name rather than crashing the
# upload, and every other format carries on as before.
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_READABLE = True
except Exception:  # pragma: no cover - only where the package is absent
    HEIC_READABLE = False

MAX_IMAGE_PIXELS = 25_000_000

_FORMAT_DETAILS = {
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "WEBP": (".webp", "image/webp"),
}

# What a browser calls an Apple picture. Some send nothing at all for a
# .heic, so the file's own first bytes have to be able to answer instead.
_HEIC_TYPES = {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"}
_UNSPOKEN_TYPES = {"", "application/octet-stream"}
_HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs", b"mif1", b"msf1"}


class ImageValidationError(ValueError):
    """The upload is not a safe supported raster image."""


@dataclass(frozen=True)
class SanitizedImage:
    data: bytes
    extension: str
    content_type: str
    width: int
    height: int


def looks_like_heic(data: bytes) -> bool:
    """An ISO container whose brand says HEIF — the box, not the name."""
    return len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12].lower() in _HEIF_BRANDS


def _open_image(data: bytes) -> Image.Image:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(BytesIO(data))
            image.verify()
        return Image.open(BytesIO(data))
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, UnidentifiedImageError, OSError) as exc:
        raise ImageValidationError("The upload is not a valid supported image.") from exc


def sanitize_image(data: bytes, declared_content_type: str) -> SanitizedImage:
    """Validate by file signature, cap decoded size, and strip active/hidden metadata."""
    declared = str(declared_content_type or "").lower().strip()
    allowed_declared = {details[1] for details in _FORMAT_DETAILS.values()}
    heic = declared in _HEIC_TYPES or (declared in _UNSPOKEN_TYPES and looks_like_heic(data))
    if heic:
        if not looks_like_heic(data):
            raise ImageValidationError("The image contents do not match the declared file type.")
        if not HEIC_READABLE:
            raise ImageValidationError("HEIC images cannot be read on this server.")
    elif declared not in allowed_declared:
        raise ImageValidationError("Only PNG, JPEG, WebP, and HEIC images are allowed.")

    image = _open_image(data)
    source_format = str(image.format or "").upper()
    if heic:
        if source_format != "HEIF":
            image.close()
            raise ImageValidationError("The image contents do not match the declared file type.")
        # A HEIC is kept as JPEG — the format every browser can actually show.
        extension, detected_content_type = _FORMAT_DETAILS["JPEG"]
    else:
        if source_format not in _FORMAT_DETAILS:
            image.close()
            raise ImageValidationError("Only PNG, JPEG, WebP, and HEIC images are allowed.")

        extension, detected_content_type = _FORMAT_DETAILS[source_format]
        if declared != detected_content_type:
            image.close()
            raise ImageValidationError("The image contents do not match the declared file type.")

    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        image.close()
        raise ImageValidationError("The image dimensions are too large.")

    saved_format = "JPEG" if heic else source_format
    try:
        image.seek(0)
        clean = ImageOps.exif_transpose(image)
        has_alpha = clean.mode in {"RGBA", "LA"} or (clean.mode == "P" and "transparency" in clean.info)
        clean = clean.convert("RGBA" if has_alpha and saved_format != "JPEG" else "RGB")
        output = BytesIO()
        if saved_format == "JPEG":
            clean.save(output, format="JPEG", quality=92, optimize=True)
        elif saved_format == "PNG":
            clean.save(output, format="PNG", optimize=True)
        else:
            clean.save(output, format="WEBP", quality=92, method=4)
    except OSError as exc:
        raise ImageValidationError("The image could not be safely decoded.") from exc
    finally:
        image.close()

    return SanitizedImage(
        data=output.getvalue(),
        extension=extension,
        content_type=detected_content_type,
        width=width,
        height=height,
    )
