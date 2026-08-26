"""A refusal has to say what it was.

High Entry's status line showed the bare word "Error" and nothing else (Phil,
2026-08-27: "Why error on the screen"). The cause: a failed response with no
`detail` field went through `JSON.stringify(data.detail)`, which is `undefined`
for a missing key, so `new Error(undefined)` carried no message and the
fallback `String(err.message || err)` printed the Error object's own name.

A line that says only "Error" is worse than no line: it reports that something
broke and refuses to say what.
"""

import re
import unittest
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parent.parent / "static" / "philforge-app.js").read_text(encoding="utf-8")


class RecoveryErrorTests(unittest.TestCase):
    def test_the_bare_word_error_is_never_rendered(self):
        box = SCRIPT[SCRIPT.index("function _recoveryError(") :][:900]
        self.assertIn("!== 'Error'", box, "the placeholder must be filtered out")

    def test_a_failure_falls_back_to_something_that_names_it(self):
        self.assertIn("function _recoveryFailure(", SCRIPT)
        helper = SCRIPT[SCRIPT.index("function _recoveryFailure(") :][:700]
        # the server's own words first, then the status, then a sentence
        self.assertIn("_apiErrorMessage(data, '')", helper)
        self.assertIn("res.status", helper)

    def test_neither_handler_stringifies_a_missing_detail(self):
        # JSON.stringify(undefined) is what produced the empty message.
        for name in ("recoveryAddMother", "recoveryStart"):
            with self.subTest(handler=name):
                body = SCRIPT[SCRIPT.index(f"async function {name}(") :][:2400]
                self.assertNotIn("JSON.stringify(data.detail)", body)
                self.assertIn("_recoveryFailure(err, data, res,", body)

    def test_a_non_json_body_cannot_become_the_error(self):
        # A 500 can answer in HTML; res.json() would throw and mask the cause.
        for name in ("recoveryAddMother", "recoveryStart"):
            with self.subTest(handler=name):
                body = SCRIPT[SCRIPT.index(f"async function {name}(") :][:2400]
                self.assertIsNotNone(re.search(r"res\.json\(\)\.catch\(\(\) => null\)", body))


if __name__ == "__main__":
    unittest.main()
