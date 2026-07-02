#!/bin/bash
# RD-031/RD-032 — capped resource-trend logger.
# Appends one CSV line per run (cron: * * * * *) and self-trims to a hard cap, so it
# can NEVER become an unbounded writer (the exact hazard RD-031 fights). Output lives in
# /home/pi (overlay/RAM) by design — bounded and does not grow the SD card.
# Deploy: /home/pi/res_trend.sh (chmod +x), persisted to /media/root-ro/home/pi/res_trend.sh.
# Read it: tail -n 60 /home/pi/logs/res_trend.csv   (or pull into the WebUI sysstat panel).

OUT=/home/pi/logs/res_trend.csv
MAX=4320   # ~3 days at 1 line/min; ~100 B/line => <500 KB ceiling

mkdir -p /home/pi/logs
ts=$(date '+%Y-%m-%dT%H:%M:%S')
read -r load1 _ _ _ < /proc/loadavg
mem=$(free -m | awk '/^Mem:/{print $3"u/"$7"a/"$2"t"}')
overlay=$(df -h / | awk 'NR==2{print $5}')
jrnl=$(journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[KMGB]+' | tail -1)
logsz=$(du -sm /home/pi/logs 2>/dev/null | cut -f1)
temp=$(vcgencmd measure_temp 2>/dev/null | grep -oE '[0-9.]+')
thr=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)

echo "$ts,load=$load1,memMB=$mem,overlay=$overlay,journal=$jrnl,logsMB=$logsz,temp=${temp}C,throttled=$thr" >> "$OUT"

# Hard cap — keep only the last $MAX lines
lines=$(wc -l < "$OUT" 2>/dev/null || echo 0)
if [ "$lines" -gt "$MAX" ]; then
    tail -n "$MAX" "$OUT" > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"
fi
