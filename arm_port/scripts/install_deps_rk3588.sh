#!/usr/bin/env bash
#
# Prepare a LubanCat 5 / RK3588 board for the jammer ARM port.
#
#   bash arm_port/scripts/install_deps_rk3588.sh [--no-sysctl] [--dry-run]
#
# Installs build/runtime dependencies, creates the virtualenv, applies the network
# and scheduler tuning that matters for a 20 MS/s X310 stream, and (unless
# --dry-run) leaves UHD to build_uhd.sh.
#
# Run as a normal user with sudo rights.  Nothing here touches RF: transmission is
# still gated by ZH_RF_TX_ENABLE=LAB_ONLY at runtime.

set -Eeuo pipefail

DRY_RUN=0
APPLY_SYSCTL=1
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --no-sysctl) APPLY_SYSCTL=0 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '[dry-run] %s\n' "$*"
    else
        printf '+ %s\n' "$*"
        "$@"
    fi
}

echo "== system =="
uname -a
[[ -r /proc/device-tree/model ]] && echo "board: $(tr -d '\0' < /proc/device-tree/model)"
echo "python: $($PYTHON_BIN -V 2>&1)"

echo
echo "== apt packages =="
APT_PACKAGES=(
    build-essential cmake git pkg-config
    python3-venv python3-dev python3-pip
    libboost-all-dev libusb-1.0-0-dev
    libfftw3-dev libmbedtls-dev libgsm1-dev
    libsamplerate0-dev libboost-test-dev
    libncurses5-dev libssl-dev
    net-tools iproute2 ethtool
    cpufrequtils
)
run sudo apt-get update
run sudo apt-get install -y "${APT_PACKAGES[@]}"

echo
echo "== python environment =="
if [[ ! -d "$VENV_DIR" ]]; then
    run "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
run "$VENV_DIR/bin/python" -m pip install --upgrade pip wheel
run "$VENV_DIR/bin/python" -m pip install -r "$ROOT_DIR/requirements-arm.txt"

echo
echo "== network tuning (X310 on 1 GbE) =="
if [[ $APPLY_SYSCTL -eq 1 ]]; then
    SYSCTL_FILE=/etc/sysctl.d/90-jammer.conf
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "[dry-run] write $SYSCTL_FILE and apply"
    else
        sudo tee "$SYSCTL_FILE" >/dev/null <<'EOF'
# Jammer ARM port: keep UHD sample bursts from being dropped by a small socket buffer.
net.core.rmem_max = 33554432
net.core.wmem_max = 33554432
net.core.rmem_default = 8388608
net.core.wmem_default = 8388608
net.core.netdev_max_backlog = 5000
EOF
        sudo sysctl --system >/dev/null
        echo "applied $SYSCTL_FILE"
    fi
else
    echo "skipped (--no-sysctl)"
fi

echo
echo "== realtime scheduling for the backend =="
if [[ $DRY_RUN -eq 1 ]]; then
    echo "[dry-run] add CAP_SYS_NICE for the venv python"
else
    sudo setcap cap_sys_nice+ep "$(readlink -f "$VENV_DIR/bin/python")" || \
        echo "warning: setcap failed; run the backend with sudo or accept default scheduling"
fi

echo
echo "== next steps =="
cat <<EOF
1. Build UHD with Python bindings:   bash $ROOT_DIR/scripts/build_uhd.sh
2. Verify the device:                $VENV_DIR/bin/python -m jammer_arm doctor
3. Software regression (no radio):   bash $ROOT_DIR/scripts/run_stack.sh mock
4. Live RX only (TX still disabled): bash $ROOT_DIR/scripts/run_stack.sh live
EOF
