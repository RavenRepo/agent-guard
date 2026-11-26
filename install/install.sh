#!/usr/bin/env bash
# Install agent-guard on this host. Everything happens in installer.py: a staged, atomic install with system hook
# registrations, an activation proof with the real clients, and rollback if the proof fails.
#   sudo bash install/install.sh install --agent-user dev --enforce-paths /home/dev/work
#   sudo bash install/install.sh status
#   sudo bash install/install.sh undo [--purge]
# The log goes to /var/log/agent-guard/.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
exec /usr/bin/python3 -I -B "$(cd "$(dirname "$0")" && pwd)/installer.py" "$@"
