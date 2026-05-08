#!/usr/bin/env bash
# Install costwatch as systemd USER units (no sudo required).
# After install, the daemon runs as your user and the digest fires at 18:00 local daily.
#
# To survive reboots without an interactive login (recommended):
#     sudo loginctl enable-linger $USER
#
# Usage:
#     bash scripts/install-systemd.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$USER_UNIT_DIR"

for unit in costwatch.service costwatch-digest.service costwatch-digest.timer; do
    install -m 0644 "systemd/$unit" "$USER_UNIT_DIR/$unit"
    echo "installed: $USER_UNIT_DIR/$unit"
done

systemctl --user daemon-reload

# Stop any ad-hoc background daemon on port 8000 before starting under systemd.
pkill -f "python -m costwatch" 2>/dev/null || true
sleep 1

systemctl --user enable --now costwatch.service
systemctl --user enable --now costwatch-digest.timer

echo
echo "active units:"
systemctl --user --no-pager status costwatch.service | head -5 || true
echo
systemctl --user --no-pager list-timers costwatch-digest.timer || true
echo
echo "Done. Daemon is running and the daily digest is scheduled for 18:00 local."
echo "To survive reboot without login: sudo loginctl enable-linger $USER"
