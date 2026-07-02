"""
services/wyoming.py - Wyoming protocol helpers
Low-level framing used by STT (Whisper), TTS (Piper), and wakeword (OWW).
All three services share this protocol; keep helpers here to avoid duplication.
"""

import json


def wy_send(sock, etype, data, payload=b""):
    """Send a Wyoming protocol frame over sock."""
    hdr = {"type": etype, "data": data}
    if payload:
        hdr["payload_length"] = len(payload)
    sock.sendall((json.dumps(hdr) + "\n").encode())
    if payload:
        sock.sendall(payload)


def read_line(sock, buf):
    """Read one newline-delimited frame from sock, returning (line_bytes, remaining_buf)."""
    while b"\n" not in buf:
        buf += sock.recv(4096)
    nl = buf.index(b"\n")
    return buf[:nl], buf[nl + 1:]
