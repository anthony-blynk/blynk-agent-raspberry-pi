#!/usr/bin/env bash
# Builds a pre-baked Pi 5 image: Docker, this project's docker-compose.yml,
# and a blynk.env with server/template ID/vendor prefix already filled in
# (token deliberately left blank - see config/template.env) - so the image
# boots straight into BLE provisioning, no install.sh step needed.
#
# Host requirement: real arm64 Debian Bookworm/Trixie, or Raspberry Pi OS
# itself - that's rpi-image-gen's own natively-supported host, not x86_64
# (which needs containers/QEMU and is the slower, less-supported path).
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
RIG_DIR="$HERE/rpi-image-gen"
# rpi-image-gen's build command requires custom layers under a layer/
# subdirectory of -S's target (confirmed by testing - it mirrors its own
# internal device/image/layer structure), unlike the standalone
# `layer --list --path=` command, which accepts an arbitrary directory
# directly.
LAYER_FILES="$HERE/layers/layer/blynk-agent/files"

# Pinned for reproducibility - not a git submodule (deliberately: this
# project already uses plain on-demand `git clone`s elsewhere, e.g.
# install.sh's own fallback path, rather than vendoring dependencies).
# This directory is gitignored - never committed to this repo.
RIG_COMMIT="caaf5cd3c1090ec8308226c4f8992bd5ced232d7"

if [ ! -d "$RIG_DIR" ]; then
  echo "Cloning rpi-image-gen..."
  git clone https://github.com/raspberrypi/rpi-image-gen.git "$RIG_DIR"
fi
if [ "$(git -C "$RIG_DIR" rev-parse HEAD)" != "$RIG_COMMIT" ]; then
  git -C "$RIG_DIR" fetch origin "$RIG_COMMIT"
  git -C "$RIG_DIR" checkout "$RIG_COMMIT"
fi

if [ ! -f "$RIG_DIR/install_deps.sh.done" ]; then
  echo "First-time setup: installing rpi-image-gen's host dependencies (needs sudo)..."
  (cd "$RIG_DIR" && sudo ./install_deps.sh) && touch "$RIG_DIR/install_deps.sh.done"
fi

if [ ! -f "$HERE/config/template.env" ]; then
  echo "Missing $HERE/config/template.env"
  exit 1
fi

# shellcheck disable=SC1091
. "$HERE/config/template.env"

if [ -z "$BLYNK_SERVER" ] || [ -z "$BLYNK_TEMPLATE_ID" ]; then
  echo "config/template.env needs BLYNK_SERVER and BLYNK_TEMPLATE_ID filled in."
  exit 1
fi

echo "Staging blynk.env and docker-compose.yml into the layer..."
mkdir -p "$LAYER_FILES"
cp "$REPO_ROOT/docker-compose.yml" "$LAYER_FILES/docker-compose.yml"
cat > "$LAYER_FILES/blynk.env" <<EOF
BLYNK_SERVER=$BLYNK_SERVER
BLYNK_TEMPLATE_ID=$BLYNK_TEMPLATE_ID
BLYNK_AUTH_TOKEN=
BLYNK_VENDOR_PREFIX=${BLYNK_VENDOR_PREFIX:-Blynk}
EOF
# blynk-first-boot.service is static and already tracked in git at
# layers/layer/blynk-agent/files/ - nothing to stage for it.

echo "Building image (this needs real Linux, not just Docker cross-arch emulation - see README)..."
(cd "$RIG_DIR" && ./rpi-image-gen build -c "$HERE/config/blynk.yaml" -S "$HERE/layers")

echo
echo "Done - see $RIG_DIR/work/ for the built .img. Flash it with:"
echo "  sudo rpi-imager --cli <path-to-.img> /dev/sdX"
