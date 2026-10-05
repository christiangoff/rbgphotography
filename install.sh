#!/usr/bin/env bash
# One-time setup on the Raspberry Pi. Run from inside the website folder:
#     bash install.sh
# It creates config.ini (if missing) and installs a service that starts the
# site on boot. Safe to run again after updates.
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
USER_NAME="$(id -un)"

python3 -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9+ needed"'

if [ ! -f config.ini ]; then
  cp config.example.ini config.ini
  echo "Created config.ini. Edit it with:  nano $DIR/config.ini"
fi
mkdir -p data

sed -e "s|^User=.*|User=$USER_NAME|" \
    -e "s|/home/pi/rbg-website|$DIR|g" deploy/rbg-website.service \
  | sudo tee /etc/systemd/system/rbg-website.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable rbg-website >/dev/null
sudo systemctl restart rbg-website
sleep 1
systemctl --no-pager --lines=3 status rbg-website || true

PORT="$(sed -n 's/^port *= *//p' config.ini | head -1)"
echo
echo "Done. Open http://$(hostname).local:${PORT:-8080} on any device on your home Wi-Fi"
echo "or http://$(hostname -I | awk '{print $1}'):${PORT:-8080}"
