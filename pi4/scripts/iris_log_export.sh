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
# Retention: size-based, removes oldest daily files when total exceeds 500MB.
# (S175: raised from 100MB -- SD has 23G+ free, 100MB was evicting forensic
# history sooner than necessary for no disk-pressure reason.)

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

# Size cap: remove oldest daily files if logs total exceeds 500MB
while true; do
    TOTAL_MB=$(du -sm "$LOGDIR"/iris-*.log 2>/dev/null | awk '{s+=$1} END{printf "%d", s+0}')
    [ "${TOTAL_MB:-0}" -le 500 ] && break
    OLDEST=$(ls -tr "$LOGDIR"/iris-*.log 2>/dev/null | head -1)
    [ -z "$OLDEST" ] && break
    rm -f "$OLDEST"
done

sync
sudo mount -o remount,ro /media/root-ro 2>/dev/null

# Secondary backup: scp the daily logs to GandalfAI C:\GPU_BOX\iris-logs\
# Key: /home/pi/.ssh/id_iris_logs (ed25519, authorized in C:\ProgramData\ssh\administrators_authorized_keys)
#
# S236: copy only the TWO NEWEST dailies, not the whole glob. The unconditional
# glob re-sent every retained file on every run: measured 45 files / 149 MB at a
# */15 cadence, so ~14 GB/day of LAN and GandalfAI disk churn to re-copy files
# that have not changed since June, and it delayed the conversation drain below
# by the ~46 s it took. Only today's file is ever appended; yesterday's only
# across a midnight boundary. Nothing is lost: verified before this change that
# GandalfAI already retains 69 dailies back to 2026-05-17, a superset of the 45
# the Pi still keeps (the prune loop above trims the Pi side, not GandalfAI).
# _RECENT is intentionally unquoted so the two paths word-split into two args;
# these names are iris-YYYYMMDD.log and never contain whitespace.
_GANDALF="gandalf@192.168.0.20"
_GANDALF_DEST="$_GANDALF:C:/IRIS/iris-logs/"
_RECENT=$(ls -t /media/root-ro/home/pi/logs/iris-*.log 2>/dev/null | head -2)
if [ -n "$_RECENT" ]; then
    scp -i /home/pi/.ssh/id_iris_logs \
        -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes \
        $_RECENT \
        "$_GANDALF_DEST" 2>/dev/null || true
fi

# S224a: drain the conversation outbox to the corpus server on GandalfAI.
# (Port was 8006; MOVED to 8021 at RD-057/S234 because 8006 is Proxmox VE's own
# web UI port. The live port comes from the drain script, not from this comment.)
# assistant.flush_conversation_log() queues one file per finished conversation into
# /home/pi/logs/outbox; the drain POSTs each one and deletes it only after an ack,
# so steady state is an empty queue and ZERO extra SD writes. It opens its own
# rw window only when something is still unacked (GandalfAI asleep), and always
# hands the card back read-only. Runs last, and never exits non-zero, so a drain
# problem cannot affect the journal export above.
/usr/bin/python3 /home/pi/scripts/iris_corpus_drain.py 2>&1 \
  | logger -t iris-corpus-drain || true
