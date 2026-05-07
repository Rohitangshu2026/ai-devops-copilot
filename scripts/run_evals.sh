#!/usr/bin/env bash
# Run the offline evaluation suite for the AI DevOps Copilot.
# Generates parameterized variants first (if --generated flag is set),
# then runs the eval harness and reports results.
#
# Usage:
#   bash scripts/run_evals.sh                # run hand-authored 20-scenario suite
#   bash scripts/run_evals.sh --generated    # generate variants then run all 100
#   bash scripts/run_evals.sh --verbose      # show per-scenario output
#   bash scripts/run_evals.sh --fail-fast    # stop on first failure

set -euo pipefail
cd "$(dirname "$0")/.."

SUITE="incidents"
VERBOSE=""
FAIL_FAST=""

for arg in "$@"; do
  case "$arg" in
    --generated) SUITE="all" ;;
    --verbose|-v) VERBOSE="--verbose" ;;
    --fail-fast) FAIL_FAST="--fail-fast" ;;
  esac
done

PYTHON="${PYTHON:-python3}"
AGENT_BACKEND="agent-backend"

echo "═══════════════════════════════════════════════════════"
echo "  AI DevOps Copilot — Eval Suite"
echo "═══════════════════════════════════════════════════════"
echo "  Suite:   $SUITE"
echo "  Python:  $($PYTHON --version 2>&1)"
echo

# Generate parameterized variants if needed
if [[ "$SUITE" == "all" ]]; then
  echo "▶ Generating parameterized fixture variants..."
  $PYTHON scripts/gen_eval_fixtures.py
  echo
fi

# Run the eval harness from repo root (sys.path set inside run_evals.py)
echo "▶ Running eval harness..."
PYTHONPATH="${AGENT_BACKEND}" \
  $PYTHON evals/run_evals.py --suite "$SUITE" $VERBOSE $FAIL_FAST

EXIT_CODE=$?

echo
if [[ "$EXIT_CODE" -eq 0 ]]; then
  echo "✅  Eval suite PASSED"
else
  echo "❌  Eval suite FAILED — review evals/results/ for details"
fi
exit $EXIT_CODE
