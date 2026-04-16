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
  # P1.2 — inter-agent isolation. Each agent's cron-invoked script
  # runs inside an unprivileged user namespace via bwrap so a
  # compromised agent can't read another agent's workspace files.
  # Mr Fixit (fix-it) explicitly opts out — see
  # feedback_fixit_bubblewrap_exempt.md.
  bubblewrap
)

echo "[host-system-deps] apt-get update..."
apt-get update -qq

echo "[host-system-deps] installing: ${PACKAGES[*]}"
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${PACKAGES[@]}"

# P1.2 — Ubuntu 24.04 ships with apparmor_restrict_unprivileged_userns=1
# by default, which blocks bwrap from setting up its uid map. Disable
# the restriction so unprivileged user namespaces work for the
# bubblewrap-wrapped agent crons. Persisted via /etc/sysctl.d so it
# survives reboot.
SYSCTL_FILE="/etc/sysctl.d/99-clawford-bwrap.conf"
if [[ ! -f "$SYSCTL_FILE" ]]; then
  echo "[host-system-deps] writing $SYSCTL_FILE for bwrap userns..."
  cat > "$SYSCTL_FILE" <<EOF
# Required by bubblewrap (P1.2 inter-agent isolation). Ubuntu 24.04
# defaults to restricting unprivileged user namespaces under AppArmor;
# bwrap fails with "setting up uid map: Permission denied" otherwise.
kernel.apparmor_restrict_unprivileged_userns=0
EOF
  sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi

echo "[host-system-deps] ok — apt prerequisites in place"
