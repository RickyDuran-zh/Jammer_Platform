#!/usr/bin/env bash
#
# Jammer Platform one-command launcher.
# Runs Python UI, Python RL service, and the selected MATLAB backend together.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-}"
ALGO="ppo"
UI_ENV="${UI_ENV:-jammer_ui}"
RL_ENV="${RL_ENV:-jammer_rl}"
CONDA_BIN="${CONDA_BIN:-conda}"
MATLAB_BIN="${MATLAB_BIN:-matlab}"
ALLOW_RF=0
START_UI=1
START_RL=1
DRY_RUN=0
LOG_DIR="$ROOT_DIR/logs"

UI_PID=""
RL_PID=""
MATLAB_PID=""

usage() {
    cat <<'EOF'
Usage:
  bash scripts/run_platform.sh mock [options]
  bash scripts/run_platform.sh live [options]

Modes:
  mock                 Start UI + RL + MATLAB mock backend (no USRP TX).
  live                 Start UI + RL + MATLAB X310 backend. TX remains disabled
                       unless --allow-rf is supplied.

Options:
  --algo NAME          RL algorithm: ppo, a2c, or bdq (default: ppo).
  --allow-rf           Arm RF transmission for live mode only. Use only in an
                       authorized shielded or cabled laboratory setup.
  --ui-env NAME        Conda environment for the PyQt UI (default: jammer_ui).
  --rl-env NAME        Conda environment for the RL service (default: jammer_rl).
  --conda-bin PATH     Conda executable/command (default: conda).
  --matlab-bin PATH    MATLAB executable/command (default: matlab).
  --no-ui              Do not start the Python UI.
  --no-rl              Do not start the RL service.
  --dry-run            Print the resolved launch plan without starting processes.
  -h, --help           Show this help.

Environment overrides:
  UI_ENV, RL_ENV, CONDA_BIN, MATLAB_BIN, DEEPSEEK_API_KEY

Examples:
  bash scripts/run_platform.sh mock
  bash scripts/run_platform.sh mock --algo a2c
  bash scripts/run_platform.sh live
  bash scripts/run_platform.sh live --allow-rf --algo ppo
EOF
}

die() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

for_pid() {
    local pid="$1"
    [[ -n "$pid" ]] || return

    # Git Bash assigns POSIX PIDs to child processes. On Windows, translate
    # that PID to the native PID and terminate the full descendant tree so
    # conda.exe, Python, matlab.exe, and their children do not survive Ctrl+C.
    if command -v taskkill.exe >/dev/null 2>&1; then
        local native_pid
        native_pid="$(ps -W 2>/dev/null | awk -v bash_pid="$pid" '$1 == bash_pid { print $4; exit }')"
        if [[ -n "$native_pid" ]]; then
            printf 'Stopping process tree (Bash pid %s, Windows pid %s).\n' \
                "$pid" "$native_pid"
            taskkill.exe //PID "$native_pid" //T //F >/dev/null 2>&1 || true
            return
        fi
    fi

    if kill -0 "$pid" >/dev/null 2>&1; then
        printf 'Stopping process (Bash pid %s).\n' "$pid"
        kill "$pid" >/dev/null 2>&1 || true
    fi
}

wait_for_port() {
    local port="$1"
    local pid="$2"
    local label="$3"
    local attempt

    for ((attempt = 1; attempt <= 150; attempt++)); do
        if (echo >"/dev/tcp/127.0.0.1/$port") >/dev/null 2>&1; then
            printf '%s is listening on port %s.\n' "$label" "$port"
            return 0
        fi
        if ! kill -0 "$pid" >/dev/null 2>&1; then
            printf '%s exited before opening port %s. See logs for details.\n' \
                "$label" "$port" >&2
            return 1
        fi
        sleep 0.2
    done

    printf '%s did not open port %s within 30 seconds. See logs for details.\n' \
        "$label" "$port" >&2
    return 1
}

cleanup() {
    local status=$?
    trap - EXIT INT TERM
    printf '\nStopping launched processes...\n'
    for_pid "$MATLAB_PID" &
    for_pid "$RL_PID" &
    for_pid "$UI_PID" &
    wait || true
    wait "${MATLAB_PID:-}" 2>/dev/null || true
    wait "${RL_PID:-}" 2>/dev/null || true
    wait "${UI_PID:-}" 2>/dev/null || true
    exit "$status"
}

to_matlab_path() {
    local source_path="$1"
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -w "$source_path"
        return
    fi

    local windows_path
    if windows_path="$(cd "$source_path" && pwd -W 2>/dev/null)"; then
        printf '%s\n' "$windows_path"
    else
        printf '%s\n' "$source_path"
    fi
}

escape_matlab_string() {
    printf '%s' "$1" | sed "s/'/''/g"
}

start_ui() {
    (
        cd "$ROOT_DIR/python/app"
        exec "$CONDA_BIN" run --no-capture-output -n "$UI_ENV" python main_window.py
    ) >"$LOG_DIR/ui.log" 2>&1 &
    UI_PID=$!
    printf 'UI started (pid %s): %s\n' "$UI_PID" "$LOG_DIR/ui.log"
}

start_rl() {
    (
        cd "$ROOT_DIR/python/rl_service"
        exec "$CONDA_BIN" run --no-capture-output -n "$RL_ENV" \
            python rl_server.py --algo "$ALGO"
    ) >"$LOG_DIR/rl.log" 2>&1 &
    RL_PID=$!
    printf 'RL service started (pid %s, algo %s): %s\n' "$RL_PID" "$ALGO" "$LOG_DIR/rl.log"
}

start_matlab() {
    local matlab_root
    matlab_root="$(escape_matlab_string "$(to_matlab_path "$ROOT_DIR")")"
    local backend
    local rf_setup

    if [[ "$MODE" == "mock" ]]; then
        backend="run_mock_backend_spectrum_control"
        rf_setup="setenv('ZH_RF_TX_ENABLE', '');"
    else
        backend="run_jammer_backend"
        if [[ "$ALLOW_RF" -eq 1 ]]; then
            rf_setup="setenv('ZH_RF_TX_ENABLE', 'LAB_ONLY');"
        else
            rf_setup="setenv('ZH_RF_TX_ENABLE', '');"
        fi
    fi

    local matlab_command
    matlab_command="try, cd('$matlab_root'); addpath(fullfile(pwd, 'matlab')); project_setup; $rf_setup $backend; catch ME, disp(getReport(ME, 'extended')); exit(1); end"

    (
        cd "$ROOT_DIR"
        exec "$MATLAB_BIN" -batch "$matlab_command"
    ) >"$LOG_DIR/matlab.log" 2>&1 &
    MATLAB_PID=$!
    printf 'MATLAB backend started (pid %s, %s mode): %s\n' \
        "$MATLAB_PID" "$MODE" "$LOG_DIR/matlab.log"
}

[[ -n "$MODE" ]] || {
    usage
    exit 1
}
shift

case "$MODE" in
    mock|live) ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        die "Unknown mode: $MODE. Use mock or live."
        ;;
esac

while [[ $# -gt 0 ]]; do
    case "$1" in
        --algo)
            [[ $# -ge 2 ]] || die "--algo requires a value"
            ALGO="$2"
            shift 2
            ;;
        --allow-rf)
            ALLOW_RF=1
            shift
            ;;
        --ui-env)
            [[ $# -ge 2 ]] || die "--ui-env requires a value"
            UI_ENV="$2"
            shift 2
            ;;
        --rl-env)
            [[ $# -ge 2 ]] || die "--rl-env requires a value"
            RL_ENV="$2"
            shift 2
            ;;
        --conda-bin)
            [[ $# -ge 2 ]] || die "--conda-bin requires a value"
            CONDA_BIN="$2"
            shift 2
            ;;
        --matlab-bin)
            [[ $# -ge 2 ]] || die "--matlab-bin requires a value"
            MATLAB_BIN="$2"
            shift 2
            ;;
        --no-ui)
            START_UI=0
            shift
            ;;
        --no-rl)
            START_RL=0
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "Unknown option: $1"
            ;;
    esac
done

case "$ALGO" in
    ppo|a2c|bdq) ;;
    *) die "Unsupported RL algorithm: $ALGO. Use ppo, a2c, or bdq." ;;
esac

if [[ "$ALLOW_RF" -eq 1 && "$MODE" != "live" ]]; then
    die "--allow-rf is only valid with live mode"
fi

if [[ "$ALLOW_RF" -eq 1 ]]; then
    printf '%s\n' 'WARNING: RF TX will be armed for the live MATLAB backend.'
    printf '%s\n' 'Proceed only in an authorized shielded or cabled laboratory setup.'
fi

printf 'Mode: %s | RL: %s | UI env: %s | RL env: %s\n' \
    "$MODE" "$ALGO" "$UI_ENV" "$RL_ENV"
printf 'MATLAB: %s | Conda: %s\n' "$MATLAB_BIN" "$CONDA_BIN"

if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '%s\n' 'Dry run complete; no processes were started.'
    exit 0
fi

require_command "$MATLAB_BIN"
if [[ "$START_UI" -eq 1 || "$START_RL" -eq 1 ]]; then
    require_command "$CONDA_BIN"
fi

mkdir -p "$LOG_DIR"
trap cleanup EXIT INT TERM

if [[ "$START_UI" -eq 1 ]]; then
    start_ui
    wait_for_port 5555 "$UI_PID" "UI" || die "UI startup failed"
fi
if [[ "$START_RL" -eq 1 ]]; then
    start_rl
    wait_for_port 6001 "$RL_PID" "RL service" || die "RL service startup failed"
fi

start_matlab

printf '%s\n' 'All requested components are running. Press Ctrl+C to stop them.'
wait "$MATLAB_PID"
