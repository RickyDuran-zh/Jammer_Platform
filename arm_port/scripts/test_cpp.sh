#!/usr/bin/env bash
#
# Build and run the host-side C++ tests (no UHD, no sockets, no root).
#
#   bash arm_port/scripts/test_cpp.sh
#
# Verifies the rfio wire format (cpp/include/rfio_json.hpp) - the part of the
# daemon that cannot be covered by the Python test suite because it parses bytes.
# Works with g++, clang++ or zig (CXX=... to override).

set -Eeuo pipefail

ARM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$ARM_DIR/cpp/build/tests}"
mkdir -p "$OUT_DIR"

if [[ -z "${CXX:-}" ]]; then
    for candidate in g++ clang++ c++ zig; do
        if command -v "$candidate" >/dev/null 2>&1; then
            CXX="$candidate"
            break
        fi
    done
fi
[[ -n "${CXX:-}" ]] || { echo "error: no C++ compiler found (set CXX)" >&2; exit 2; }

echo "compiler: $CXX"
# zig needs its `c++` subcommand; every other compiler is invoked directly.
if [[ "$CXX" == *zig* ]]; then
    COMPILER=("$CXX" c++)
else
    COMPILER=("$CXX")
fi

"${COMPILER[@]}" -std=c++17 -O2 -Wall -Wextra \
    -I "$ARM_DIR/cpp/include" \
    "$ARM_DIR/cpp/tests/test_json.cpp" -o "$OUT_DIR/test_json"

"$OUT_DIR/test_json"
