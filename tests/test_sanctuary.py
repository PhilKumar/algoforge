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

    def test_counter_column_is_never_read_as_money(self):
        """An HDFC key-fact sheet numbers its rows AND dates them."""
        kfs = (
            "Instalment No.,Due Date,Instalment Amt (Rs),Principal (Rs),Interest (Rs),Outstanding Principal (Rs)\n"
            "1,06/09/2025,42677,30881,11796,2382701\n"
            "2,06/10/2025,51698,31147,20551,2351554\n"
        ).encode()
        result = parse_emi_document("kfs.csv", kfs)
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][0]["amount"], 42677.0)  # not 1
        self.assertEqual(result["rows"][1]["amount"], 51698.0)  # not 2
        self.assertEqual(result["rows"][0]["outstanding"], 2382701.0)

    def test_a_stray_two_cell_row_cannot_pose_as_the_header(self):
        """ "Installment Frequency | Monthly" once captured the whole parse."""
        sheet = (
            "Installment Frequency,Monthly\n"
            "Sanction Date,19/08/2025\n"
            "Instalment No.,Due Date,Instalment Amt,Principal,Interest,Outstanding\n"
            "1,06/09/2025,42677,30881,11796,2382701\n"
            "2,06/10/2025,51698,31147,20551,2351554\n"
        ).encode()
        result = parse_emi_document("kfs.csv", sheet)
        self.assertEqual([row["amount"] for row in result["rows"]], [42677.0, 51698.0])

    def test_a_line_whose_date_follows_a_counter_still_reads(self):
        from sanctuary_emi import _parse_lines_fallback

        result = _parse_lines_fallback([["21 06/05/2027 51698 36668 15030 1705975"]], [])
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertEqual(row["due_date"], "2027-05-06")
        self.assertEqual(row["amount"], 51698.0)
        self.assertEqual(row["principal_part"], 36668.0)

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

    def test_a_revolving_loan_keeps_its_drawn_amount(self):
        async def run():
            loan_id = await self.sanctuary_db.create_loan(
                1, {"name": "ICICI OD", "emi_amount": 0, "drawn_amount": 318572.84}
            )
            await self.sanctuary_db.update_loan(1, loan_id, {"drawn_amount": 300000.0})
            loans = await self.sanctuary_db.list_loans(1)
            return next(loan for loan in loans if loan["id"] == loan_id)

        loan = asyncio.run(run())
        self.assertEqual(loan["emi_amount"], 0)
        self.assertEqual(loan["drawn_amount"], 300000.0)

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


class StatementParserTests(unittest.TestCase):
    """The statement importer: an ICICI-shaped table, the dedupe key, and the
    rule that deposits and self-transfers never count as spending."""

    CSV = (
        "DETAILED STATEMENT\n"
        "Account Number,000099887766 ( INR ) - USER\n"
        "S No.,Value Date,Transaction Date,Cheque Number,Transaction Remarks,"
        "Withdrawal Amount(INR),Deposit Amount(INR),Balance(INR)\n"
        "1,01/01/2021,01/01/2021,,MPS/APOLLO PHAR/2021,1001.00,0.00,69010.49\n"
        "2,01/01/2021,01/01/2021,,UPI/1001/salary in,0.00,50000.00,119010.49\n"
        "3,02/01/2021,02/01/2021,,UPI/1002/Sweep to OD ac,5000.00,0.00,114010.49\n"
        "4,02/01/2021,02/01/2021,,UPI/1003/You are paying tea,100.00,0.00,113910.49\n"
        "5,02/01/2021,02/01/2021,,UPI/1004/You are paying tea,100.00,0.00,113810.49\n"
    ).encode()

    def parse(self):
        from sanctuary_statements import parse_statement

        return parse_statement("2021.csv", self.CSV)

    def test_both_directions_post_and_carry_their_direction(self):
        result = self.parse()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["rows"]), 5)
        by_dir = {"in": 0, "out": 0}
        for row in result["rows"]:
            by_dir[row["dir"]] += 1
        self.assertEqual(by_dir, {"in": 1, "out": 4})
        self.assertEqual(result["deposits_count"], 1)
        self.assertAlmostEqual(result["deposits_total"], 50000.00)
        self.assertEqual(result["account_tail"], "887766")

    def test_same_day_same_amount_rows_get_distinct_refs(self):
        rows = self.parse()["rows"]
        refs = {r["ref_id"] for r in rows}
        self.assertEqual(len(refs), 5, "the serial+balance must split the twin tea payments")

    def test_reparsing_yields_identical_refs(self):
        first = {r["ref_id"] for r in self.parse()["rows"]}
        second = {r["ref_id"] for r in self.parse()["rows"]}
        self.assertEqual(first, second)

    def test_a_reverse_sweep_is_a_self_transfer_and_names_its_od_account(self):
        from sanctuary_statements import categorise, parse_statement

        self.assertEqual(categorise("000012345678: Rev Sweep From"), "Self transfer")
        blob = (
            "Account Number,000099887766 ( INR )\n"
            "S No.,Value Date,Transaction Date,Cheque Number,Transaction Remarks,"
            "Withdrawal Amount(INR),Deposit Amount(INR),Balance(INR)\n"
            "1,05/01/2021,05/01/2021,,000012345678: Rev Sweep From,0.00,5000.00,10000.00\n"
            "2,06/01/2021,06/01/2021,,Sweep to OD Ac,2500.00,0.00,7500.00\n"
        ).encode()
        result = parse_statement("x.csv", blob)
        self.assertEqual(
            result["linked_accounts"],
            [{"number": "000012345678", "kind": "Sweep-linked overdraft (OD)"}],
        )

    def test_an_inf_transfer_names_its_linked_account(self):
        from sanctuary_statements import parse_statement

        blob = (
            "Account Number,000099887766 ( INR )\n"
            "S No.,Value Date,Transaction Date,Cheque Number,Transaction Remarks,"
            "Withdrawal Amount(INR),Deposit Amount(INR),Balance(INR)\n"
            "1,05/01/2021,05/01/2021,,INF/308164322648/ 000055667788/A NAME,0.00,900.00,1000.00\n"
        ).encode()
        result = parse_statement("x.csv", blob)
        self.assertEqual(
            result["linked_accounts"],
            [{"number": "000055667788", "kind": "Linked account (internal transfer)"}],
        )

    def test_the_drcr_dialect_reads_direction_from_the_flag(self):
        from sanctuary_statements import parse_statement

        # Kotak's shape: one Amount column, direction in a Dr / Cr flag,
        # a second flag after Balance, and a timestamp on the date.
        blob = (
            '"","","Account Statement"\n'
            '"X","","","","Account No.","000012345678"\n'
            '"Sl. No.","Transaction Date","Value Date","Description","Chq /Ref No.",'
            '"Amount","Dr / Cr","Balance","Dr / Cr"\n'
            '"1","02-01-2023 02:58:07","02-01-2023","Ins Debit SPLN 1","CLIN-1","2,018.15","DR","0.00","CR"\n'
            '"2","02-01-2023 10:40:30","02-01-2023","UPI/incoming","UPI-2","14,000.00","CR","14,000.00","CR"\n'
        ).encode()
        result = parse_statement("kotak.csv", blob)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["account_tail"], "345678")
        dirs = {r["dir"]: r["amount"] for r in result["rows"]}
        self.assertEqual(dirs, {"out": 2018.15, "in": 14000.00})
        self.assertEqual(result["rows"][0]["entry_date"], "2023-01-02")

    def test_a_reexport_with_new_serials_keeps_the_same_refs(self):
        from sanctuary_statements import parse_statement

        # The same Dec-30 deposit exported twice: the year file numbers it
        # 462, a top-up file numbers it 1. One transaction, one ref.
        row = ",30/12/2020,30/12/2020,,UPI/1/refill,0.00,491.00,547.43\n"
        header = (
            "Account Number,000099887766 ( INR )\n"
            "S No.,Value Date,Transaction Date,Cheque Number,Transaction Remarks,"
            "Withdrawal Amount(INR),Deposit Amount(INR),Balance(INR)\n"
        )
        year_file = (header + "462" + row).encode()
        topup_file = (header + "1" + row).encode()
        ref_a = parse_statement("year.csv", year_file)["rows"][0]["ref_id"]
        ref_b = parse_statement("topup.csv", topup_file)["rows"][0]["ref_id"]
        self.assertEqual(ref_a, ref_b)

    def test_default_rules_reach_the_obvious_payees(self):
        from sanctuary_statements import categorise

        self.assertEqual(categorise("MPS/APOLLO PHAR/2021"), "Health")
        self.assertEqual(categorise("UPI/1002/Sweep to OD ac"), "Self transfer")
        self.assertEqual(categorise("UPI/419/payment on CRED/cred.club@axisb"), "Credit card bill")
        self.assertEqual(categorise("UPI/9/completely new shop"), "Uncategorised")

    def test_user_rule_wins_over_the_default(self):
        from sanctuary_statements import categorise

        rules = [{"match": "apollo", "category": "Pharmacy run"}]
        self.assertEqual(categorise("MPS/APOLLO PHAR/2021", rules), "Pharmacy run")

    def test_payee_key_prefers_the_upi_handle(self):
        from sanctuary_statements import payee_key

        # The bank half churns (okax, oki, okic are one person) — the key
        # is the name half, marked as a handle by its trailing @.
        self.assertEqual(payee_key("UPI/1001/Jan/emman.joy@okici/IDFC FIRST Bank/"), "emman.joy@")
        self.assertEqual(payee_key("UPI/9/x/someone@okax/A/"), payee_key("UPI/8/y/someone@oki/B/"))
        self.assertEqual(payee_key("BIL/ONL/000123456789/Indian Oil/998877/Cylinder"), "indian oil")

    def test_bulk_insert_skips_what_is_already_posted(self):
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        os.environ["PHILFORGE_DB"] = db_path
        self.addCleanup(os.unlink, db_path)
        import importlib

        import config
        import db as core_db

        importlib.reload(config)
        core_db.config = config
        core_db._initialized = False
        core_db._init_db_sync()
        import sanctuary_db

        sanctuary_db.config = config

        async def run():
            rows = [
                {"entry_date": "2021-01-01", "amount": 10.0, "note": "a", "ref_id": "stmt:x:1"},
                {"entry_date": "2021-01-02", "amount": 20.0, "note": "b", "ref_id": "stmt:x:2"},
            ]
            first = await sanctuary_db.add_ledger_many(1, rows)
            second = await sanctuary_db.add_ledger_many(
                1,
                rows
                + [
                    {"entry_date": "2021-01-03", "amount": 30.0, "note": "c", "ref_id": "stmt:x:3"},
                ],
            )
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(first, 2)
        self.assertEqual(second, 1, "the re-upload must add only the new row")


if __name__ == "__main__":
    unittest.main()
