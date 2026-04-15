#!/usr/bin/env bash
# install-host-system-deps.sh — apt prerequisites for install-host-deps.sh.
#
# install-host-deps.sh installs Python packages with pip --user. Several
# of those packages (pillow transitively, camoufox's headful fallback)
# need C build tools and X11 stubs that aren't part of the base VPS image.
# Run this once per VPS as a prerequisite to install-host-deps.sh.
#
# Idempotent — apt-get install is a no-op when packages are already
# present. Requires sudo because apt mutates /var/lib/dpkg.
#
# USAGE:
#   ssh openclaw@<vps> "sudo ~/repo/ops/scripts/install-host-system-deps.sh"

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "[host-system-deps] ERROR: must run as root (use sudo)" >&2
  exit 1
fi

PACKAGES=(
  # Pillow build prerequisites — JPEG, freetype, zlib, PNG headers
  libjpeg-dev
  libfreetype6-dev
  zlib1g-dev
  libpng-dev
  # Camoufox headful fallback — virtual framebuffer + minimal WM
  xvfb
  openbox
)

echo "[host-system-deps] apt-get update..."
apt-get update -qq

echo "[host-system-deps] installing: ${PACKAGES[*]}"
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${PACKAGES[@]}"

echo "[host-system-deps] ok — apt prerequisites in place"
