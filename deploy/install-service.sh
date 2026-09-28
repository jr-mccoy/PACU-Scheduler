#!/usr/bin/env bash
# Install the PACU Scheduler web interface as an always-on service, with a
# nightly database backup. Run from the repository as your normal user (not
# root); it asks for your password to write the systemd units.
#
#   deploy/install-service.sh            # database: <repo>/nurse_schedule.db
#   PACU_DB=/path/to/nurse_schedule.db PACU_PORT=8080 deploy/install-service.sh
#
# See docs/web-server.md for the whole setup.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
user="$(id -un)"
home="$(getent passwd "$user" | cut -d: -f6)"
python="$repo/.venv/bin/python"
db="${PACU_DB:-$repo/nurse_schedule.db}"
port="${PACU_PORT:-8080}"
backups="${PACU_BACKUPS:-$home/pacu-backups}"

if [[ "$user" == "root" ]]; then
  echo "Run this as your normal user, not with sudo; it uses sudo itself." >&2
  exit 1
fi
if [[ ! -x "$python" ]]; then
  echo "No virtual environment at $repo/.venv. Create it first (docs/web-server.md, step 2)." >&2
  exit 1
fi
if ! "$python" -c "import flask, waitress" 2>/dev/null; then
  echo "Flask and Waitress are missing: run $python -m pip install -r requirements-web.txt" >&2
  exit 1
fi

echo "Installing for user $user"
echo "  app:      $repo"
echo "  database: $db"
echo "  backups:  $backups (nightly, newest 30 kept)"
echo "  address:  http://127.0.0.1:$port (this machine only)"

sudo tee /etc/systemd/system/pacu-scheduler-web.service >/dev/null <<UNIT
[Unit]
Description=PACU Scheduler web interface
After=network.target

[Service]
Type=simple
User=$user
WorkingDirectory=$repo
Environment=PYTHONUNBUFFERED=1
ExecStart="$python" -m web --db "$db" --host 127.0.0.1 --port $port
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/pacu-scheduler-backup.service >/dev/null <<UNIT
[Unit]
Description=Back up the PACU Scheduler database

[Service]
Type=oneshot
User=$user
ExecStart="$python" "$repo/scripts/backup_db.py" --db "$db" --dest "$backups" --keep 30
UNIT

sudo tee /etc/systemd/system/pacu-scheduler-backup.timer >/dev/null <<UNIT
[Unit]
Description=Nightly PACU Scheduler database backup

[Timer]
OnCalendar=*-*-* 02:30:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now pacu-scheduler-web.service pacu-scheduler-backup.timer

sleep 2
if curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then
  echo "Running: open http://127.0.0.1:$port on this machine."
else
  echo "The service did not answer yet. Check: journalctl -u pacu-scheduler-web -n 50" >&2
fi
