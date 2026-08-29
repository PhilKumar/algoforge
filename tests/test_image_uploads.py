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
    def test_an_unspoken_type_still_admits_nothing_else(self):
        for blob in (self._heic_bytes()[:11], b"<script>alert(1)</script>", ImageUploadTests._image_bytes("PNG")):
            with self.subTest(blob=blob[:8]):
                with self.assertRaises(ImageValidationError):
                    sanitize_image(blob, "application/octet-stream")

    def test_a_file_that_only_claims_to_be_heic_is_refused(self):
        with self.assertRaises(ImageValidationError):
            sanitize_image(ImageUploadTests._image_bytes("PNG"), "image/heic")


if __name__ == "__main__":
    unittest.main()
