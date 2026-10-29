#!/usr/bin/env bash
#
# Acceptance run for the board.  Ordered from "no hardware" to "hardware":
#
#   bash arm_port/scripts/acceptance_test.sh [--skip-live] [--frames 50]
#
#   step 1  doctor      environment, UHD bindings, ports, interlock state
#   step 2  selftest    config / DSP / protocol / safety / recovery self-checks
#   step 3  unit tests  full suite (includes MATLAB equivalence fixtures)
#   step 4  bench       DSP budget on this CPU
#   step 5  live        X310 RX path (TX disabled unless ZH_RF_TX_ENABLE is set)
#   step 6  assess      gate G4: overruns / rx failures / frame time / interlock
#
# Exit code is non-zero if any step fails.  Step 5-6 are skipped with --skip-live.

set -Eeuo pipefail

ARM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${JAMMER_PYTHON:-$ARM_DIR/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="python3"
export PYTHONPATH="$ARM_DIR/python${PYTHONPATH:+:$PYTHONPATH}"

SKIP_LIVE=0
FRAMES=50
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-live) SKIP_LIVE=1; shift ;;
        --frames) FRAMES="$2"; shift 2 ;;
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

FAILED=0
step() {
    local name="$1"; shift
    echo
    echo "=============== $name ==============="
    if "$@"; then
        echo "--- $name: PASS"
    else
        echo "--- $name: FAIL"
        FAILED=$((FAILED + 1))
    fi
}

step "1. doctor"     "$PYTHON_BIN" -m jammer_arm doctor
step "2. selftest"   "$PYTHON_BIN" -m jammer_arm selftest
step "3. unit tests" "$PYTHON_BIN" -m unittest discover -s "$ARM_DIR/tests" -t "$ARM_DIR/tests"

# The C++ wire format is only covered here; skip politely if no compiler exists.
if command -v g++ >/dev/null 2>&1 || command -v clang++ >/dev/null 2>&1; then
    step "3b. cpp wire format" bash "$ARM_DIR/scripts/test_cpp.sh"
else
    echo
    echo "3b. cpp wire format: SKIPPED (no C++ compiler; install build-essential)"
fi
step "4. bench"      "$PYTHON_BIN" -m jammer_arm bench --iterations 200

if [[ "$SKIP_LIVE" -eq 0 ]]; then
    echo
    echo "=============== 5. live X310 RX ==============="
    echo "TX remains $( [[ "${ZH_RF_TX_ENABLE:-}" == "LAB_ONLY" ]] && echo 'ARMED' || echo 'disabled' )"
    if "$PYTHON_BIN" -m jammer_arm run --radio uhd --frames "$FRAMES" --warmup 5 \
            --quiet --status /tmp/jammer-live.json; then
        echo "--- 5. live: PASS"
    else
        echo "--- 5. live: FAIL (device did not stay up for $FRAMES frames)"
        FAILED=$((FAILED + 1))
    fi

    # G4 gate: judged by code, not by reading the log.  Allow 5% frame loss for
    # frames dropped by the exception isolation path.
    MIN_FRAMES=$(( FRAMES * 95 / 100 ))
    step "6. assess (gate G4)" \
        "$PYTHON_BIN" -m jammer_arm assess --status /tmp/jammer-live.json --min-frames "$MIN_FRAMES"
else
    echo
    echo "steps 5-6 (live + assess) skipped"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then
    echo "acceptance: ALL STEPS PASSED"
else
    echo "acceptance: $FAILED step(s) FAILED"
fi
exit $(( FAILED > 0 ))
