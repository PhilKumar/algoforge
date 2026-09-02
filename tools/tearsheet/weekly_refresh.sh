#!/bin/bash
# Refresh everything that publishes the five-year book, in the order it must
# happen. Run weekly.
#
# The order is the whole point. A commit on 2026-09-02 rebuilt the data and the
# landing but skipped step 3, so docs/assets/backtest-tearsheet-5yr.html — the
# document the Assets page actually serves — kept the August figures. Playwright
# caught it, the deploy was skipped, and Gap Carry did not go live until the
# render was run by hand.
#
# It stops at the first failure and it never commits or pushes: it leaves the
# working tree changed and says so, because publishing a track record is Phil's
# call, not a cron job's.
set -euo pipefail

cd "$(dirname "$0")"
REPO="$(cd ../.. && pwd)"

step() { printf '\n=== %s\n' "$1"; }

step "1/5  Is the machinery still faithful?"
# Reproduces the historical book and requires it to match what was published.
# If this is not MATCHES, nothing below should run: the numbers cannot be
# trusted, and a wrong figure on the page is worse than an old one.
if ! python3 rebuild_data.py --check | tee /tmp/pf_refresh_check.log | tail -3; then
  echo "ABORT: rebuild_data.py --check failed"; exit 1
fi
if ! grep -q "^MATCHES" /tmp/pf_refresh_check.log; then
  echo "ABORT: the check did not say MATCHES — do not publish. See /tmp/pf_refresh_check.log"
  exit 1
fi

step "2/5  Rebuild the book"
python3 rebuild_data.py --write

step "3/5  Re-render the served document   <- the step that was missed"
python3 build_report.py
echo "wrote docs/assets/backtest-tearsheet-5yr.html"

step "4/5  Regenerate the landing from the book"
python3 update_landing.py --write

step "5/5  Prove every published surface agrees"
cd "$REPO"
python3 -m pytest tests/test_published_figures_match_data.py -q
python3 tools/tearsheet/update_landing.py --check

step "What changed"
git -C "$REPO" status --short -- \
  tools/tearsheet/report_data.json \
  docs/assets/backtest-tearsheet-5yr.html \
  static/landing/forge.html \
  static/landing/dojima.js || true
echo
echo "Nothing has been committed or pushed. Review, then commit these four paths."
