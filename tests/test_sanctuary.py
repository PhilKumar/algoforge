"""Sanctuary: the EMI document parser and the ledger's monthly automation.

The parser tests mirror the three shapes banks actually produce: a dated
CSV with Indian formatting, an HDFC-style table whose date column is an
installment number, and a Kotak-style PDF whose rows collapse into single
text lines. The automation tests prove recurring items post exactly once
per elapsed month and that settling a backdated schedule clears its alerts.
"""

import asyncio
import io
import os
import tempfile
import unittest
import zipfile
from datetime import date

os.environ.setdefault("PHILFORGE_DB", os.path.join(tempfile.gettempdir(), "sanctuary-test.db"))

from sanctuary_emi import _add_months, parse_emi_document  # noqa: E402

CSV_BLOB = """HDFC Bank Personal Loan
Sr No,Due Date,EMI Amount,Principal Component,Interest Component,Outstanding Principal
1,05/09/2026,"12,345","8,100.50","4,244.50","4,91,899.50"
2,05-Oct-2026,12345,8182,4163,483717.5
bad row,,,,,
""".encode()

PERIOD_CSV = """EMI Schedule
Period,Outstanding,Principal (P),Interest (I),EMI Amount (P + I)
1,212000,0.00,2614.00,6228.50
2,212000.00,3047.50,3180.50,6228.50
3,208952.50,3093.21,3134.79,6228.50
""".encode()

LINE_ROWS = [
    ["Repayment Schedule for Period: 02 Jun 2022 to 02 May 2026"],
    ["Date Type Rate Total Amount Principal Interest Closing Balance"],
    ["02 Jun 2022 Installment 17.0 6,576 3,851.49 2,724.51 2,24,063.51"],
    ["02 Jul 2022 Installment 17.0 6,576 3,378.78 3,197.22 2,20,684.73"],
]


class TestEmiParser(unittest.TestCase):
    def test_dated_csv_with_indian_formats(self):
        result = parse_emi_document("sched.csv", CSV_BLOB)
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][0]["due_date"], "2026-09-05")
        self.assertEqual(result["rows"][0]["amount"], 12345.0)
        self.assertEqual(result["rows"][0]["outstanding"], 491899.5)
        self.assertEqual(result["rows"][1]["due_date"], "2026-10-05")

    def test_period_numbered_sheet_needs_anchor_then_anchors(self):
        blind = parse_emi_document("sched.csv", PERIOD_CSV)
        self.assertTrue(blind["needs_first_due"])
        self.assertEqual(blind["rows"], [])
        anchored = parse_emi_document("sched.csv", PERIOD_CSV, first_due=date(2025, 9, 5))
        self.assertEqual(len(anchored["rows"]), 3)
        self.assertEqual(anchored["rows"][0]["due_date"], "2025-09-05")
        self.assertEqual(anchored["rows"][2]["due_date"], "2025-11-05")
        self.assertEqual(anchored["rows"][2]["amount"], 6228.5)

    def test_single_cell_lines_self_validate(self):
        from sanctuary_emi import _parse_lines_fallback

        result = _parse_lines_fallback(LINE_ROWS, [])
        self.assertEqual(len(result["rows"]), 2)
        first = result["rows"][0]
        self.assertEqual(first["due_date"], "2022-06-02")
        self.assertEqual(first["amount"], 6576.0)
        self.assertEqual(first["principal_part"], 3851.49)
        self.assertEqual(first["outstanding"], 224063.51)

    def test_xlsx_shared_strings_and_serial_dates(self):
        shared = (
            '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main"><si><t>Due Date</t></si><si><t>EMI</t></si></sst>'
        )
        sheet = (
            '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2"><v>46300</v></c><c r="B2"><v>25000</v></c></row>'
            "</sheetData></worksheet>"
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("xl/sharedStrings.xml", shared)
            archive.writestr("xl/worksheets/sheet1.xml", sheet)
        result = parse_emi_document("loan.xlsx", buf.getvalue())
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["due_date"], "2026-10-05")

    def test_add_months_clamps_month_ends(self):
        self.assertEqual(_add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(_add_months(date(2024, 1, 31), 1), date(2024, 2, 29))
        self.assertEqual(_add_months(date(2026, 8, 5), -6), date(2026, 2, 5))


class TestLedgerAutomation(unittest.TestCase):
    """Recurring materialization and past-EMI settlement on a scratch DB."""

    def setUp(self):
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        os.environ["PHILFORGE_DB"] = self._db_path
        import importlib

        import config
        import db as core_db

        importlib.reload(config)
        core_db.config = config
        core_db._initialized = False
        core_db._init_db_sync()
        import sanctuary_db

        sanctuary_db.config = config
        self.sanctuary_db = sanctuary_db

    def tearDown(self):
        os.unlink(self._db_path)

    def test_recurring_posts_once_per_elapsed_month(self):
        import sanctuary

        async def run():
            await self.sanctuary_db.set_json_state(
                1,
                "recurring",
                [
                    {
                        "id": "r1",
                        "name": "NPS",
                        "amount": 5000,
                        "day": 1,
                        "category": "NPS",
                        "start_month": "2026-06",
                        "active": True,
                    }
                ],
            )
            await sanctuary._materialize_recurring(1)
            await sanctuary._materialize_recurring(1)  # idempotent
            months = await self.sanctuary_db.ledger_month_trend(1, ["2026-06", "2026-07", "2026-08"])
            return months

        months = asyncio.run(run())
        self.assertEqual(months.get("2026-06"), 5000.0)
        self.assertEqual(months.get("2026-07"), 5000.0)
        # current month present because today (day >= 1) has passed the post day
        self.assertEqual(months.get("2026-08"), 5000.0)

    def test_settle_past_marks_only_past_unpaid(self):
        async def run():
            loan_id = await self.sanctuary_db.create_loan(1, {"name": "Test Loan"})
            rows = [
                {"due_date": "2026-07-05", "amount": 1000},
                {"due_date": "2026-08-05", "amount": 1000},
                {"due_date": "2027-01-05", "amount": 1000},
            ]
            await self.sanctuary_db.replace_schedule(1, loan_id, rows)
            settled = await self.sanctuary_db.settle_past_emis(1, loan_id, "2026-08-25")
            unpaid = await self.sanctuary_db.unpaid_emis_through(1, "2027-12-31")
            return settled, unpaid

        settled, unpaid = asyncio.run(run())
        self.assertEqual(settled, 2)
        self.assertEqual(len(unpaid), 1)
        self.assertEqual(unpaid[0]["due_date"], "2027-01-05")

    def test_replace_schedule_preserves_paid_marks(self):
        async def run():
            loan_id = await self.sanctuary_db.create_loan(1, {"name": "Test Loan"})
            rows = [{"due_date": "2026-07-05", "amount": 1000}]
            await self.sanctuary_db.replace_schedule(1, loan_id, rows)
            emis = await self.sanctuary_db.emis_for_loan(1, loan_id)
            await self.sanctuary_db.set_emi_paid(1, emis[0]["id"], "2026-07-05")
            await self.sanctuary_db.replace_schedule(
                1, loan_id, [{"due_date": "2026-07-05", "amount": 1000}, {"due_date": "2026-08-05", "amount": 1000}]
            )
            return await self.sanctuary_db.emis_for_loan(1, loan_id)

        emis = asyncio.run(run())
        self.assertEqual(emis[0]["paid_on"], "2026-07-05")
        self.assertEqual(emis[1]["paid_on"], "")


if __name__ == "__main__":
    unittest.main()
