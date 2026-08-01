import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from data.option_archive import OptionDataArchive


class OptionDataArchiveTests(unittest.TestCase):
    def test_store_merges_exact_minutes_and_exports_deterministically(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = OptionDataArchive(tmp)
            identity = {
                "provider": "upstox",
                "underlying": "NIFTY",
                "expiry": date(2026, 7, 30),
                "strike": 24500,
                "option_type": "CE",
            }
            first = archive.store(
                **identity,
                bars={datetime(2026, 7, 22, 10, 0): {"open": 100, "high": 104, "low": 99, "close": 102}},
                instrument_key="NSE_FO|test",
            )
            second = archive.store(
                **identity,
                bars={
                    datetime(2026, 7, 22, 10, 0): {"open": 101, "high": 105, "low": 100, "close": 103},
                    datetime(2026, 7, 22, 10, 1): {"open": 103, "high": 106, "low": 102, "close": 105},
                },
                instrument_key="NSE_FO|test",
            )

            self.assertEqual(first["bar_count"], 1)
            self.assertEqual(second["bar_count"], 2)
            rows = archive.export_rows(**identity)
            self.assertEqual([row["open"] for row in rows], [101.0, 103.0])
            self.assertEqual(len(archive.inventory(provider="upstox", underlying="NIFTY")), 1)

            stored_path = next(Path(tmp).glob("*/*/*/*.json"))
            payload = json.loads(stored_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["checksum_sha256"], second["checksum_sha256"])

    def test_missing_contract_is_an_empty_series_not_a_zero_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = OptionDataArchive(tmp)
            rows = archive.load(
                provider="dhan",
                underlying="NIFTY",
                expiry=date(2026, 7, 30),
                strike=24500,
                option_type="PE",
            )
            self.assertEqual(rows, {})

    def test_checksum_mismatch_is_not_served_as_market_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = OptionDataArchive(tmp)
            identity = {
                "provider": "upstox",
                "underlying": "NIFTY",
                "expiry": date(2026, 7, 30),
                "strike": 24500,
                "option_type": "CE",
            }
            archive.store(
                **identity,
                bars={datetime(2026, 7, 22, 10, 0): {"open": 100, "high": 104, "low": 99, "close": 102}},
            )
            stored_path = next(Path(tmp).glob("*/*/*/*.json"))
            payload = json.loads(stored_path.read_text(encoding="utf-8"))
            payload["bars"][0]["open"] = 999
            stored_path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(archive.load(**identity), {})


if __name__ == "__main__":
    unittest.main()
