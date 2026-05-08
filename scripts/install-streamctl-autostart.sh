#!/bin/sh
# Install and enable mjpg-streamctl at boot (systemd).
# Run from the repo root, as the user that should run the service, e.g.:
#   sudo ./scripts/install-streamctl-autostart.sh
#
# The effective install directory is the parent of scripts/ (absolute path).
# The generated unit uses that path for WorkingDirectory, MJPG_STREAMER_ROOT, and ExecStart.

set -e
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
UNIT_TEMPLATE="$ROOT/systemd/mjpg-streamctl.service.in"
UNIT_DST=/etc/systemd/system/mjpg-streamctl.service

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

if [ ! -f "$UNIT_TEMPLATE" ]; then
  echo "Missing $UNIT_TEMPLATE" >&2
  exit 1
fi

if [ ! -f "$ROOT/streamctl_service.py" ]; then
  echo "Missing $ROOT/streamctl_service.py (wrong ROOT?)" >&2
  exit 1
fi

# User/group for the service: prefer the user who invoked sudo.
if [ -n "${SUDO_USER}" ]; then
  INSTALL_USER="${SUDO_USER}"
  INSTALL_GROUP=$(id -gn "${SUDO_USER}" 2>/dev/null || echo "${SUDO_USER}")
else
  INSTALL_USER=$(id -un)
  INSTALL_GROUP=$(id -gn)
fi

export INSTALL_ROOT="$ROOT"
export SERVICE_USER="$INSTALL_USER"
export SERVICE_GROUP="$INSTALL_GROUP"

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
python3 - "$UNIT_TEMPLATE" "$tmp" <<'PY'
import os
import sys

src, dst = sys.argv[1], sys.argv[2]
root = os.environ["INSTALL_ROOT"]
user = os.environ["SERVICE_USER"]
group = os.environ["SERVICE_GROUP"]
text = open(src, encoding="utf-8").read()
text = text.replace("@@INSTALL_ROOT@@", root)
text = text.replace("@@SERVICE_USER@@", user)
text = text.replace("@@SERVICE_GROUP@@", group)
open(dst, "w", encoding="utf-8").write(text)
PY

install -m 644 "$tmp" "$UNIT_DST"
trap - EXIT
rm -f "$tmp"

systemctl daemon-reload
systemctl enable mjpg-streamctl.service
systemctl restart mjpg-streamctl.service
systemctl --no-pager --full status mjpg-streamctl.service || true

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -n "$ip" ]; then
  echo "Done. Open http://${ip}:8899/?html=1 in a browser (control page)."
else
  echo "Done. Open http://<this-host>:8899/?html=1 in a browser (control page)."
fi
