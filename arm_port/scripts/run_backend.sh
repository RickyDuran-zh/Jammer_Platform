#!/usr/bin/env bash
#
# systemd-friendly backend entry point (no UI, no RL service - those are separate
# units).  Honours the environment exactly like the manual path:
#
#   ZH_RF_TX_ENABLE=LAB_ONLY   arms transmission (only in an authorized setup)
#
# Overridable: JAMMER_CONFIG, JAMMER_RADIO, JAMMER_PYTHON, JAMMER_EXTRA_ARGS.

set -Eeuo pipefail

ARM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${JAMMER_PYTHON:-$ARM_DIR/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="python3"

CONFIG_ARGS=()
[[ -n "${JAMMER_CONFIG:-}" ]] && CONFIG_ARGS+=(--config "$JAMMER_CONFIG")

export PYTHONPATH="$ARM_DIR/python${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON_BIN" -u -m jammer_arm run \
    --radio "${JAMMER_RADIO:-uhd}" \
    "${CONFIG_ARGS[@]}" \
    --status /run/jammer/status.json \
    ${JAMMER_EXTRA_ARGS:-}
