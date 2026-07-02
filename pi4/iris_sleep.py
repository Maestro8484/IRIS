#!/usr/bin/env python3
# iris_sleep.py — Triggered by cron at 9:00 PM
# Sends EYES:SLEEP via UDP to assistant.py. CMD listener calls _do_sleep() which
# handles MOUTH:8 + MOUTH_INTENSITY directly. Extra MOUTH: UDP sends trigger auto-wake.
import socket, subprocess, sys, os

# Ensure log directory exists, then redirect stdout+stderr for cron visibility
os.makedirs('/home/pi/logs', exist_ok=True)
sys.stdout = open('/home/pi/logs/iris_sleep.log', 'a', buffering=1)
sys.stderr = sys.stdout

sys.path.insert(0, '/home/pi')
from core.config import CMD_PORT

def log(msg):
    print(msg, flush=True)

try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(b'EYES:SLEEP\n', ('127.0.0.1', CMD_PORT))
    log('[SLEEP] UDP command sent to assistant.py')
except Exception as e:
    log(f'[SLEEP] UDP error: {e}')

try:
    open('/tmp/iris_sleep_mode', 'w').close()
    log('[SLEEP] /tmp/iris_sleep_mode written')
except Exception as e:
    log(f'[SLEEP] Flag write error: {e}')

# Optional goodnight chime. The previous hard-coded Piper invocation referenced
# /usr/local/bin/piper and /home/pi/piper/*.onnx -- neither exists on this box,
# so it errored every night. Kokoro TTS lives on GandalfAI, which is normally
# asleep at bedtime, so the sleep trigger must never depend on the network.
# Drop a pre-rendered /home/pi/sounds/goodnight.wav to enable a spoken goodnight;
# absent it, sleep proceeds silently (no error).
GOODNIGHT_WAV = '/home/pi/sounds/goodnight.wav'
if os.path.exists(GOODNIGHT_WAV):
    try:
        subprocess.run(['aplay', '-q', GOODNIGHT_WAV], timeout=15)
        log('[SLEEP] Goodnight chime played')
    except Exception as e:
        log(f'[SLEEP] Goodnight playback error (non-fatal): {e}')
else:
    log('[SLEEP] No goodnight.wav present -- skipping chime')

log('[SLEEP] Sleep mode activated')
