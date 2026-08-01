"""The frontend cache-bust version must move when the frontend moves.

Between 2026-07-30 and 2026-08-01, eighteen front-end commits shipped while
`static/asset-manifest.json` sat unchanged.  Nothing warned anyone: the `?v=`
URLs and the service worker's CACHE_NAME are both built from that one string, so
the SW's activate-purge never fired and it kept answering static requests
cache-first.  Browsers ran week-old JavaScript through three green deploys.

These tests pin the property that makes that impossible -- the version is
derived from the assets' CONTENT, not from anyone remembering to type a label.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


class AssetVersionTests(unittest.TestCase):
    def setUp(self):
        self._reset()
        self.addCleanup(self._reset)

    @staticmethod
    def _reset():
        app_module._ASSET_FINGERPRINT_CACHE = None
        app_module._ASSET_VERSION_CACHE = None

    def test_version_carries_a_content_fingerprint(self):
        fingerprint = app_module._asset_fingerprint()
        self.assertTrue(fingerprint, "the static tree should be readable in a checkout")
        self.assertTrue(app_module._asset_version().endswith(f"-{fingerprint}"))

    def test_editing_any_javascript_changes_the_version(self):
        before = app_module._asset_version()
        scratch = os.path.join(os.path.dirname(app_module._ASSET_MANIFEST_PATH), "_version_probe.js")
        with open(scratch, "w", encoding="utf-8") as handle:
            handle.write("// a byte that did not exist a moment ago\n")
        try:
            self._reset()
            self.assertNotEqual(app_module._asset_version(), before)
        finally:
            os.remove(scratch)
        self._reset()
        self.assertEqual(app_module._asset_version(), before)

    def test_the_fingerprint_is_computed_once_per_process(self):
        # It walks and reads every JS/CSS file; doing that per request would put
        # the whole static tree in the path of every page load.
        first = app_module._asset_fingerprint()
        self.assertEqual(app_module._ASSET_FINGERPRINT_CACHE, first)
        app_module._ASSET_FINGERPRINT_CACHE = "pinned"
        self.assertEqual(app_module._asset_fingerprint(), "pinned")

    def test_templates_receive_the_same_version(self):
        rendered = app_module._inject_asset_version("/static/philforge-app.js?v=__ASSET_VERSION__")
        self.assertNotIn("__ASSET_VERSION__", rendered)
        self.assertIn(app_module._asset_version(), rendered)


if __name__ == "__main__":
    unittest.main()
