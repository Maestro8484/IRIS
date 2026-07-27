#!/bin/bash
# alsa-init.sh - WM8960 mixer setup for direct Class D speaker output.
# Audio path: wm8960 Class D speaker driver -> JST 2.0 -> 2x 3W 8Ohm speakers (no external amp).
#
# DEPLOY TARGET: Pi4 /usr/local/bin/alsa-init.sh  (run by alsa-init.service).
# This repo copy is canonical. Until S224b the script existed ONLY on the Pi and
# was in no version control at all, despite being the file that decides whether
# IRIS can hear or speak.
#
# S224b - WHY THIS RESOLVES THE CARD BY NAME:
# every line used to hardcode `amixer -c 0`. The card INDEX is not stable across
# boots. On the 2026-07-21 21:31 boot the wm8960 enumerated as card 1 because the
# two vc4-hdmi devices took 0 and 2, so every command failed with
#   amixer: Cannot find the given element from control sysdefault:0
# and the codec came up at driver defaults: mic input boost 0dB instead of 29dB,
# and the PCM output mixer switched OFF. IRIS was deaf AND mute, with the mic
# reading RMS 22 ambient (about 29dB / ~28x too quiet for the wakeword model to
# ever score). alsa-init.service showed `failed` the whole time and nothing
# surfaced it, because the failure is silent from the user's side.
#
# Two hardening changes beyond the name lookup:
#   1. Controls are set by NAME, not by numid. numids are per-card ordinals and
#      are a second thing that can silently shift. Every name below was verified
#      live against the running card during S224b.
#   2. The script VERIFIES the input and output paths after setting them and
#      exits non-zero with a loud message if they did not take, so a future
#      failure shows up in `systemctl status alsa-init` instead of as a robot
#      that mysteriously stopped listening.

set -u

sleep 8

CARD=$(awk '/wm8960/{gsub(/[^0-9]/,"",$1); print $1; exit}' /proc/asound/cards)
if [ -z "${CARD:-}" ]; then
    echo "alsa-init: FATAL - no wm8960 card found in /proc/asound/cards" >&2
    cat /proc/asound/cards >&2
    exit 1
fi
echo "alsa-init: wm8960 resolved to card $CARD" >&2

set_ctl() {
    # set_ctl <control name> <value>
    if ! amixer -c "$CARD" sset "$1" "$2" >/dev/null 2>&1; then
        echo "alsa-init: WARN - failed to set '$1' to '$2'" >&2
        return 1
    fi
    return 0
}

rc=0

# Output path - playback levels and the speaker driver.
set_ctl 'Headphone' 120                     || rc=1
set_ctl 'Speaker' 127                       || rc=1
set_ctl 'Playback' 255                      || rc=1   # DAC digital, max
set_ctl 'Speaker DC' 5                      || rc=1   # max
set_ctl 'Speaker AC' 5                      || rc=1   # max

# Output path - PCM must be routed into the output mixer or playback is silent.
set_ctl 'Left Output Mixer PCM' on          || rc=1
set_ctl 'Right Output Mixer PCM' on         || rc=1

# Input path - required for mic capture via the LINPUT1/RINPUT1 boost stage.
# Without these the wakeword never fires: the signal reaching the ADC is ~29dB down.
set_ctl 'Capture' 48                        || rc=1
set_ctl 'Left Input Mixer Boost' on         || rc=1
set_ctl 'Right Input Mixer Boost' on        || rc=1
set_ctl 'Left Input Boost Mixer LINPUT1' 3  || rc=1   # 29dB
set_ctl 'Right Input Boost Mixer RINPUT1' 3 || rc=1   # 29dB

# Verify the two paths that silently break IRIS, and say so out loud if they did
# not take. A wrong value here is invisible until someone speaks to her.
boost=$(amixer -c "$CARD" sget 'Left Input Boost Mixer LINPUT1' 2>/dev/null | grep -oE '[0-9]+\.[0-9]+dB' | head -1)
opcm=$(amixer -c "$CARD" sget 'Left Output Mixer PCM' 2>/dev/null | grep -oE '\[on\]|\[off\]' | head -1)
echo "alsa-init: mic input boost = ${boost:-UNKNOWN}, output mixer PCM = ${opcm:-UNKNOWN}" >&2
[ "${boost:-}" = "29.00dB" ] || { echo "alsa-init: ERROR - mic boost not at 29.00dB; IRIS will not hear the wakeword" >&2; rc=1; }
[ "${opcm:-}"  = "[on]"    ] || { echo "alsa-init: ERROR - output mixer PCM off; IRIS will be silent" >&2; rc=1; }

exit $rc
