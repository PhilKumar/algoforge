import unittest
from io import BytesIO

from PIL import Image

import image_uploads
from image_uploads import ImageValidationError, looks_like_heic, sanitize_image


class ImageUploadTests(unittest.TestCase):
    @staticmethod
    def _image_bytes(fmt: str = "PNG", size: tuple[int, int] = (8, 6)) -> bytes:
        output = BytesIO()
        Image.new("RGB", size, (12, 34, 56)).save(output, format=fmt)
        return output.getvalue()

    def test_rewrites_supported_image_from_signature(self):
        result = sanitize_image(self._image_bytes("PNG"), "image/png")
        self.assertEqual(result.extension, ".png")
        self.assertEqual(result.content_type, "image/png")
        self.assertEqual((result.width, result.height), (8, 6))
        self.assertTrue(result.data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_rejects_non_image_with_image_content_type(self):
        with self.assertRaises(ImageValidationError):
            sanitize_image(b"<script>alert(1)</script>", "image/png")

    def test_rejects_declared_type_mismatch(self):
        with self.assertRaises(ImageValidationError):
            sanitize_image(self._image_bytes("JPEG"), "image/png")

    def test_rejects_unsupported_declared_type(self):
        with self.assertRaises(ImageValidationError):
            sanitize_image(self._image_bytes("PNG"), "image/svg+xml")


class HeicTests(unittest.TestCase):
    """A picture out of an Apple photo library. It is let in and kept as
    JPEG — a stored .heic would open on his Mac and nowhere else."""

    @staticmethod
    def _heic_bytes(size: tuple[int, int] = (8, 6)) -> bytes:
        output = BytesIO()
        Image.new("RGB", size, (12, 34, 56)).save(output, format="HEIF")
        return output.getvalue()

    def test_the_brand_in_the_box_is_what_says_heic(self):
        self.assertTrue(looks_like_heic(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"))
        self.assertTrue(looks_like_heic(b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00"))
        self.assertFalse(looks_like_heic(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00"), "a video is not a picture")
        self.assertFalse(looks_like_heic(b"\x89PNG\r\n\x1a\n"))
        self.assertFalse(looks_like_heic(b"ftyp"), "too short to say anything")

    @unittest.skipUnless(image_uploads.HEIC_READABLE, "pillow-heif is not installed")
    def test_a_heic_is_kept_as_a_jpeg(self):
        result = sanitize_image(self._heic_bytes(), "image/heic")
        self.assertEqual(result.extension, ".jpg")
        self.assertEqual(result.content_type, "image/jpeg")
        self.assertEqual((result.width, result.height), (8, 6))
        self.assertTrue(result.data.startswith(b"\xff\xd8\xff"))

    @unittest.skipUnless(image_uploads.HEIC_READABLE, "pillow-heif is not installed")
    def test_a_browser_that_names_no_type_is_answered_by_the_bytes(self):
        """macOS hands some .heic files over with no content type at all."""
        for declared in ("", "application/octet-stream", "image/heif"):
            with self.subTest(declared=declared):
                self.assertEqual(sanitize_image(self._heic_bytes(), declared).content_type, "image/jpeg")

    @unittest.skipUnless(image_uploads.HEIC_READABLE, "pillow-heif is not installed")
    def test_an_unspoken_type_still_admits_nothing_that_is_not_a_picture(self):
        for blob in (self._heic_bytes()[:11], b"<script>alert(1)</script>", b"%PDF-1.7 not a picture"):
            with self.subTest(blob=blob[:8]):
                with self.assertRaises(ImageValidationError):
                    sanitize_image(blob, "application/octet-stream")

    def test_a_file_that_only_claims_to_be_heic_is_refused(self):
        with self.assertRaises(ImageValidationError):
            sanitize_image(ImageUploadTests._image_bytes("PNG"), "image/heic")


class ConvertingAwayFromTheServerTests(unittest.TestCase):
    """A journal import converts its photographs in a child process that is
    retired as it goes, because a twelve-megapixel picture costs a hundred
    megabytes to decode and Python gives that back to itself, not to the
    machine. The child hands back plain values, which is all that can cross
    between processes."""

    def test_the_parts_a_worker_can_hand_back(self):
        from image_uploads import sanitized_parts

        buf = BytesIO()
        Image.new("RGB", (8, 6), (12, 34, 56)).save(buf, format="TIFF")
        data, extension, content_type = sanitized_parts(buf.getvalue(), "image/tiff")
        self.assertEqual((extension, content_type), (".jpg", "image/jpeg"))
        self.assertTrue(data.startswith(b"\xff\xd8\xff"))
        self.assertTrue(all(isinstance(part, (bytes, str)) for part in (data, extension, content_type)))

    def test_it_refuses_what_sanitize_refuses(self):
        from image_uploads import sanitized_parts

        with self.assertRaises(ImageValidationError):
            sanitized_parts(b"%PDF-1.7", "image/jpeg")


class WhatAMacHandsOverTests(unittest.TestCase):
    """A picture leaving the Photos app is rarely a JPEG. Copying one puts a
    TIFF on the clipboard, dragging one can too, and the library itself holds
    HEIC. Refusing them by their content type turned real photographs into
    "only PNG, JPEG, WebP and HEIC images are allowed"."""

    @staticmethod
    def _bytes(fmt: str, mode: str = "RGB", size: tuple[int, int] = (8, 6)) -> bytes:
        output = BytesIO()
        Image.new(mode, size, (12, 34, 56) if mode == "RGB" else (12, 34, 56, 128)).save(output, format=fmt)
        return output.getvalue()

    def test_a_tiff_off_the_clipboard_is_kept_as_a_jpeg(self):
        result = sanitize_image(self._bytes("TIFF"), "image/tiff")
        self.assertEqual((result.extension, result.content_type), (".jpg", "image/jpeg"))
        self.assertTrue(result.data.startswith(b"\xff\xd8\xff"))

    def test_the_bytes_decide_when_the_browser_says_nothing(self):
        for fmt in ("TIFF", "GIF", "BMP"):
            with self.subTest(fmt=fmt):
                self.assertEqual(sanitize_image(self._bytes(fmt), "").content_type, "image/jpeg")

    def test_transparency_is_kept_by_saving_a_png_instead(self):
        clear = sanitize_image(self._bytes("TIFF", mode="RGBA"), "image/tiff")
        self.assertEqual((clear.extension, clear.content_type), (".png", "image/png"))

    def test_a_plain_png_is_still_a_png(self):
        """The three the browser can already show pass through untouched."""
        for fmt, ctype, ext in (("PNG", "image/png", ".png"), ("JPEG", "image/jpeg", ".jpg")):
            with self.subTest(fmt=fmt):
                result = sanitize_image(self._bytes(fmt), ctype)
                self.assertEqual((result.extension, result.content_type), (ext, ctype))

    def test_an_hdr_photo_off_a_phone_is_kept(self):
        """An iPhone HDR or dual-lens shot is an MPO — a JPEG carrying more
        than one frame. Named formats one at a time, it was the next thing
        refused; anything Pillow can decode is a picture now."""
        buf = BytesIO()
        Image.new("RGB", (8, 6), (12, 34, 56)).save(
            buf, format="MPO", append_images=[Image.new("RGB", (8, 6), (56, 34, 12))]
        )
        for declared in ("image/jpeg", "", "application/octet-stream"):
            with self.subTest(declared=declared):
                result = sanitize_image(buf.getvalue(), declared)
                self.assertEqual(result.content_type, "image/jpeg")
                self.assertTrue(result.data.startswith(b"\xff\xd8\xff"))

    def test_what_a_browser_can_show_already_is_left_in_its_own_format(self):
        result = sanitize_image(self._bytes("WEBP"), "image/webp")
        self.assertEqual((result.extension, result.content_type), (".webp", "image/webp"))

    def test_only_the_first_frame_of_a_multi_frame_picture_is_kept(self):
        """A drop is one picture, not an album: the frame he is looking at."""
        buf = BytesIO()
        Image.new("RGB", (8, 6), (1, 2, 3)).save(
            buf, format="GIF", save_all=True, append_images=[Image.new("RGB", (8, 6), (9, 9, 9))]
        )
        self.assertEqual(sanitize_image(buf.getvalue(), "").content_type, "image/jpeg")

    def test_a_document_or_a_movie_is_still_not_a_picture(self):
        """Widening what counts as a picture must not widen it to everything."""
        for blob, what in (
            (b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", "a PDF"),
            (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "an SVG"),
            (b"\x00\x00\x00\x20ftypqt  \x00\x00\x00\x00", "a Live Photo movie"),
        ):
            with self.subTest(what=what):
                with self.assertRaises(ImageValidationError):
                    sanitize_image(blob, "image/jpeg")

    def test_what_is_not_a_picture_is_still_refused_by_name(self):
        with self.assertRaises(ImageValidationError) as caught:
            sanitize_image(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", "image/tiff")
        self.assertIn("not a valid supported image", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
