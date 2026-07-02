#!/bin/bash
# IRIS log export — runs every 15 min via /etc/cron.d/iris-logs
#
# This IS the persistent-forensics layer for the read-only-overlay Pi4: journald is
# volatile (RAM, lost on reboot) by design to protect the SD, so this appends the
# journal to an SD-persisted daily file every 15 min — surviving rotation AND reboot
# without continuous SD writes. Covers assistant + iris-web + ogle-bridge so a crash
# in any of them (e.g. the RD-034 config-write path in iris-web) leaves a trail.
#
# APPEND mode with timestamp tracking: captures only events since last run,
# so journald rotation cannot destroy already-captured history.
# Retention: size-based, removes oldest daily files when total exceeds 100MB.

STAMP="/run/iris_log_last_ts"
LOGDIR="/media/root-ro/home/pi/logs"
TODAY=$(date +%Y%m%d)
SINCE=$(cat "$STAMP" 2>/dev/null || echo "1970-01-01 00:00:00")
NOW=$(date "+%Y-%m-%d %H:%M:%S")

# Write new timestamp before export so a slow remount doesn't create a gap
echo "$NOW" > "$STAMP"

sudo mount -o remount,rw /media/root-ro 2>/dev/null
mkdir -p "$LOGDIR"

# Append new entries since last capture to today's file. Multiple -u units are
# interleaved chronologically. iris-web.service owns config writes/persist (RD-034);
# ogle-bridge.service is the vision gaze path.
journalctl -u assistant.service -u iris-web.service -u ogle-bridge.service \
  --since="$SINCE" --output=short \
  >> "$LOGDIR/iris-${TODAY}.log" 2>/dev/null

# Size cap: remove oldest daily files if logs total exceeds 100MB
while true; do
    TOTAL_MB=$(du -sm "$LOGDIR"/iris-*.log 2>/dev/null | awk '{s+=$1} END{printf "%d", s+0}')
    [ "${TOTAL_MB:-0}" -le 100 ] && break
    OLDEST=$(ls -tr "$LOGDIR"/iris-*.log 2>/dev/null | head -1)
    [ -z "$OLDEST" ] && break
    rm -f "$OLDEST"
done

sync
sudo mount -o remount,ro /media/root-ro 2>/dev/null

# Secondary backup: scp all daily logs to GandalfAI C:\IRIS\iris-logs\
# Key: /home/pi/.ssh/id_iris_logs (ed25519, authorized in C:\ProgramData\ssh\administrators_authorized_keys)
_GANDALF="gandalf@192.168.1.3"
_GANDALF_DEST="$_GANDALF:C:/IRIS/iris-logs/"
scp -i /home/pi/.ssh/id_iris_logs \
    -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes \
    /media/root-ro/home/pi/logs/iris-*.log \
    "$_GANDALF_DEST" 2>/dev/null || true
