"""Reading the zip the iPhone's Journal app exports.

Every entry here is invented. The shape is Apple's — index.html beside an
Entries folder of one HTML file per entry and a Photos folder — and the
tests hold it loosely on purpose: Apple has changed this export before, so
what is pinned is that a day's writing survives however the date is said.
"""

from __future__ import annotations

import io
import os
import sys
import unittest
import zipfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sanctuary_apple_journal as aj  # noqa: E402

# The shape a real export has: no <time> anywhere, the day in a pageHeader,
# the pictures in an assetGrid pointing at ../Resources, and the writing
# itself OUTSIDE the container, in the spans Cocoa's HTML writer wrapped it
# in. Apple's own title and body divs are left empty.
REAL_SHAPE = """<!DOCTYPE html><html><body>
<p class="p1"><span class="s1"><div class="pageContainer">
  <div class="pageHeader">Saturday, 14 September 2024</div>
  <div class="assetGrid">
    <div class="gridItem assetType_photo" id="AAA"><img class="asset_image" src="../Resources/AAA.jpeg"/></div>
    <div class="gridItem assetType_music" id="BBB"><img class="mediaPlayIcon" src="../Resources/mediaPlayIcon.heic"/>
      <img class="asset_image" src="../Resources/BBB.heic"/></div>
    <div class="gridItem assetType_video" id="CCC"><video class="asset_video"><source src="../Resources/CCC.MOV"/></video></div>
    <div class="gridItem assetType_link" id="DDD"><a href="https://example.org/a-page">
      <img class="asset_image" src="../Resources/DDD.heic"/></a></div>
    <div class="gridItem assetType_stateOfMind" id="EEE">
      <div class="gridItemOverlayText">Content</div><div class="gridItemOverlayText">Family</div>
      <img class="asset_image" src="../Resources/EEE.heic"/></div>
  </div><div class="title"></div></div></span><span class="s2">Sedentary lifestyle </span>
<span class="s1"><div class="bodyText"></div></span></p>
<p class="p2"><span class="s3">What a hectic day. Fifteen hours of sitting.</span></p>
</body></html>"""

WITH_TIME = """<html><body>
<article><time datetime="2024-01-14T09:12:00Z">Sunday, January 14, 2024</time>
<h1>The long walk</h1>
<p>We went as far as the water and turned back.</p>
<p>The dog would not come out of it.</p>
<img src="../Photos/IMG_0001.jpg" alt="the water">
</article></body></html>"""

PRINTED_DATE_ONLY = """<html><body>
<div>Tuesday, March 5, 2024</div>
<h2>Rain all day</h2>
<p>Nothing happened and that was the whole of it.</p>
</body></html>"""

NAMED_DATE_ONLY = """<html><body><p>No date anywhere in the writing.</p></body></html>"""

NO_DATE_AT_ALL = """<html><body><p>Not a day I can name.</p></body></html>"""

MISSING_PICTURE = """<html><body><time datetime="2024-05-02">May 2, 2024</time>
<p>The picture did not come with it.</p><img src="../Photos/gone.jpg"></body></html>"""


def _jpeg(colour=(80, 120, 160)) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (12, 9), colour).save(out, format="JPEG")
    return out.getvalue()


def _export(pages: dict[str, str], photos: dict[str, bytes] | None = None, stamp=(2026, 8, 31, 10, 0, 0)) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("AppleJournalEntries/index.html", "<html><body>An index, not an entry.</body></html>")
        for name, html in pages.items():
            archive.writestr(zipfile.ZipInfo(f"AppleJournalEntries/Entries/{name}", stamp), html)
        for name, blob in (photos or {}).items():
            archive.writestr(f"AppleJournalEntries/Photos/{name}", blob)
    return buf.getvalue()


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.blob = _export({"Entry-A.html": WITH_TIME}, {"IMG_0001.jpg": _jpeg()})
        self.entry = aj.read_export(self.blob)[0]

    def test_the_day_the_entry_was_written(self):
        self.assertEqual(self.entry["entry_date"], "2024-01-14")

    def test_the_title_and_the_writing_under_it(self):
        self.assertEqual(self.entry["title"], "The long walk")
        self.assertIn("as far as the water", self.entry["body"])
        self.assertIn("would not come out of it", self.entry["body"])

    def test_the_day_and_the_title_are_not_said_twice_in_the_body(self):
        self.assertNotIn("January 14, 2024", self.entry["body"])
        self.assertFalse(self.entry["body"].startswith("The long walk"))

    def test_the_picture_comes_out_of_the_same_zip(self):
        [picture] = self.entry["photos"]
        self.assertEqual(picture["name"], "IMG_0001.jpg")
        self.assertEqual(picture["caption"], "the water")
        self.assertTrue(picture["data"].startswith(b"\xff\xd8\xff"))

    def test_the_index_is_not_an_entry(self):
        self.assertEqual(len(aj.read_export(self.blob)), 1)


class WhereTheDateComesFromTests(unittest.TestCase):
    """Four places, in order of how much they can be trusted."""

    def test_the_line_the_app_prints_when_there_is_no_time_tag(self):
        [entry] = aj.read_export(_export({"Entry-B.html": PRINTED_DATE_ONLY}))
        self.assertEqual(entry["entry_date"], "2024-03-05")
        self.assertEqual(entry["title"], "Rain all day")

    def test_the_name_of_the_file_when_the_writing_says_nothing(self):
        [entry] = aj.read_export(_export({"2023-11-09-Entry.html": NAMED_DATE_ONLY}))
        self.assertEqual(entry["entry_date"], "2023-11-09")

    def test_the_zip_s_own_clock_is_the_last_resort(self):
        [entry] = aj.read_export(_export({"Entry-C.html": NO_DATE_AT_ALL}, stamp=(2025, 6, 7, 8, 0, 0)))
        self.assertEqual(entry["entry_date"], "2025-06-07")

    def test_a_date_that_could_not_be_read_is_never_guessed_at(self):
        blob = _export({"Entry-D.html": NO_DATE_AT_ALL}, stamp=(1980, 1, 1, 0, 0, 0))
        entry = aj.read_export(blob)[0]
        self.assertIn(entry["entry_date"], ("1980-01-01", ""))


class ImportingTwiceTests(unittest.TestCase):
    """He will export again next month; the months already here must not
    arrive a second time."""

    def test_the_same_entry_fingerprints_the_same_way(self):
        first = aj.read_export(_export({"Entry-A.html": WITH_TIME}, {"IMG_0001.jpg": _jpeg()}))
        again = aj.read_export(_export({"Renamed-By-Apple.html": WITH_TIME}, {"IMG_0001.jpg": _jpeg()}))
        self.assertEqual(first[0]["fingerprint"], again[0]["fingerprint"], "the file's name is not the entry")

    def test_a_different_day_is_a_different_entry(self):
        one = aj.read_export(_export({"a.html": WITH_TIME}, {"IMG_0001.jpg": _jpeg()}))[0]
        two = aj.read_export(
            _export({"a.html": WITH_TIME.replace("2024-01-14", "2024-01-15")}, {"IMG_0001.jpg": _jpeg()})
        )[0]
        self.assertNotEqual(one["fingerprint"], two["fingerprint"])


class WhatCanGoWrongTests(unittest.TestCase):
    def test_a_picture_the_export_left_out_does_not_lose_the_entry(self):
        [entry] = aj.read_export(_export({"Entry-E.html": MISSING_PICTURE}))
        self.assertEqual(entry["entry_date"], "2024-05-02")
        self.assertEqual(entry["photos"], [])
        self.assertIn("did not come with it", entry["body"])

    def test_something_that_is_not_a_zip(self):
        with self.assertRaises(ValueError):
            aj.read_export(b"not a zip at all")

    def test_a_zip_with_no_entries_in_it(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("AppleJournalEntries/index.html", "<html></html>")
        with self.assertRaises(ValueError):
            aj.read_export(buf.getvalue())

    def test_an_empty_entry_is_not_carried_over(self):
        blank = "<html><body><time datetime='2024-02-02'>February 2, 2024</time></body></html>"
        self.assertEqual(aj.read_export(_export({"blank.html": blank})), [])

    def test_they_come_back_oldest_first(self):
        blob = _export({"z.html": WITH_TIME, "a.html": PRINTED_DATE_ONLY})
        self.assertEqual([e["entry_date"] for e in aj.read_export(blob)], ["2024-01-14", "2024-03-05"])


class TheShapeAppleActuallyWritesTests(unittest.TestCase):
    """Built from his own export. The synthetic entries above are the shape
    the documentation describes; this is the shape the phone produced, and
    the two are not the same — which is why the reader holds both."""

    def setUp(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("AppleJournalEntries/index.html", "<html>index</html>")
            archive.writestr("AppleJournalEntries/Entries/2024-09-14_Sedentary_lifestyle.html", REAL_SHAPE)
            # What a Mac puts in every zip it makes, shadowing every file.
            archive.writestr("__MACOSX/AppleJournalEntries/Entries/._2024-09-14_Sedentary_lifestyle.html", "\x00junk")
            for name in ("AAA.jpeg", "BBB.heic", "DDD.heic", "EEE.heic", "mediaPlayIcon.heic"):
                archive.writestr(f"AppleJournalEntries/Resources/{name}", _jpeg())
        self.entries = aj.read_export(buf.getvalue())

    def test_the_mac_s_shadow_files_are_not_a_second_journal(self):
        self.assertEqual(len(self.entries), 1, "__MACOSX would have doubled every entry")

    def test_the_day_is_read_from_the_header_apple_prints(self):
        self.assertEqual(self.entries[0]["entry_date"], "2024-09-14")

    def test_the_title_comes_from_the_name_apple_gave_the_file(self):
        self.assertEqual(self.entries[0]["title"], "Sedentary lifestyle")

    def test_the_writing_is_found_outside_the_container(self):
        self.assertIn("Fifteen hours of sitting", self.entries[0]["body"])
        self.assertNotIn("Saturday, 14 September", self.entries[0]["body"])

    def test_only_his_photographs_come_over(self):
        """Cover art, a link's preview, a mood's swatch and a play button are
        all .heic files in Resources beside the real pictures."""
        self.assertEqual([p["name"] for p in self.entries[0]["photos"]], ["AAA.jpeg"])

    def test_a_video_is_counted_rather_than_dropped_in_silence(self):
        self.assertEqual(self.entries[0]["videos"], 1)

    def test_what_was_attached_in_words_is_kept_as_words(self):
        body = self.entries[0]["body"]
        self.assertIn("Felt: Content · Family", body)
        self.assertIn("https://example.org/a-page", body)

    def test_an_untitled_day_is_not_given_its_first_sentence_as_a_title(self):
        untitled = REAL_SHAPE.replace('<span class="s2">Sedentary lifestyle </span>', "")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("AppleJournalEntries/Entries/2024-09-14.html", untitled)
        self.assertEqual(aj.read_export(buf.getvalue())[0]["title"], "")


class DateReadingTests(unittest.TestCase):
    def test_the_shapes_apple_prints(self):
        for said, expected in (
            ("Sunday, January 14, 2024", date(2024, 1, 14)),
            ("January 14, 2024", date(2024, 1, 14)),
            ("Wednesday, September 3 2025", date(2025, 9, 3)),
            # What his own phone prints — the day before the month.
            ("Saturday, 14 September 2024", date(2024, 9, 14)),
            ("Monday, 12 August 2024", date(2024, 8, 12)),
            ("14 September 2024", date(2024, 9, 14)),
        ):
            with self.subTest(said=said):
                self.assertEqual(aj._date_from_text(said), expected)

    def test_what_is_not_a_date(self):
        for said in ("Somewhere in the summer", "14/01/2024 was long", "Fourteenth of January"):
            with self.subTest(said=said):
                self.assertIsNone(aj._date_from_text(said))


if __name__ == "__main__":
    unittest.main()
