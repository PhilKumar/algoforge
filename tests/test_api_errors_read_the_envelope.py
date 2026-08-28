"""Every refusal this app shows must come from the envelope it actually sends.

`error_handlers.py` wraps refusals as {success: false, error: {code, title,
message, detail}}. The SPECIFIC reason is at error.detail, and only for 4xx --
exactly the cases a person can act on. A handler reading `data.detail` finds
nothing there and falls back to its own generic string, so the server's real
answer never reaches the screen.

That is how "Chart unavailable." hid every High Entry chart refusal (Phil,
2026-08-27). Fixing the one he could see left 20 more with the same habit,
across the admin and profile flows -- where a wrong message costs the most,
because the person reading it is trying to fix an account.
"""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "static" / "philforge-app.js").read_text(encoding="utf-8")

# `<name>.detail || <name>.message` -- the blind read. The helper's own body is
# allowed to spell it out; nothing else is.
BLIND = re.compile(r"(\w+)\.detail \|\| \1\.message")


class ErrorEnvelopeTests(unittest.TestCase):
    def test_no_handler_reads_detail_without_the_envelope(self):
        offenders = []
        for number, line in enumerate(SCRIPT.splitlines(), start=1):
            if not BLIND.search(line):
                continue
            # pfErrorText IS the fallback reader; it is allowed to say this.
            if "error.detail || error.message" in line:
                continue
            offenders.append(f"{number}: {line.strip()[:100]}")
        self.assertEqual(
            offenders,
            [],
            "these read data.detail and miss error.detail -- use _apiErrorMessage(data, fallback):\n"
            + "\n".join(offenders),
        )

    def test_the_helper_reads_both_shapes_and_422_lists(self):
        body = SCRIPT[SCRIPT.index("function _apiErrorMessage(") :][:600]
        self.assertIn("data?.detail", body)
        self.assertIn("data?.error?.detail", body)
        self.assertIn("data?.error?.message", body)
        # FastAPI validation errors arrive as a LIST of {msg}, which stringifies
        # to "[object Object]" if handed straight to a message box.
        self.assertIn("Array.isArray(detail)", body)

    def test_the_admin_and_profile_flows_use_it(self):
        # The flows where a wrong message costs the most: the reader is trying
        # to fix an account and needs the server's actual reason.
        for variable, fallback in (
            ("usersData", "'Failed to load users'"),
            ("enginesData", "'Failed to load engine status'"),
            ("data", "'Failed to create user'"),
            ("data", "'Failed to reset password'"),
            ("data", "'Failed to delete user'"),
            ("data", "'Password change failed'"),
            ("data", "'Failed to save broker credentials'"),
            ("data", "'Failed to load profile'"),
            ("data", "'IP check failed'"),
            ("d", "'Upload failed'"),
        ):
            with self.subTest(fallback=fallback):
                self.assertIn(f"_apiErrorMessage({variable}, {fallback})", SCRIPT)


if __name__ == "__main__":
    unittest.main()
