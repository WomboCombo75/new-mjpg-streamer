#!/bin/sh
# Install and enable mjpg-streamctl at boot (systemd).
# Run: sudo ./scripts/install-streamctl-autostart.sh

set -e
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
UNIT_SRC="$ROOT/systemd/mjpg-streamctl.service"
UNIT_DST=/etc/systemd/system/mjpg-streamctl.service

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

if [ ! -f "$UNIT_SRC" ]; then
  echo "Missing $UNIT_SRC" >&2
  exit 1
fi

install -m 644 "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable mjpg-streamctl.service
systemctl restart mjpg-streamctl.service
systemctl --no-pager --full status mjpg-streamctl.service || true
echo "Done. Open http://$(hostname -I | awk '{print $1}'):8899/?html=1 in a browser (control page)."
