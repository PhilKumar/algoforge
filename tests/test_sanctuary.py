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


class LoanDiscoveryTests(unittest.TestCase):
    """EMI-shaped streams found in statement rows, keyed on the number that
    repeats — the loan account — not the per-debit reference."""

    def rows(self, notes_dates_amounts):
        return [{"note": n, "entry_date": d, "amount": a} for n, d, a in notes_dates_amounts]

    def test_a_stream_keyed_on_the_repeating_number_survives_reference_churn(self):
        from datetime import date

        from sanctuary_statements import discover_loans

        rows = self.rows(
            [
                (f"ACH/SOME FINANCE/REF{90000 + i}11/00998877665", f"2023-{m:02d}-05", 5000.0)
                for i, m in enumerate(range(1, 9))
            ]
        )
        found = discover_loans(rows, date(2024, 6, 1))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["count"], 8)
        self.assertTrue(found[0]["closed"])

    def test_two_narration_eras_of_one_loan_merge(self):
        from datetime import date

        from sanctuary_statements import discover_loans

        era1 = [("ACH/LENDER/112233445566", f"2023-{m:02d}-10", 9000.0) for m in range(1, 6)]
        era2 = [(f"ACH/LENDER/NEWFMT{770 + m}/112233445566", f"2023-{m:02d}-10", 9000.0) for m in range(6, 12)]
        found = discover_loans(self.rows(era1 + era2), date(2024, 6, 1))
        self.assertEqual(len(found), 1, "one loan, not two")
        self.assertEqual(found[0]["count"], 11)

    def test_unrepeating_references_fall_back_to_the_lender_name(self):
        from datetime import date

        from sanctuary_statements import discover_loans

        # Every debit carries a fresh reference; the lender's name, long and
        # short across formats, is the only constant. 68 such debits were
        # invisible until the name became the key.
        rows = self.rows(
            [
                (f"DECS DR/{6631286928 + i * 7}/TP LENDER CO", f"2017-{m:02d}-10", 20600.0)
                for i, m in enumerate(range(1, 7))
            ]
            + [
                (f"ACH/TP LENDER CO HOMES LTD/{1573707511 + i * 999}", f"2017-{m:02d}-10", 20600.0)
                for i, m in enumerate(range(7, 13))
            ]
        )
        found = discover_loans(rows, date(2018, 6, 1))
        self.assertEqual(len(found), 1, "two narration formats, one loan")
        self.assertEqual(found[0]["count"], 12)

    def test_parallel_loans_on_one_mandate_stay_two(self):
        from datetime import date

        from sanctuary_statements import discover_loans

        rows = self.rows(
            [("ACH/LENDER/556677889900", f"2024-{m:02d}-05", 20000.0) for m in range(1, 13)]
            + [("ACH/LENDER/556677889900", f"2024-{m:02d}-15", 5000.0) for m in range(5, 13)]
        )
        found = discover_loans(rows, date(2025, 1, 1))
        self.assertEqual(sorted((c["count"], c["emi"]) for c in found), [(8, 5000.0), (12, 20000.0)])

    def test_a_restructured_emi_is_one_loan_ending_on_its_last_amount(self):
        from datetime import date

        from sanctuary_statements import discover_loans

        rows = self.rows(
            [("ACH/LENDER/556677889900", f"2019-{m:02d}-05", 20000.0) for m in range(1, 13)]
            + [("ACH/LENDER/556677889900", f"2020-{m:02d}-05", 9000.0) for m in range(1, 13)]
        )
        found = discover_loans(rows, date(2021, 6, 1))
        self.assertEqual([(c["count"], c["emi"]) for c in found], [(24, 9000.0)])

    def test_a_recent_stream_reads_as_running(self):
        from datetime import date

        from sanctuary_statements import discover_loans

        rows = self.rows([("ACH/LENDER/445566778899", f"2024-{m:02d}-07", 12000.0) for m in range(1, 7)])
        found = discover_loans(rows, date(2024, 7, 1))
        self.assertFalse(found[0]["closed"])


class DocumentNamingTests(unittest.TestCase):
    """Reading a paper's own filename for its title, kind, series and date."""

    def read(self, name, folder=""):
        from sanctuary_docs import classify_document

        return classify_document(name, folder)

    def test_a_payslip_is_named_by_its_month(self):
        r = self.read("Payslips/July 2020.pdf", "Payslips")
        self.assertEqual((r["category"], r["series"], r["doc_date"]), ("Work", "Payslips", "2020-07-01"))

    def test_a_tight_month_year_still_reads(self):
        # "Apr2021shiftAll" carries no separators at all.
        self.assertEqual(self.read("Apr2021shiftAll.pdf", "Payslips")["doc_date"], "2021-04-01")

    def test_the_employer_folder_joins_the_title(self):
        r = self.read("Payslips/Kyndryl/Sep2021.pdf", "Payslips/Kyndryl")
        self.assertEqual(r["title"], "Kyndryl · Sep2021")
        self.assertEqual(r["series"], "Payslips")

    def test_shift_allowances_are_not_swallowed_by_their_parent(self):
        # They live INSIDE the payslips folder; the word in their own path
        # must not claim them for Payslips.
        r = self.read("Payslips/Shift Allowances/Aug2020.pdf", "Payslips/Shift Allowances")
        self.assertEqual(r["series"], "Shift allowances")

    def test_his_own_folder_names_file_their_papers(self):
        # The folders he keeps are the strongest signal in the whole folder.
        self.assertEqual(self.read("Evin1.pdf", "IT Submission")["series"], "Tax declarations")
        self.assertEqual(self.read("6thStd_1st_term_fees.pdf", "2022_IT_Proof_Submission")["series"], "School fees")
        self.assertEqual(self.read("e-Nomination.pdf", "EPF")["series"], "EPF")
        self.assertEqual(self.read("Invoice Dec.pdf", "Broadband")["series"], "Utilities")
        self.assertEqual(self.read("2016_ITRV.pdf")["series"], "Tax returns")

    def test_a_file_with_no_extension_is_read_by_its_first_bytes(self):
        import sanctuary

        self.assertEqual(sanctuary._sniff_content_type(b"%PDF-1.4 x", ""), "application/pdf")
        self.assertEqual(
            sanctuary._sniff_content_type(b"\x89PNG\r\n\x1a\nx", "application/octet-stream"),
            "image/png",
        )
        self.assertEqual(sanctuary._sniff_content_type("a note".encode(), ""), "text/plain")
        self.assertEqual(sanctuary._sniff_content_type(b"\x00\x01\x02\xfe", ""), "")

    def test_an_unreadable_name_waits_as_other(self):
        # A name that says nothing must not be guessed at — it waits for a
        # tap. ("Onward to paramakudi" is no longer such a name: the travel
        # rule reads it, which is the point of the rules.)
        r = self.read("Export.pdf")
        self.assertEqual((r["category"], r["series"]), ("Other", ""))


class CardLoanScheduleTests(unittest.TestCase):
    """A credit card's linked-loan table, read into a debt with real dates.

    The figures here are invented — the shape is what the bank prints.
    """

    TABLE = """Loan EMI Table
Principal Tenure Outstanding
Loan Number Loan Booked Date Loan Type Interest Rate (%)
Amount (Rs.) (months) Principal (Rs.)
0000000000999888777 14 Oct 2025 INSTALOAN 300000.00 11.88 3 250000.00
Principal Amount (Rs) Interest Amount (Rs) EMI Date
7370.00 9900.00 13 Nov 2025
7443.00 5867.00 13 Dec 2025
7500.00 5800.00 13 Jan 2026
This is a system generated document and does not require signature."""

    def read(self, text=None, filename="LINKED LOANS_1234_27-08-26_13.35.pdf"):
        import sanctuary_statements

        return sanctuary_statements.parse_card_loan_schedule(text if text is not None else self.TABLE, filename)

    def test_the_header_is_read_whole(self):
        r = self.read()
        self.assertEqual(r["loan_number"], "999888777")
        self.assertEqual(r["booked"], "2025-10-14")
        self.assertEqual((r["principal"], r["rate"], r["tenure"]), (300000.0, 11.88, 3))
        self.assertEqual(r["outstanding"], 250000.0)

    def test_each_instalment_is_principal_plus_interest(self):
        r = self.read()
        self.assertEqual(len(r["emis"]), 3)
        self.assertEqual(r["emis"][0]["amount"], 17270.0, "7370 principal + 9900 interest")
        self.assertEqual((r["first"], r["last"]), ("2025-11-13", "2026-01-13"))
        self.assertEqual(r["emi"], 17270.0)

    def test_every_row_knows_what_is_still_owed(self):
        """The bank prints no running balance, so it is computed — what was
        borrowed less every principal rupee paid through that row. The loan
        card reads its OUTSTANDING from exactly this column; without it the
        card showed a dash."""
        r = self.read()
        self.assertEqual(r["emis"][0]["outstanding"], 292630.0, "300000 - 7370 principal")
        self.assertEqual(r["emis"][1]["outstanding"], 285187.0, "then - 7443")
        self.assertEqual(r["emis"][2]["outstanding"], 277687.0, "then - 7500")

    def test_the_card_is_named_by_the_file_the_bank_hands_over(self):
        self.assertEqual(self.read()["card_tail"], "1234")
        self.assertEqual(self.read(filename="schedule.pdf")["card_tail"], "", "no four-digit token, no card claimed")

    def test_a_half_read_table_is_refused(self):
        """A schedule with a header but no instalments would put a wrong
        debt on the shelf — worse than leaving him to type it."""
        header_only = "\n".join(self.TABLE.splitlines()[:5])
        self.assertIsNone(self.read(header_only))
        self.assertIsNone(self.read("a letter about nothing at all"))
        self.assertIsNone(self.read(""))


class CarryForwardTests(unittest.TestCase):
    """His pay lands on the last days of a month, so what funds August is
    whatever July still held on the 31st."""

    def setUp(self):
        db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
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

    def test_unsorted_rows_carry_their_direction(self):
        """The review row says which way each payee's money went, so the
        query MUST return `source` — omitting it took the whole panel down
        with a 500 that the page then hid as 'nothing to sort'."""

        async def run():
            await self.sanctuary_db.add_ledger_many(
                1,
                [
                    {
                        "entry_date": "2026-08-01",
                        "category": "Uncategorised",
                        "amount": 100.0,
                        "note": "BRK/NA/1",
                        "source": "statement",
                        "ref_id": "stmt:111111:x1",
                    },
                    {
                        "entry_date": "2026-08-02",
                        "category": "Uncategorised",
                        "amount": 200.0,
                        "note": "MMT/Withdrawal/BROKER",
                        "source": "statement-in",
                        "ref_id": "stmt:111111:x2",
                    },
                ],
            )
            return await self.sanctuary_db.uncategorised_ledger(1)

        rows = asyncio.run(run())
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn("source", row, "the review row cannot tell in from out without it")
        self.assertEqual({r["source"] for r in rows}, {"statement", "statement-in"})

    def test_the_month_before_january_is_last_december(self):
        import sanctuary

        self.assertEqual(sanctuary._previous_month("2026-01"), "2025-12")
        self.assertEqual(sanctuary._previous_month("2026-08"), "2026-07")

    def test_what_arrives_at_month_end_is_the_next_months_envelope(self):
        """His pay lands on the 31st, so it is the NEXT month that spends it.
        The month it landed in must not count it as money to spend."""
        import sanctuary

        async def run():
            await self.sanctuary_db.add_ledger_many(
                1,
                [
                    {
                        "entry_date": "2026-07-31",
                        "category": "Salary",
                        "amount": 120000.0,
                        "note": "NEFT-KYNDRYL",
                        "source": "statement-in",
                        "ref_id": "stmt:111111:a",
                    },
                    {
                        "entry_date": "2026-08-05",
                        "category": "Groceries",
                        "amount": 30000.0,
                        "note": "g",
                        "source": "statement",
                        "ref_id": "stmt:111111:c",
                    },
                ],
            )
            july = await sanctuary._pay_that_arrived(1, "2026-07", {}, 0.0)
            august = await sanctuary._pay_that_arrived(1, "2026-08", {}, 0.0)
            return july, august

        july, august = asyncio.run(run())
        self.assertEqual(july, 120000.0, "July's pay is what August will live on")
        self.assertEqual(august, 0.0, "August's own pay has not landed yet")

    def test_a_month_he_has_priced_himself_is_believed(self):
        """A salary he set by hand outranks the statement, in the carry too."""
        import sanctuary

        async def run():
            return await sanctuary._pay_that_arrived(1, "2026-07", {"2026-07": {"salary": 90000.0}}, 0.0)

        self.assertEqual(asyncio.run(run()), 90000.0)

    def test_the_envelope_travels_whole_not_net_of_spending(self):
        """What August lives on is July's pay ENTIRE. Subtracting July's own
        spending would take the same rupees out twice — July was funded by
        June, not by the pay that arrived on the 31st."""
        import sanctuary

        async def run():
            await self.sanctuary_db.add_ledger_many(
                1,
                [
                    {
                        "entry_date": "2026-07-31",
                        "category": "Salary",
                        "amount": 120000.0,
                        "note": "NEFT-KYNDRYL",
                        "source": "statement-in",
                        "ref_id": "stmt:111111:s",
                    },
                    {
                        "entry_date": "2026-07-09",
                        "category": "Groceries",
                        "amount": 45000.0,
                        "note": "g",
                        "source": "statement",
                        "ref_id": "stmt:111111:g",
                    },
                ],
            )
            return await sanctuary._pay_that_arrived(1, "2026-07", {}, 0.0)

        self.assertEqual(asyncio.run(run()), 120000.0, "the whole pay travels, not 120000-45000")


class CounterpartyAccountTests(unittest.TestCase):
    """An RTGS narration names the other side in full; the IFSC that closes
    it says which bank. Account numbers here are invented."""

    def read(self, note):
        import sanctuary_statements

        return sanctuary_statements.counterparty_accounts(note)

    def test_the_account_before_an_ifsc_is_read_with_its_bank(self):
        found = self.read("RTGS-HDFCR52025110881191896-A NAME-12345678901234  -HDFC0000240")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["number"], "12345678901234")
        self.assertEqual(found[0]["bank"], "HDFC")

    def test_each_bank_is_named_by_its_ifsc(self):
        for ifsc, bank in (("KKBK0001234", "Kotak"), ("ICIC0000123", "ICICI"), ("UTIB0000456", "Axis")):
            found = self.read(f"NEFT-REF999-A NAME-987654321098-{ifsc}")
            self.assertEqual(found[0]["bank"], bank, ifsc)

    def test_a_reference_number_is_never_mistaken_for_an_account(self):
        """A transfer reference is a long digit run too — inventing an
        account he does not hold would be worse than missing one."""
        self.assertEqual(self.read("MMT/IMPS/609257357027/Withdrawal/RAISESECUR/Axis Bank"), [])
        self.assertEqual(self.read("BRK/Raise Securities/20260323024820"), [])
        self.assertEqual(self.read(""), [])


class KnownAccountsTests(unittest.TestCase):
    """The accounts the sanctuary can prove he banks through, read back
    from the reference each imported row carries."""

    def setUp(self):
        db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
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

    def test_accounts_are_read_from_the_import_reference(self):
        async def run():
            await self.sanctuary_db.add_ledger_many(
                1,
                [
                    {
                        "entry_date": "2026-03-01",
                        "category": "Uncategorised",
                        "amount": 10.0,
                        "note": "a",
                        "source": "statement",
                        "ref_id": "stmt:503204:aa",
                    },
                    {
                        "entry_date": "2026-03-05",
                        "category": "Uncategorised",
                        "amount": 20.0,
                        "note": "b",
                        "source": "statement",
                        "ref_id": "stmt:503204:bb",
                    },
                    {
                        "entry_date": "2025-01-05",
                        "category": "Salary",
                        "amount": 30.0,
                        "note": "c",
                        "source": "statement-in",
                        "ref_id": "stmt:258400:cc",
                    },
                    # typed by hand — it belongs to no account
                    {
                        "entry_date": "2026-02-02",
                        "category": "Milk",
                        "amount": 40.0,
                        "note": "d",
                        "source": "manual",
                        "ref_id": "",
                    },
                ],
            )
            return await self.sanctuary_db.statement_account_summary(1)

        rows = {r["tail"]: r for r in asyncio.run(run())}
        self.assertEqual(set(rows), {"503204", "258400"}, "a hand-typed row is not an account")
        self.assertEqual(rows["503204"]["entries"], 2)
        self.assertEqual((rows["503204"]["first"], rows["503204"]["last"]), ("2026-03-01", "2026-03-05"))


class PlannerReadingTests(unittest.TestCase):
    """One written line in, a task and a date out. Thursday 27 Aug 2026 is
    'today' throughout, so every expectation below is a real calendar date
    and not a relative guess."""

    TODAY = date(2026, 8, 27)

    def read(self, text):
        import sanctuary_plan

        return sanctuary_plan.read_plan(text, self.TODAY)

    def test_the_phrases_he_actually_writes(self):
        cases = [
            ("call the bank before next wednesday", "call the bank", "2026-09-02", "by"),
            ("school fees before 15th of October", "school fees", "2026-10-15", "by"),
            ("renew the policy after this month", "renew the policy", "2026-08-31", "after"),
            ("Oliver's results tomo", "Oliver's results", "2026-08-28", "on"),
            ("send the photos next week", "send the photos", "2026-08-31", "on"),
            ("review insurance next year", "review insurance", "2027-01-01", "on"),
            ("holiday planning in 3 weeks", "holiday planning", "2026-09-17", "on"),
            ("talk to Beulah day after tomorrow", "talk to Beulah", "2026-08-29", "on"),
            ("pay rent before the 5th", "pay rent", "2026-09-05", "by"),
        ]
        for text, title, due, kind in cases:
            with self.subTest(text=text):
                read = self.read(text)
                self.assertEqual((read["title"], read["due"], read["kind"]), (title, due, kind))

    def test_next_wednesday_is_next_weeks_wednesday(self):
        """Said on a Thursday, 'next wednesday' is six days away, not the
        one in this same week."""
        self.assertEqual(self.read("x next wednesday")["due"], "2026-09-02")
        self.assertEqual(self.read("x on wednesday")["due"], "2026-09-02")

    def test_the_longest_phrase_wins(self):
        """'15th of October' must not lose to the bare '15th' inside it."""
        self.assertEqual(self.read("fees before 15th of October")["due"], "2026-10-15")

    def test_a_verb_that_looks_like_a_month_is_left_alone(self):
        """'march the kids to school tomo' is due tomorrow, not in March."""
        read = self.read("march the kids to school tomo")
        self.assertEqual(read["due"], "2026-08-28")
        self.assertEqual(read["title"], "march the kids to school")

    def test_may_is_a_word_before_it_is_a_month(self):
        read = self.read("I may need to call the plumber")
        self.assertEqual(read["due"], "")
        self.assertEqual(read["title"], "I may need to call the plumber")

    def test_an_unreadable_line_keeps_all_of_itself(self):
        """A wrong date on a school fee is worse than no date, so a line
        with nothing to read stays whole and waits under someday."""
        read = self.read("think about the thing we discussed")
        self.assertEqual((read["title"], read["due"]), ("think about the thing we discussed", ""))

    def test_the_object_of_the_task_is_not_stripped(self):
        """Trimming the dangling preposition must not eat the word the task
        is about — 'clear this' and 'do it' are whole tasks."""
        self.assertEqual(self.read("clear this by 2nd")["title"], "clear this")
        self.assertEqual(self.read("do it before friday")["title"], "do it")

    def test_a_past_date_this_year_means_next_year(self):
        """In late August, '15 March' is next March."""
        self.assertEqual(self.read("audit on 15 March")["due"], "2027-03-15")

    def test_the_words_he_wrote_travel_with_the_date(self):
        read = self.read("call the bank before next wednesday")
        self.assertEqual(read["said"], "before next wednesday")

    def test_horizons_sort_the_week_the_way_it_feels(self):
        import sanctuary_plan as plan

        self.assertEqual(plan.horizon("2026-08-26", self.TODAY), "overdue")
        self.assertEqual(plan.horizon("2026-08-27", self.TODAY), "today")
        self.assertEqual(plan.horizon("2026-08-28", self.TODAY), "tomorrow")
        self.assertEqual(plan.horizon("2026-08-30", self.TODAY), "this week")
        self.assertEqual(plan.horizon("2026-09-02", self.TODAY), "next week")
        self.assertEqual(plan.horizon("2026-12-01", self.TODAY), "later")
        self.assertEqual(plan.horizon("", self.TODAY), "someday")


class IdentityReadingTests(unittest.TestCase):
    """Reading the registrations off a payslip. Every number below is made
    up — his own never appear in this repository."""

    def find(self, text):
        import sanctuary_identity

        return {item["kind"]: item["number"] for item in sanctuary_identity.find_identifiers(text)}

    def test_an_older_payslip_gives_up_everything(self):
        text = (
            "|Emp No : 08255T PERNR : 12345678 |Department : |\n"
            "|Pay period : 01.09.2020 - 30.09.2020 |PF No. : PY/KRP/12345/123456 |\n"
            "| |UAN No : 100200300400 | |\n"
        )
        self.assertEqual(
            self.find(text),
            {"uan": "100200300400", "pf": "PY/KRP/12345/123456", "pernr": "12345678"},
        )

    def test_a_masked_number_is_refused(self):
        """The newer payslips print 'UAN Number : ******1234'. Storing that
        would leave the panel looking answered when it is not."""
        text = "UAN Number : ******1234\nBank Account No : ******123456\nEmployee ID : 1234567\n"
        found = self.find(text)
        self.assertNotIn("uan", found, "a masked UAN is not a UAN")
        self.assertEqual(found.get("employee_id"), "1234567", "the unmasked ones still come through")

    def test_the_lenders_pan_is_not_his(self):
        """His Form 12BB names the housing company's PAN four lines below
        his own. Taking the wrong one would put a stranger's number on the
        card the family is told to trust."""
        text = (
            "1. Permanent Account Number of the employee: AAAAA1111A\n"
            "(iv) Permanent Account Number of the lender BBBBB2222B\n"
        )
        self.assertEqual(self.find(text).get("pan"), "AAAAA1111A")

    def test_a_page_of_only_someone_elses_pan_yields_nothing(self):
        text = "Permanent Account Number (PAN) of the Lender BBBBB2222B\nCanfin Homes Ltd., Pan :BBBBB2222B\n"
        self.assertNotIn("pan", self.find(text))

    def test_the_papers_vote_and_the_scan_stops_when_they_agree(self):
        import sanctuary_identity

        tally: dict = {}
        for month in ("Sep", "Oct", "Nov"):
            sanctuary_identity.merge_findings(
                tally,
                [
                    {"kind": "uan", "label": "UAN", "number": "100200300400"},
                    {"kind": "pan", "label": "PAN", "number": "AAAAA1111A"},
                    {"kind": "pf", "label": "Provident fund", "number": "PY/KRP/12345/123456"},
                ],
                f"{month} 2020",
            )
        # one stray payslip belonging to somebody else
        sanctuary_identity.merge_findings(tally, [{"kind": "uan", "label": "UAN", "number": "999888777666"}], "a stray")
        best = {item["kind"]: (item["number"], item["papers"]) for item in sanctuary_identity.best_of(tally)}
        self.assertEqual(best["uan"], ("100200300400", 3), "the number the most papers agree on wins")
        self.assertEqual(best["pan"][1], 3)
        self.assertTrue(sanctuary_identity.is_complete(tally), "three papers each is enough to stop reading")

    def test_one_paper_alone_is_not_enough_to_stop(self):
        import sanctuary_identity

        tally: dict = {}
        sanctuary_identity.merge_findings(
            tally, [{"kind": "uan", "label": "UAN", "number": "100200300400"}], "one payslip"
        )
        self.assertFalse(sanctuary_identity.is_complete(tally))


class ImportantInfoTests(unittest.TestCase):
    """What Important info is allowed to say — the numbers a family would
    have to telephone, and never a number that is not one."""

    def test_running_loans_carry_their_account_number(self):
        import sanctuary

        lines = sanctuary._loan_account_lines(
            [
                {"id": 2, "lender": "Zebra Finance", "account_no": "111111", "active": 1},
                {"id": 1, "lender": "Alpha Bank", "account_no": "222222", "active": 1},
                # settled, and one still running but with no number known
                {"id": 3, "lender": "Closed Lender", "account_no": "333333", "active": 0},
                {"id": 4, "lender": "Nameless", "account_no": "", "active": 1},
            ]
        )
        self.assertEqual(
            [(x["lender"], x["number"]) for x in lines],
            [("Alpha Bank", "222222"), ("Zebra Finance", "111111")],
            "only running loans with a number, sorted by lender",
        )
        self.assertEqual(lines[0]["id"], "loan-1", "the row id keys the Show button")

    def test_a_number_that_will_not_decrypt_is_not_shown(self):
        """decrypt_value hands the ciphertext back when the key cannot open
        it, which used to reach the page as an account ending 'xKVg=='."""
        import sanctuary

        view = sanctuary._account_view({"kind": "bank", "bank": "HDFC", "number": "gAAAAABqkAkQnotakey"})
        self.assertEqual(view["number"], "")
        self.assertEqual(view["tail"], "")


class SalaryFromBankTests(unittest.TestCase):
    """A month with no payslip still knows its pay: the employer's credit
    in the bank statement is the salary."""

    def salary(self, rows):
        import sanctuary

        return sanctuary._salary_from_ledger(rows)

    def test_employer_credit_is_the_salary(self):
        """His March pay arrives named by the employer, not by "salary"."""
        rows = [
            {
                "amount": 118505.19,
                "source": "statement-in",
                "category": "Uncategorised",
                "note": "NEFT-DEUTH0260-KYNDRYL SOLUTIONS-0504790",
            },
        ]
        self.assertEqual(self.salary(rows), 118505.19)

    def test_pay_split_across_two_credits_is_summed(self):
        rows = [
            {"amount": 100000.0, "source": "statement-in", "category": "Salary", "note": "NEFT-KYNDRYL"},
            {"amount": 18505.19, "source": "statement-in", "category": "Salary", "note": "NEFT-KYNDRYL ARREARS"},
        ]
        self.assertEqual(self.salary(rows), 118505.19)

    def test_sweeps_and_outgoings_are_not_pay(self):
        """The month's big movements are his own money and his own bills —
        only an employer credit counts, and only when it comes IN."""
        rows = [
            {"amount": 103540.94, "source": "statement-in", "category": "Self transfer", "note": "Rev Sweep From"},
            {"amount": 100000.0, "source": "statement-in", "category": "Uncategorised", "note": "BRK/Raise Securities"},
            {"amount": 51698.0, "source": "statement", "category": "HDFC loan", "note": "ACH/HDFC BANK"},
            {"amount": 5000.0, "source": "statement", "category": "Uncategorised", "note": "PAID TO KYNDRYL CANTEEN"},
        ]
        self.assertEqual(self.salary(rows), 0, "a sweep, a broker and a debit are not pay")

    def test_employer_narration_is_categorised_as_salary(self):
        import sanctuary_statements

        self.assertEqual(
            sanctuary_statements.categorise("NEFT-DEUTH0260-KYNDRYL SOLUTIONS-0504790"),
            "Salary",
        )

    def test_his_own_idioms_are_default_rules(self):
        """The brokers, the card bill by NEFT, his wallet and his own Axis
        account — the shapes his statements actually print, so the resort
        button can file them without a taught rule."""
        import sanctuary_statements as st

        for note, want in (
            ("BRK/Raise Securities/20260323024820", "Investments"),
            ("MMT/IMPS/609257357027/Withdrawal/RAISESECUR/Axis Bank", "Investments"),
            ("MMT/IMPS/505410637502/Withdrawal/MONEYLICIO/Yes Bank Ltd", "Investments"),
            ("CMS/ CMS3125142229/ANGEL ONE LIMITED CLIENT", "Investments"),
            ("BIL/NEFT/001589486941/NEFTCC-/PHILIPR/CITI0000003", "Credit card bill"),
            ("MMT/IMPS/818019811931/PayZapp - Phili/PAYZAPP WA/HDFC BANK LTD", "Self transfer"),
            ("MMT/IMPS/523716694159/PHILIPRANJ/UTIB0005390", "Self transfer"),
            ("RTGS-HDFCR52023021482685717-HDFC BANK LTD RA OPS-14", "HDFC loan"),
            # the word he typed at the counter is the truest thing in the row
            ("UPI/MADHEENA CHICKE/551722241235/chicken", "Groceries"),
            ("UPI/SUSHAANTH HOMOE/551005321873/medicine", "Health"),
            ("UPI/CHINNADURAI MUR/551031307144/snacks", "Eating out"),
            ("Int.Pd:3712258400:01-01-2021 to 31-03-2021", "Interest"),
            ("ACH/KISETSUSAISONFINANCE/ICIC70221062430/KISETSUSAI", "Kisetsu loan"),
            ("DECS DR/2630439134/TP CAN FIN", "Home loan"),
            ("BIL/ONL/001675706445/Alpha Educ/760315248/Oliver 1st term", "School fees"),
            ("UPI/phil.shiny@/912713537635/To self", "Self transfer"),
        ):
            self.assertEqual(st.categorise(note), want, note)

    def test_the_traps_that_look_like_rules_but_are_not(self):
        """Patterns I measured and refused: 'lab' catches PineLabs POS
        terminals and Divi's Laboratories, and one handle called
        'paytmqr' stands behind 445 rows of every kind of shop."""
        import sanctuary_statements as st

        # neither may be dragged into Health by a three-letter "lab"
        self.assertNotEqual(st.categorise("UPI/349645/UPI/shoes.425/HDFC BANK LTD/PINELABPOSDQR42"), "Health")
        self.assertNotEqual(st.categorise("NACH-10-CR-DIVISLABORATORIES-000000002702843"), "Health")
        self.assertEqual(st.categorise("UPI/032011257798/UPI/paytmqr28100505/Paytm Payments/"), "Uncategorised")
        # His second handle's remarks say Coin — money going INTO an
        # investment, not a transfer to himself. It must not be swept up
        # by the rule that claims "phil.shiny@".
        self.assertEqual(st.categorise("UPI/300403051101/Coin/phil.shiny-1@ok/Kotak"), "Uncategorised")


class PayslipReadingTests(unittest.TestCase):
    """A payslip's own text, read for the month it belongs to and what
    actually reached the bank."""

    HEAD = "Kyndryl Solutions Pvt. Ltd.\nPAYSLIP FOR THE MONTH OF September 2022\n"

    def read(self, text):
        from sanctuary_docs import read_payslip

        return read_payslip(text)

    def test_a_payslip_gives_its_month_and_take_home(self):
        r = self.read(
            self.HEAD + "|30.09.2022 ICICI BANK 0350 95,170.45 = 109,659.45 - 14,489.00 + 0.00 |\n"
            "|Take Home Pay 95,170.45 |"
        )
        self.assertEqual(r["month"], "2022-09")
        self.assertEqual(r["net"], 95170.45)
        self.assertEqual(r["paid_on"], "2022-09-30")
        self.assertEqual(r["employer"], "Kyndryl Solutions Pvt. Ltd.")

    def test_the_european_format_of_the_older_slips_is_not_read_as_rupees_68(self):
        # IBM's older payslips print "68.026,49" — sixty-eight THOUSAND. Read
        # naively that is Rs 68, a thousandfold lie in a salary field.
        r = self.read(
            "IBM India Pvt. Ltd.\nPAYSLIP FOR THE MONTH OF April 2016\n"
            "|30.04.2016 ICICI BANK 0350 68.026,49 = 75.739,49 - 7.713,00 + 0,00 |\n"
            "|Take Home Pay 68.026,49 |"
        )
        self.assertEqual(r["net"], 68026.49)
        self.assertEqual(r["paid_on"], "2016-04-30")

    def test_the_transfer_date_is_not_the_pay_period_start(self):
        r = self.read(
            self.HEAD + "|Pay period : 01.09.2022 - 30.09.2022 |\n"
            "|30.09.2022 ICICI BANK 0350 95,170.45 = 1.00 - 0.00 + 0.00 |\n"
            "|Take Home Pay 95,170.45 |"
        )
        self.assertEqual(r["paid_on"], "2022-09-30")

    def test_half_a_payslip_reads_as_none(self):
        self.assertIsNone(self.read(self.HEAD + "|no figures here|"))
        self.assertIsNone(self.read("Take Home Pay 95,170.45"))
        self.assertIsNone(self.read("a shopping list"))


class VaultTests(unittest.TestCase):
    """The vault: encrypted at rest, refusing to store plaintext, and the
    document number encrypted like the file it belongs to."""

    def setUp(self):
        db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        os.environ["PHILFORGE_DB"] = self._db_path
        os.environ["ENCRYPTION_KEY"] = (
            __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
        )
        import importlib

        import auth
        import config
        import db as core_db

        importlib.reload(config)
        core_db.config = config
        core_db._initialized = False
        core_db._init_db_sync()
        auth.config = config
        auth._fernet = None
        self.auth = auth
        import sanctuary_db

        sanctuary_db.config = config
        self.sanctuary_db = sanctuary_db

    def tearDown(self):
        os.unlink(self._db_path)
        self.auth._fernet = None
        os.environ.pop("ENCRYPTION_KEY", None)

    def test_bytes_round_trip_and_ciphertext_differs(self):
        blob = b"%PDF-1.4 the licence"
        sealed = self.auth.encrypt_bytes(blob)
        self.assertIsNotNone(sealed)
        self.assertNotIn(b"licence", sealed)
        self.assertEqual(self.auth.decrypt_bytes(sealed), blob)

    def test_no_key_means_refusal_not_plaintext(self):
        self.auth._fernet = None
        self.auth.config.ENCRYPTION_KEY = ""
        self.assertIsNone(self.auth.encrypt_bytes(b"secret"))

    def test_document_row_round_trip(self):
        async def run():
            doc_id = await self.sanctuary_db.create_document(
                1,
                {
                    "title": "Driving licence",
                    "category": "Identity",
                    "doc_number": self.auth.encrypt_value("TN-00 1234"),
                    "filename": "dl.pdf",
                    "content_type": "application/pdf",
                    "size": 1234,
                    "file_token": "ab" * 16,
                },
            )
            return await self.sanctuary_db.get_document(1, doc_id)

        doc = asyncio.run(run())
        self.assertEqual(doc["title"], "Driving licence")
        self.assertNotEqual(doc["doc_number"], "TN-00 1234", "the number must not rest in clear")
        self.assertEqual(self.auth.decrypt_value(doc["doc_number"]), "TN-00 1234")

    def test_same_content_is_found_by_its_fingerprint(self):
        """The same paper offered twice must be findable, so it is stored once."""
        import hashlib

        sha = hashlib.sha1(b"%PDF-1.4 the same paper").hexdigest()

        async def run():
            await self.sanctuary_db.create_document(
                1, {"title": "Form 16", "filename": "f16.pdf", "size": 23, "content_sha": sha}
            )
            hit = await self.sanctuary_db.find_document_by_sha(1, sha)
            miss = await self.sanctuary_db.find_document_by_sha(1, "0" * 40)
            other_user = await self.sanctuary_db.find_document_by_sha(2, sha)
            blank = await self.sanctuary_db.find_document_by_sha(1, "")
            return hit, miss, other_user, blank

        hit, miss, other_user, blank = asyncio.run(run())
        self.assertEqual(hit["title"], "Form 16")
        self.assertIsNone(miss)
        self.assertIsNone(other_user, "one user's fingerprints must not answer for another")
        self.assertIsNone(blank, "a blank fingerprint matches nothing, not everything")

    def test_old_rows_without_fingerprint_are_narrowed_by_name_and_size(self):
        async def run():
            await self.sanctuary_db.create_document(1, {"title": "Old", "filename": "a.pdf", "size": 10})
            await self.sanctuary_db.create_document(1, {"title": "Other name", "filename": "b.pdf", "size": 10})
            await self.sanctuary_db.create_document(
                1, {"title": "Stamped", "filename": "a.pdf", "size": 10, "content_sha": "ff" * 20}
            )
            rows = await self.sanctuary_db.documents_without_sha(1, "a.pdf", 10)
            await self.sanctuary_db.set_document_sha(1, rows[0]["id"], "ab" * 20)
            after = await self.sanctuary_db.documents_without_sha(1, "a.pdf", 10)
            return rows, after

        rows, after = asyncio.run(run())
        self.assertEqual([r["title"] for r in rows], ["Old"], "name+size narrows, a stamped row is excluded")
        self.assertEqual(after, [], "once stamped, the row leaves the unfingerprinted set")


class HoldingsTests(unittest.TestCase):
    """A broker's holdings export — what he owns, read from the file he
    downloads. The workbook is built here, so the figures are invented."""

    def book(self, sheets):
        """A minimal .xlsx in the shape Zerodha's Console writes."""
        import zipfile

        NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            for index, rows in enumerate(sheets, start=1):
                body = "".join(
                    "<row>" + "".join(f'<c t="inlineStr"><is><t>{c}</t></is></c>' for c in row) + "</row>"
                    for row in rows
                )
                archive.writestr(
                    f"xl/worksheets/sheet{index}.xml",
                    f'<?xml version="1.0"?><worksheet {NS}><sheetData>{body}</sheetData></worksheet>',
                )
        return buf.getvalue()

    MF = [
        ["Mutual Funds Holdings Statement as on 2026-08-27"],
        ["Summary"],
        ["Invested Value", "50000.0000"],
        ["Present Value", "60000.0000"],
        ["Unrealized P&amp;L", "10000.0000"],
        ["Symbol", "ISIN", "Instrument Type", "Quantity Available", "Average Price", "Previous Closing Price"],
        ["SOME ELSS FUND - DIRECT PLAN", "INF000X01AA1", "Equity - ELSS", "500.0000", "100.0000", "120.0000"],
    ]

    def test_a_holdings_sheet_is_read_whole(self):
        import sanctuary_holdings

        r = sanctuary_holdings.parse_holdings(self.book([self.MF]))
        self.assertEqual(r["as_on"], "2026-08-27")
        self.assertEqual((r["invested"], r["present"], r["gain"]), (50000.0, 60000.0, 10000.0))
        held = r["sheets"][0]["holdings"][0]
        self.assertEqual(held["name"], "SOME ELSS FUND - DIRECT PLAN")
        self.assertEqual((held["units"], held["invested"], held["value"]), (500.0, 50000.0, 60000.0))

    def test_the_combined_sheet_is_a_total_not_a_holding(self):
        """Zerodha repeats every figure under 'Combined'. Counting it would
        double what he owns."""
        import sanctuary_holdings

        combined = [
            ["Combined Holdings Statement as on 2026-08-27"],
            ["Summary"],
            ["Invested Value", "50000.0000"],
            ["Present Value", "60000.0000"],
        ]
        r = sanctuary_holdings.parse_holdings(self.book([self.MF, combined]))
        self.assertEqual(r["present"], 60000.0, "not 120000 — Combined is the same money said twice")
        self.assertEqual([s["kind"] for s in r["sheets"]], ["Mutual Funds"])

    def test_an_empty_side_is_not_news(self):
        """His Equity sheet holds nothing; a row of zeroes is noise."""
        import sanctuary_holdings

        empty = [
            ["Equity Holdings Statement as on 2026-08-27"],
            ["Summary"],
            ["Invested Value", "0.0000"],
            ["Present Value", "0.0000"],
        ]
        r = sanctuary_holdings.parse_holdings(self.book([empty, self.MF]))
        self.assertEqual([s["kind"] for s in r["sheets"]], ["Mutual Funds"])

    def test_a_file_that_is_not_a_holdings_export_is_refused(self):
        import sanctuary_holdings

        self.assertIsNone(sanctuary_holdings.parse_holdings(b"not a workbook at all"))
        self.assertIsNone(sanctuary_holdings.parse_holdings(self.book([[["nothing", "here"]]])))


if __name__ == "__main__":
    unittest.main()
