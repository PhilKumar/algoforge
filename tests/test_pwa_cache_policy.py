import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class PwaCachePolicyTests(unittest.TestCase):
    def test_authenticated_pages_and_user_assets_are_not_shell_cached(self):
        source = (REPO_ROOT / "static" / "sw.js").read_text(encoding="utf-8")
        shell = source.split("self.addEventListener('install'", 1)[0]
        for private_path in ("'/'", "'/charts-viewer'", "'/market-movers'", "'/study-lounge'"):
            self.assertNotIn(private_path, shell)
        self.assertIn("fetch(request, { cache: 'no-store' })", source)
        self.assertIn("if (!publicShellAsset) return;", source)
        self.assertNotIn("'/charts-static/'", shell)
        self.assertNotIn("'/study-assets/'", shell)


if __name__ == "__main__":
    unittest.main()
