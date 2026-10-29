#!/usr/bin/env bash
#
# One-command launcher for the ARM stack (RK3588): PyQt UI, RL service, and the
# Python jammer backend.
#
#   bash arm_port/scripts/run_stack.sh mock            # no radio at all
#   bash arm_port/scripts/run_stack.sh live            # X310 RX, TX stays disabled
#   bash arm_port/scripts/run_stack.sh live --allow-rf # ONLY in an authorized
#                                                      # shielded/cabled setup
#
# Options:
#   --algo ppo|a2c|bdq     RL algorithm (default ppo)
#   --radio uhd|rfio|mock  device backend for live mode (default uhd)
#   --rfio-port N          port of the rfio daemon when --radio rfio (default 5601)
#   --no-ui / --no-rl      start only part of the stack
#   --frames N             stop the backend after N frames
#   --duration S           stop the backend after S seconds
#   --set KEY=VALUE        backend configuration override (repeatable)
#   --dry-run              print the plan and exit
#
# Logs go to logs/ (git-ignored).

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ARM_DIR="$ROOT_DIR/arm_port"
LOG_DIR="$ROOT_DIR/logs"
VENV_DIR="${VENV_DIR:-$ARM_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-}"

MODE="${1:-}"
[[ -n "$MODE" ]] || { echo "usage: $0 mock|live [options]" >&2; exit 2; }
shift || true

ALGO="ppo"
RADIO="uhd"
RFIO_PORT=5601
ALLOW_RF=0
START_UI=1
START_RL=1
DRY_RUN=0
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --algo) ALGO="$2"; shift 2 ;;
        --radio) RADIO="$2"; shift 2 ;;
        --rfio-port) RFIO_PORT="$2"; shift 2 ;;
        --allow-rf) ALLOW_RF=1; shift ;;
        --no-ui) START_UI=0; shift ;;
        --no-rl) START_RL=0; shift ;;
        --frames) EXTRA_ARGS+=(--frames "$2"); shift 2 ;;
        --duration) EXTRA_ARGS+=(--duration "$2"); shift 2 ;;
        --set) EXTRA_ARGS+=(--set "$2"); shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

case "$MODE" in
    mock|live) ;;
    *) echo "mode must be mock or live" >&2; exit 2 ;;
esac
if [[ "$ALLOW_RF" -eq 1 && "$MODE" != "live" ]]; then
    echo "--allow-rf is only valid with live" >&2
    exit 2
fi
if [[ -z "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$VENV_DIR/bin/python"
    [[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="python3"
fi

export PYTHONPATH="$ARM_DIR/python${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "$LOG_DIR"

if [[ "$MODE" == "mock" ]]; then
    BACKEND_CMD=("$PYTHON_BIN" -m jammer_arm mock --quiet "${EXTRA_ARGS[@]}")
else
    BACKEND_CMD=("$PYTHON_BIN" -m jammer_arm run --radio "$RADIO" --rfio-port "$RFIO_PORT" \
                 --quiet "${EXTRA_ARGS[@]}")
fi

echo "mode      : $MODE"
echo "python    : $PYTHON_BIN"
echo "rl algo   : $ALGO"
echo "ui        : $START_UI"
echo "rl        : $START_RL"
echo "rf tx     : $( [[ "$ALLOW_RF" -eq 1 ]] && echo 'ARMED (LAB_ONLY)' || echo 'disabled' )"
echo "backend   : ${BACKEND_CMD[*]}"
echo "logs      : $LOG_DIR"

if [[ "$DRY_RUN" -eq 1 ]]; then
    exit 0
fi

PIDS=()
cleanup() {
    local code=$?
    trap - INT TERM EXIT
    echo
    echo "stopping stack..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    exit $code
}
trap cleanup INT TERM EXIT

if [[ "$START_UI" -eq 1 ]]; then
    ( cd "$ROOT_DIR/python/app" && exec "$PYTHON_BIN" -u main_window.py ) \
        > "$LOG_DIR/ui.log" 2>&1 &
    PIDS+=($!)
    echo "ui        : pid ${PIDS[-1]} -> $LOG_DIR/ui.log"
    sleep 2
fi

if [[ "$START_RL" -eq 1 ]]; then
    ( cd "$ROOT_DIR/python/rl_service" && exec "$PYTHON_BIN" -u rl_server.py --algo "$ALGO" ) \
        > "$LOG_DIR/rl.log" 2>&1 &
    PIDS+=($!)
    echo "rl        : pid ${PIDS[-1]} -> $LOG_DIR/rl.log"
    sleep 1
fi

if [[ "$ALLOW_RF" -eq 1 ]]; then
    export ZH_RF_TX_ENABLE=LAB_ONLY
    echo
    echo "*** RF TRANSMISSION ARMED: ensure the X310 is in a shield box or on an"
    echo "*** adequately attenuated cabled link before continuing."
    echo
fi

"${BACKEND_CMD[@]}" 2>&1 | tee "$LOG_DIR/backend.log"
