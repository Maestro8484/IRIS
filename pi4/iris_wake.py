#!/usr/bin/env python3
# iris_wake.py — Triggered by cron at 7:30 AM
# Sends EYES:WAKE + MOUTH:0 via UDP to assistant.py (which owns the serial port).
import socket, time, sys, os

# Ensure log directory exists, then redirect stdout+stderr for cron visibility
os.makedirs('/home/pi/logs', exist_ok=True)
sys.stdout = open('/home/pi/logs/iris_wake.log', 'a', buffering=1)
sys.stderr = sys.stdout

sys.path.insert(0, '/home/pi')
from core.config import CMD_PORT

def log(msg):
    print(msg, flush=True)

try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(b'EYES:WAKE\n', ('127.0.0.1', CMD_PORT))
        time.sleep(0.1)
        s.sendto(b'MOUTH:0\n', ('127.0.0.1', CMD_PORT))
    log('[WAKE] UDP commands sent to assistant.py')
except Exception as e:
    log(f'[WAKE] UDP error: {e}')

try:
    os.remove('/tmp/iris_sleep_mode')
    log('[WAKE] /tmp/iris_sleep_mode removed')
except FileNotFoundError:
    log('[WAKE] /tmp/iris_sleep_mode already absent')
except Exception as e:
    log(f'[WAKE] Flag removal error: {e}')

log('[WAKE] Wake mode activated')
