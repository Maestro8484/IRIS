#!/usr/bin/env bash
# kokoro_voice_audition.sh -- render a Kokoro TTS voice-audition matrix for IRIS voice tuning.
#
# Renders one WAV per recipe (voice blend + speed) from the Kokoro server and writes a
# LEGEND.txt mapping filename -> exact voice string + speed, so the operator can audition
# on desktop headphones and the winning string is ready to paste into config.py / iris_config.json.
#
# Run from SuperMaster (Git Bash) -- reaches Kokoro 8004 directly, no robot, no base64.
#   bash scripts/kokoro_voice_audition.sh
#
# To run a new round: edit OUTDIR, SENTENCE if desired, and the RECIPES list below
# (one line per recipe: "NN_name | voice_string | speed"), then re-run. One command.

set -u

KOKORO="http://192.168.1.3:8004/v1/audio/speech"

# --- Round config -----------------------------------------------------------
OUTDIR="$HOME/kokoro_voice_samples/round1"
SENTENCE="Sit down. I haven't got all day, and neither, frankly, have you. Now -- what is it you think you know?"

# Recipe list: "NN_label | voice_string | speed"
# Kokoro blend syntax: parenthesis weights, e.g. bf_lily(0.6)+bf_emma(0.4)
# Round 3: WIDE male-axis sweep (round 2's 10-30% george was inaudible). Emma stripped
# out so male gravitas is the only variable; big steps so the axis is clearly audible.
RECIPES=(
  "21_lily_pure        | bf_lily                       | 0.95"
  "22_lily_george40    | bf_lily(0.6)+bm_george(0.4)   | 0.95"
  "23_lily_george60    | bf_lily(0.4)+bm_george(0.6)   | 0.95"
  "24_lily_lewis40     | bf_lily(0.6)+bm_lewis(0.4)    | 0.95"
  "25_lily_lewis60     | bf_lily(0.4)+bm_lewis(0.6)    | 0.95"
  "26_george_dom       | bf_lily(0.3)+bm_george(0.7)   | 0.95"
  "27_isabella_george  | bf_v0isabella(0.5)+bm_george(0.5) | 0.95"
  "28_lily_george50    | bf_lily(0.5)+bm_george(0.5)   | 0.90"
)
# ---------------------------------------------------------------------------

mkdir -p "$OUTDIR"
LEGEND="$OUTDIR/LEGEND.txt"
: > "$LEGEND"
echo "IRIS Kokoro voice audition -- rendered $(date)" >> "$LEGEND"
echo "Sentence: $SENTENCE" >> "$LEGEND"
echo "" >> "$LEGEND"
printf "%-20s %-42s %s\n" "FILE" "VOICE" "SPEED" >> "$LEGEND"
printf "%-20s %-42s %s\n" "----" "-----" "-----" >> "$LEGEND"

# JSON-escape the sentence (handles the embedded apostrophes/quotes safely).
esc_sentence=$(printf '%s' "$SENTENCE" | python -c 'import json,sys; print(json.dumps(sys.stdin.read()))')

for row in "${RECIPES[@]}"; do
  label=$(echo "$row" | awk -F'|' '{gsub(/^ +| +$/,"",$1); print $1}')
  voice=$(echo "$row" | awk -F'|' '{gsub(/^ +| +$/,"",$2); print $2}')
  speed=$(echo "$row" | awk -F'|' '{gsub(/^ +| +$/,"",$3); print $3}')
  out="$OUTDIR/${label}.wav"

  body="{\"model\":\"kokoro\",\"input\":${esc_sentence},\"voice\":\"${voice}\",\"response_format\":\"wav\",\"speed\":${speed}}"
  code=$(curl -s -o "$out" -w "%{http_code}" -X POST "$KOKORO" -H "Content-Type: application/json" -d "$body")

  if [ "$code" = "200" ] && [ -s "$out" ]; then
    sz=$(wc -c < "$out" | tr -d ' ')
    printf "%-20s %-42s %s\n" "${label}.wav" "$voice" "$speed" >> "$LEGEND"
    echo "OK   $label  ($voice  speed $speed)  ${sz} bytes"
  else
    rm -f "$out"
    printf "%-20s %-42s %s   [FAILED http %s -- dropped]\n" "${label}.wav" "$voice" "$speed" "$code" >> "$LEGEND"
    echo "FAIL $label  http $code  ($voice) -- dropped"
  fi
done

echo ""
echo "Done. WAVs + LEGEND.txt in: $OUTDIR"
