#!/usr/bin/env bash
# Runs the Python runtime tests against a temporary ARTIFACT_REVIEW_HOME.
set -uo pipefail
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
A="$ROOT/skills/artifact-review"
AREV="$A/scripts/arev"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/artifact review selftest.XXXXXX")"
OUT="$WORK/results.txt"
export ARTIFACT_REVIEW_HOME="$WORK/state"

cleanup() {
  "$AREV" stop --all >/dev/null 2>&1 || true
  # A passing run keeps nothing. A failing run keeps the logs it just named.
  [ -n "${KEEP_LOGS-}" ] || rm -rf "$WORK"
}
trap cleanup EXIT

echo "== cli-foundation"
CLI_RAW="$WORK/cli-foundation.log"
if python3 "$ROOT/tests/runtime/test_cli_foundation.py" > "$CLI_RAW" 2>&1; then
  echo "PASS CLI lifecycle, registry, URL, and heartbeat foundation" | tee -a "$OUT"
else
  echo "FAIL CLI foundation - see $CLI_RAW" | tee -a "$OUT"
  tail -20 "$CLI_RAW"
fi

echo "== asset-delivery"
ASSET_RAW="$WORK/asset-delivery.log"
if python3 "$ROOT/tests/runtime/test_asset_delivery.py" > "$ASSET_RAW" 2>&1; then
  echo "PASS hashed, compressed, conditional asset delivery" | tee -a "$OUT"
else
  echo "FAIL asset delivery - see $ASSET_RAW" | tee -a "$OUT"
  tail -20 "$ASSET_RAW"
fi

echo "== review-store"
STORE_RAW="$WORK/review-store.log"
if python3 "$ROOT/tests/runtime/test_review_store.py" > "$STORE_RAW" 2>&1; then
  echo "PASS normalized SQLite persistence, migration, and recovery" | tee -a "$OUT"
else
  echo "FAIL review store - see $STORE_RAW" | tee -a "$OUT"
  tail -20 "$STORE_RAW"
fi

echo "== reports-retention"
REPORT_RAW="$WORK/reports-retention.log"
if python3 "$ROOT/tests/runtime/test_reports_retention.py" > "$REPORT_RAW" 2>&1; then
  echo "PASS reusable reports, archives, retention, and delayed shutdown" | tee -a "$OUT"
else
  echo "FAIL reports and retention - see $REPORT_RAW" | tee -a "$OUT"
  tail -20 "$REPORT_RAW"
fi

echo "== artifact-checks"
CHECKS_RAW="$WORK/artifact-checks.log"
if python3 "$ROOT/tests/runtime/test_checks.py" > "$CHECKS_RAW" 2>&1; then
  echo "PASS artifact checks, source coverage, and guidance staleness" | tee -a "$OUT"
else
  echo "FAIL artifact checks - see $CHECKS_RAW" | tee -a "$OUT"
  tail -20 "$CHECKS_RAW"
fi

echo "== text-edits"
EDITS_RAW="$WORK/text-edits.log"
if python3 "$ROOT/tests/runtime/test_text_edits.py" > "$EDITS_RAW" 2>&1; then
  echo "PASS reviewer text edits written into the artifact" | tee -a "$OUT"
else
  echo "FAIL text edits - see $EDITS_RAW" | tee -a "$OUT"
  tail -20 "$EDITS_RAW"
fi

echo "== agent-resume"
RESUME_RAW="$WORK/agent-resume.log"
if python3 "$ROOT/tests/runtime/test_agent_resume.py" > "$RESUME_RAW" 2>&1; then
  echo "PASS agent session resume from the review" | tee -a "$OUT"
else
  echo "FAIL agent resume - see $RESUME_RAW" | tee -a "$OUT"
  tail -20 "$RESUME_RAW"
fi

echo "== whiteboard-chrome"
CHROME_RAW="$WORK/whiteboard-chrome.log"
if python3 "$ROOT/tests/runtime/test_whiteboard_chrome.py" > "$CHROME_RAW" 2>&1; then
  echo "PASS diagram editor chrome hooks still match the bundle" | tee -a "$OUT"
else
  echo "FAIL whiteboard chrome - see $CHROME_RAW" | tee -a "$OUT"
  tail -20 "$CHROME_RAW"
fi

echo
if grep -q "^FAIL" "$OUT"; then
  KEEP_LOGS=1
  echo "SELFTEST: FAIL"
  echo "Logs: $WORK"
  exit 1
fi
echo "SELFTEST: PASS"
