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

# A camera roll holds far more than three formats. Copying a photo in Photos
# puts a TIFF on the clipboard, an iPhone writes HEIC, an HDR or dual-lens
# shot is an MPO, a newer library holds AVIF — and naming them one at a time
# only found the next one he happened to drag in. So anything Pillow can
# decode is a picture, and anything it cannot is not; the three a browser can
# already show are kept as they are, the rest become JPEG, or PNG where there
# is transparency to keep. Nothing is stored as it arrived either way: every
# picture is decoded and written out again, which is what strips whatever was
# riding along in it.
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
    """Validate by file signature, cap decoded size, and strip active/hidden metadata.

    The FILE says what it is; the browser's content type only gets to catch
    one lying about itself. Asking the content type first is what turned a
    picture copied out of Photos — which reaches the page as a TIFF — into
    "only PNG, JPEG, WebP and HEIC are allowed", for a perfectly good photo.
    """
    declared = str(declared_content_type or "").lower().strip()
    image = _open_image(data)
    source_format = str(image.format or "").upper()

    if source_format in _FORMAT_DETAILS:
        extension, detected_content_type = _FORMAT_DETAILS[source_format]
        if declared not in _UNSPOKEN_TYPES and declared != detected_content_type:
            image.close()
            raise ImageValidationError("The image contents do not match the declared file type.")
    else:
        # What it will be kept as is decided below, once its transparency
        # is known; a photograph has none and belongs in JPEG.
        extension, detected_content_type = _FORMAT_DETAILS["JPEG"]

    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        image.close()
        raise ImageValidationError("The image dimensions are too large.")

    try:
        image.seek(0)
        clean = ImageOps.exif_transpose(image)
        has_alpha = clean.mode in {"RGBA", "LA"} or (clean.mode == "P" and "transparency" in clean.info)
        saved_format = source_format
        if source_format not in _FORMAT_DETAILS:
            saved_format = "PNG" if has_alpha else "JPEG"
            extension, detected_content_type = _FORMAT_DETAILS[saved_format]
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


def sanitized_parts(data: bytes, declared_content_type: str) -> tuple[bytes, str, str]:
    """sanitize_image, in the shape a worker process can hand back.

    A twelve-megapixel photograph needs a hundred megabytes to decode, and
    Python hands that back to itself, not to the machine. Doing this in a
    child that exits keeps the server the size it was.
    """
    cleaned = sanitize_image(data, declared_content_type)
    return cleaned.data, cleaned.extension, cleaned.content_type
