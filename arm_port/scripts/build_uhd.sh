#!/usr/bin/env bash
#
# Build UHD from source on RK3588 with the Python API enabled.
#
#   bash arm_port/scripts/build_uhd.sh [--ref v4.9.0.0] [--prefix /usr/local] [--jobs 8]
#
# Why source and not the distribution package: Debian/Ubuntu ship UHD without the
# Python bindings, and the bindings are the only part of the stack that is not
# covered by the prebuilt packages.  Building them needs Boost.Python plus the
# numpy headers, which this script installs.
#
# The FPGA image on the X310 must be compatible with the UHD version; after
# installing, run `uhd_usrp_probe` and let it report an image/UHD mismatch.  Use
# `uhd_image_loader` (with the matching image package) to update the device.

set -Eeuo pipefail

UHD_REF="${UHD_REF:-v4.9.0.0}"
UHD_PREFIX="${UHD_PREFIX:-/usr/local}"
UHD_SRC="${UHD_SRC:-$HOME/src/uhd}"
JOBS="${JOBS:-$(nproc)}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ref) UHD_REF="$2"; shift 2 ;;
        --prefix) UHD_PREFIX="$2"; shift 2 ;;
        --jobs) JOBS="$2"; shift 2 ;;
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

echo "UHD ref    : $UHD_REF"
echo "prefix     : $UHD_PREFIX"
echo "source dir : $UHD_SRC"
echo "jobs       : $JOBS"

sudo apt-get install -y python3-dev python3-numpy libboost-python-dev cmake g++ git

if [[ -d "$UHD_SRC/.git" ]]; then
    git -C "$UHD_SRC" fetch --tags --depth 1 origin "$UHD_REF" || true
    git -C "$UHD_SRC" checkout "$UHD_REF"
else
    mkdir -p "$(dirname "$UHD_SRC")"
    git clone --depth 1 --branch "$UHD_REF" https://github.com/EttusResearch/uhd.git "$UHD_SRC"
fi

mkdir -p "$UHD_SRC/host/build"
cd "$UHD_SRC/host/build"

cmake .. \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$UHD_PREFIX" \
    -DENABLE_PYTHON_API=ON \
    -DENABLE_EXAMPLES=OFF \
    -DENABLE_TESTS=OFF \
    -DENABLE_UTILS=ON \
    -DENABLE_MANUAL=OFF

make -j"$JOBS"
sudo make install
sudo ldconfig

# The Python bindings land in <prefix>/lib/pythonX.Y/site-packages; expose them to
# every interpreter on the board (including the venv, which inherits system paths).
PY_TAG="$(python3 -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')"
SITE_DIR="$UHD_PREFIX/lib/$PY_TAG/site-packages"
if [[ -d "$SITE_DIR" ]]; then
    echo "$SITE_DIR" | sudo tee "/usr/local/lib/$PY_TAG/dist-packages/uhd.pth" >/dev/null 2>&1 || {
        sudo mkdir -p "/usr/local/lib/$PY_TAG/dist-packages"
        echo "$SITE_DIR" | sudo tee "/usr/local/lib/$PY_TAG/dist-packages/uhd.pth" >/dev/null
    }
    echo "python bindings: $SITE_DIR"
else
    echo "warning: $SITE_DIR not found; check -DENABLE_PYTHON_API and Boost.Python" >&2
fi

echo
echo "== verification =="
uhd_config_info --version || true
python3 - <<'PY' || echo "warning: python bindings not importable yet (open a new shell / check PYTHONPATH)"
import uhd
print("uhd python:", uhd.get_version_string())
PY

cat <<'EOF'

Next:
  uhd_find_devices                      # device visible on the network?
  uhd_usrp_probe --args addr=192.168.10.2
  python3 -m jammer_arm doctor

If `import uhd` fails inside the venv, add the site-packages path to
arm_port/.venv/lib/pythonX.Y/site-packages/uhd.pth or export PYTHONPATH.
EOF
