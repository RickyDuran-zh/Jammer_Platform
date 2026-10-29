#!/usr/bin/env bash
#
# Build the optional C++ rfio daemon (fallback when the UHD Python bindings are
# unavailable on the board).
#
#   bash arm_port/scripts/build_cpp.sh [--prefix /usr/local]
#
# Produces build/jammer-rfio.  Requires UHD headers/libs (see build_uhd.sh).

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${PREFIX:-/usr/local}"
BUILD_DIR="$ROOT_DIR/cpp/build"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

cmake "$ROOT_DIR/cpp" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX"

make -j"$(nproc)"

echo
echo "built: $BUILD_DIR/jammer-rfio"
echo "install with: sudo cmake --install $BUILD_DIR"
echo
echo "smoke test without a radio (synthetic IQ, same wire protocol):"
echo "  $BUILD_DIR/jammer-rfio --sim --port 5601 &"
echo "  python -m jammer_arm run --radio rfio --rfio-port 5601 --frames 5"
