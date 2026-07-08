#!/bin/bash
# drift_check.sh -- RAM-vs-SD md5 report for every file in deploy_manifest.txt.
# Read-only: never remounts /media/root-ro, never writes outside /home/pi/logs/.
# S192d (RD-038 audit AUD-3). Run manually or via cron (see iris-drift-check.cron).
set -u

MANIFEST="/home/pi/scripts/deploy_manifest.txt"
LOG_DIR="/home/pi/logs"
LOG_FILE="$LOG_DIR/drift_report_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$LOG_DIR"

{
  echo "IRIS drift report -- $(date -Iseconds)"
  echo "pi_path|ram_md5|sd_md5|verdict|flag"

  drift_count=0
  missing_count=0

  while IFS='|' read -r repo_path pi_path flag; do
    # Skip blank lines and comments
    [[ -z "$repo_path" || "$repo_path" == \#* ]] && continue

    sd_path="/media/root-ro${pi_path}"

    if [[ ! -f "$pi_path" ]]; then
      echo "${pi_path}|MISSING|-|MISSING|${flag}"
      missing_count=$((missing_count + 1))
      continue
    fi
    if [[ ! -f "$sd_path" ]]; then
      echo "${pi_path}|-|MISSING|MISSING|${flag}"
      missing_count=$((missing_count + 1))
      continue
    fi

    ram_md5=$(md5sum "$pi_path" | cut -d' ' -f1)
    sd_md5=$(md5sum "$sd_path" | cut -d' ' -f1)

    if [[ "$ram_md5" == "$sd_md5" ]]; then
      verdict="OK"
    else
      verdict="DRIFT"
      drift_count=$((drift_count + 1))
    fi

    echo "${pi_path}|${ram_md5}|${sd_md5}|${verdict}|${flag}"
  done < "$MANIFEST"

  echo "---"
  echo "SUMMARY: ${drift_count} drift, ${missing_count} missing"
} | tee "$LOG_FILE"
