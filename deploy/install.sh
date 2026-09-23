#!/usr/bin/env bash
# Install the mint console on a fresh Ubuntu host. Run as root.
#
#   sudo bash deploy/install.sh
#
# Idempotent: safe to re-run. It never touches an existing .env.
set -euo pipefail

SERVICE_USER="${SERVICE_USER:-osnm}"
APP_DIR="${APP_DIR:-/opt/osnm-ui}"
WORKDIR="${WORKDIR:-/home/$SERVICE_USER/osnm-z}"
STATE_DIR="${STATE_DIR:-/home/$SERVICE_USER/.osnm-ui}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

echo "==> packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip curl git chrony >/dev/null

echo "==> service user: $SERVICE_USER"
id "$SERVICE_USER" >/dev/null 2>&1 || adduser --disabled-password --gecos "mint console" "$SERVICE_USER"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 750 "$WORKDIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$STATE_DIR" "$STATE_DIR/jobs"

echo "==> application -> $APP_DIR"
install -d -m 755 "$APP_DIR"
install -m 644 "$SRC/app.py" "$SRC/turbo.py" "$SRC/index.html" "$APP_DIR/"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$SRC/requirements.txt"
"$APP_DIR/venv/bin/python" -m py_compile "$APP_DIR/app.py" "$APP_DIR/turbo.py"

echo "==> config"
if [ ! -f "$WORKDIR/.env" ]; then
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 600 "$SRC/.env.example" "$WORKDIR/.env"
  echo "    wrote $WORKDIR/.env from the example - set RPC_URL in it"
else
  echo "    $WORKDIR/.env already exists, left untouched"
fi

echo "==> systemd"
sed -e "s|^User=.*|User=$SERVICE_USER|" \
    -e "s|^Group=.*|Group=$SERVICE_USER|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$APP_DIR|" \
    -e "s|OSNM_WORKDIR=.*|OSNM_WORKDIR=$WORKDIR|" \
    -e "s|OSNM_STATE_DIR=.*|OSNM_STATE_DIR=$STATE_DIR|" \
    -e "s|ExecStart=.*|ExecStart=$APP_DIR/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8787 --log-level warning|" \
    "$SRC/deploy/osnm-ui.service" > /etc/systemd/system/osnm-ui.service
systemctl daemon-reload
systemctl enable --now osnm-ui
sleep 2
systemctl is-active osnm-ui

echo
echo "==> chrony (mint timing depends on the clock)"
systemctl enable --now chrony >/dev/null 2>&1 || true
chronyc tracking 2>/dev/null | grep -E 'System time|Leap status' || true

cat <<EOF

Done. The console listens on 127.0.0.1:8787 only.

Reach it with an SSH tunnel from your own machine:

    ssh -N -L 8787:127.0.0.1:8787 $SERVICE_USER@YOUR_SERVER

then open http://127.0.0.1:8787

Standard mode additionally needs the opensea-mint binary at
${OSNM_BIN:-/usr/local/bin/opensea-mint}; see the README. Turbo mode does not.
EOF
