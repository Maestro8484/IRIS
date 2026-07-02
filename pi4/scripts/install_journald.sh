#!/bin/bash
set -e
SRC=/home/pi/etc/journald.iris.conf
DST=/etc/systemd/journald.conf.d/iris.conf
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp "$SRC" "$DST"
sudo systemctl restart systemd-journald
echo "journald retention extended -- 500MB, 1 year"
