"""The provident fund, read off its own papers.

Every number here is invented. The shapes are the EPFO's — a year-wise
member passbook, a claim receipt from the current portal, and the older
combined claim form — but no real member id, UAN or balance appears.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sanctuary_epf as epf  # noqa: E402

PASSBOOK_RUNNING = """
lnL; iklcqd / Member Passbook
LFkkiuk vkbZMh@uke | Establishment ID/Name AAXXX1111111111 / EXAMPLE WORKS PRIVATE LIMITED
lnL; vkbZMh@uke | Member ID/Name AAXXX11111111111111111 / A MEMBER
tUe frfFk | Date of Birth 01-01-1980
;w , u | UAN 100000000001
bZih,Q iklcqd [ foÙkh; o"kZ - 2026-2027 ] / EPF Passbook [ Financial Year - 2026-2027 ]
OB Int. Updated upto 31/03/2026 10,000 20,000 30,000
Mar-2026 01-04-2026 CR Cont. for Due-Month 042026 50,000 15,000 1,000 800 1,250
Total Contributions for the year [ 2026 ] 2,000 1,600 2,500
Total Transfer-Ins/VDRs for the year [ 2026 ] 0 0 0
Total Withdrawals for the year [ 2026 ] 500 0 0
Interest details N/A 0 0 0
Closing Balance as on 31/03/2027 99,999 99,999 99,999
"""

PASSBOOK_FINISHED = """
lnL; iklcqd / Member Passbook
LFkkiuk vkbZMh@uke | Establishment ID/Name BBYYY2222222222 / OLD EMPLOYER LIMITED
lnL; vkbZMh@uke | Member ID/Name BBYYY22222222222222222 / A MEMBER
;w , u | UAN 100000000001
bZih,Q iklcqd [ foÙkh; o"kZ - 2023-2024 ] / EPF Passbook [ Financial Year - 2023-2024 ]
OB Int. Updated upto 31/03/2023 7,000 8,000 9,000
Total Contributions for the year [ 2023 ] 0 0 0
Total Transfer-Ins/VDRs for the year [ 2023 ] 0 0 0
Total Withdrawals for the year [ 2023 ] 7,500 8,600 0
Int. Updated upto 31/03/2024 500 600 0
Closing Balance as on 31/03/2024 0 0 9,000
"""

CLAIM_NEW = """
Tracking ID 100000000000001 Receipt Date11-08-2026 09:47:57
Universal Account Number (UAN) 100000000001
Member ID AAXXX11111111111111111
Date of Joining EPF 01-09-2021
Date of Exit EPF N/A
Claim Type PF Advance (FORM 31)
Advance Para Housing Related Needs
Eligible Claim Amount (Rs) 45000/- (Subject to change during processing at EPFO
Requested Claim Amount (Rs) 40000/- (Subject to change during processing at EPFO
"""

CLAIM_OLD = """
Submitted online at UAN e-seva portal on 2020-03-31 20:49:05 www.epfindia.gov.in
UAN Based Combined Claim Form 19/10C WB/31 for Advances/PF Final Settlement/Pension Fund Withdrawal
2. Universal Account Number(UAN) 100000000001
6.a Purpose of Advance OUTBREAK OF PANDEMIC (COVID-19)
6.b Amount of Advance (In Rs) 30000
Member ID BBYYY22222222222222222
UAN - 100000000001 Tracking ID - 100000000000002
"""

NOT_EPF = "e-Nomination filed on 01-01-2026 for A MEMBER. Nothing to do with money."


class PassbookTests(unittest.TestCase):
    def test_the_three_columns_are_employee_employer_pension(self):
        book = epf.read_passbook(PASSBOOK_RUNNING)
        self.assertEqual(book["opening"], {"employee": 10000.0, "employer": 20000.0, "pension": 30000.0})
        self.assertEqual(book["contributions"], {"employee": 2000.0, "employer": 1600.0, "pension": 2500.0})
        self.assertEqual(book["withdrawals"], {"employee": 500.0, "employer": 0.0, "pension": 0.0})

    def test_the_paper_says_whose_year_it_is(self):
        book = epf.read_passbook(PASSBOOK_RUNNING)
        self.assertEqual(book["year_from"], 2026)
        self.assertEqual(book["year_to"], 2027)
        self.assertEqual(book["employer_name"], "EXAMPLE WORKS PRIVATE LIMITED")
        self.assertEqual(book["closes_on"], "2027-03-31")

    def test_the_member_number_is_kept_by_its_tail_alone(self):
        book = epf.read_passbook(PASSBOOK_RUNNING)
        self.assertEqual(book["member"], "··11111")
        self.assertNotIn(book["member_id"], epf.summarise([book], date(2026, 8, 29))["accounts"][0].values())

    def test_a_paper_that_is_not_a_passbook_is_not_read_as_one(self):
        self.assertIsNone(epf.read_passbook(NOT_EPF))
        self.assertIsNone(epf.read_epf(NOT_EPF))


class BalanceTodayTests(unittest.TestCase):
    """The closing balance printed on a year still running is what the year
    WILL close at — a full year of interest, months that have not happened.
    Quoting it as today's balance shows him money he does not have."""

    def test_a_running_year_is_opened_plus_what_actually_moved(self):
        summary = epf.summarise([epf.read_passbook(PASSBOOK_RUNNING)], date(2026, 8, 29))
        account = summary["accounts"][0]
        self.assertTrue(account["still_running"])
        self.assertEqual(account["employee"], 10000 + 2000 - 500)
        self.assertEqual(account["employer"], 20000 + 1600)
        self.assertEqual(account["pension"], 30000 + 2500)
        self.assertNotEqual(account["fund"], 99999 + 99999, "the printed closing balance is a forecast")

    def test_a_finished_year_is_its_closing_balance(self):
        summary = epf.summarise([epf.read_passbook(PASSBOOK_FINISHED)], date(2026, 8, 29))
        account = summary["accounts"][0]
        self.assertFalse(account["still_running"])
        self.assertEqual(account["fund"], 0)
        self.assertEqual(account["pension"], 9000)
        self.assertTrue(account["moved_on"], "the fund was transferred; the pension stayed behind")

    def test_the_day_the_year_ends_it_becomes_history(self):
        running = epf.read_passbook(PASSBOOK_RUNNING)
        on_the_day = epf.summarise([running], date(2027, 3, 31))["accounts"][0]
        the_day_after = epf.summarise([running], date(2027, 4, 1))["accounts"][0]
        self.assertEqual(on_the_day["employee"], 11500, "still running on the last day of the year")
        self.assertEqual(the_day_after["employee"], 99999, "closed, and the printed balance is now the truth")


class WholeFundTests(unittest.TestCase):
    def setUp(self):
        self.papers = [
            epf.read_passbook(PASSBOOK_RUNNING),
            epf.read_passbook(PASSBOOK_FINISHED),
            epf.read_claim(CLAIM_NEW),
            epf.read_claim(CLAIM_OLD),
        ]

    def test_the_fund_is_the_sum_of_the_accounts(self):
        summary = epf.summarise(self.papers, date(2026, 8, 29))
        self.assertEqual(summary["fund"], 11500 + 21600)
        self.assertEqual(summary["pension"], 32500 + 9000)
        self.assertEqual(summary["worth"], 11500 + 21600 + 32500 + 9000)

    def test_one_account_keeps_only_its_newest_year(self):
        older = epf.read_passbook(PASSBOOK_RUNNING.replace("2026-2027", "2025-2026"))
        summary = epf.summarise([older, *self.papers], date(2026, 8, 29))
        self.assertEqual(len(summary["accounts"]), 2, "two employers, not three statements")
        self.assertEqual(summary["accounts"][0]["year_from"], 2026)

    def test_a_claim_answered_long_ago_is_not_money_coming(self):
        """The 2020 advance was asked, paid, and is inside the withdrawals
        the statements already show. Only a claim newer than the newest
        statement is still on its way."""
        summary = epf.summarise(self.papers, date(2026, 8, 29))
        by_date = {c["asked_on"]: c for c in summary["claims"]}
        self.assertTrue(by_date["2026-08-11"]["awaiting"])
        self.assertFalse(by_date["2020-03-31"]["awaiting"])
        self.assertEqual(summary["claimed_pending"], 40000)

    def test_a_claim_is_never_taken_off_the_balance(self):
        """The fund holds the money until it pays, and the next passbook
        will say so. Subtracting it here would hide it twice."""
        summary = epf.summarise(self.papers, date(2026, 8, 29))
        self.assertEqual(summary["fund"], 33100, "unchanged by a 40,000 claim in flight")

    def test_no_member_number_survives_into_the_summary(self):
        summary = epf.summarise(self.papers, date(2026, 8, 29))
        printed = repr(summary)
        self.assertNotIn("AAXXX11111111111111111", printed)
        self.assertNotIn("BBYYY22222222222222222", printed)
        self.assertNotIn("100000000000001", printed, "nor the claim's tracking id")

    def test_nothing_at_all_is_an_empty_fund_not_a_crash(self):
        summary = epf.summarise([], date(2026, 8, 29))
        self.assertEqual(summary["worth"], 0)
        self.assertEqual(summary["accounts"], [])
        self.assertEqual(summary["as_of"], "")


class FilingTests(unittest.TestCase):
    """A year-wise passbook downloads under its member account and nothing
    else — no word in the name says provident fund, so neither the vault's
    filing nor the fund's own scan would ever look at it."""

    def test_a_passbook_named_after_its_account_is_filed_under_epf(self):
        import sanctuary_docs

        filed = sanctuary_docs.classify_document("AAXXX11111111111111111_2019.pdf")
        self.assertEqual((filed["category"], filed["series"]), ("Finance", "EPF"))

    def test_an_ordinary_paper_is_left_where_it_was(self):
        import sanctuary_docs

        for name in ("HDFC statement 2026.pdf", "school fee receipt.pdf", "ABCDE1234.pdf"):
            with self.subTest(name=name):
                self.assertNotEqual(sanctuary_docs.classify_document(name)["series"], "EPF")

    def test_the_scan_recognises_a_member_account_in_a_name(self):
        import sanctuary_docs

        self.assertTrue(sanctuary_docs.looks_like_member_id("AAXXX11111111111111111_2026.pdf"))
        self.assertFalse(sanctuary_docs.looks_like_member_id("holiday in shimla.pdf"))


class ClaimTests(unittest.TestCase):
    def test_the_current_portal_receipt(self):
        claim = epf.read_claim(CLAIM_NEW)
        self.assertEqual(claim["claim_type"], "PF Advance (FORM 31)")
        self.assertEqual(claim["reason"], "Housing Related Needs")
        self.assertEqual(claim["eligible"], 45000)
        self.assertEqual(claim["requested"], 40000)
        self.assertEqual(claim["asked_on"], "2026-08-11")

    def test_the_older_combined_form_says_the_same_things(self):
        claim = epf.read_claim(CLAIM_OLD)
        self.assertEqual(claim["requested"], 30000)
        self.assertEqual(claim["reason"], "OUTBREAK OF PANDEMIC (COVID-19)")
        self.assertEqual(claim["asked_on"], "2020-03-31")
        self.assertEqual(claim["member"], "··22222")

    def test_a_nomination_form_is_not_a_claim(self):
        self.assertIsNone(epf.read_claim(NOT_EPF))

    def test_a_figure_the_paper_smudged_reads_as_nothing_owed(self):
        smudged = CLAIM_NEW.replace("Requested Claim Amount (Rs) 40000", "Requested Claim Amount (Rs) ----")
        self.assertIsNone(epf.read_claim(smudged), "no amount, no claim — never a guess")


if __name__ == "__main__":
    unittest.main()
