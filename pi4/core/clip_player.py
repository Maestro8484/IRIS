"""
core/clip_player.py - WAV clip playback for IRIS quip responses.
Uses aplay directly on the wm8960 soundcard (not the Kokoro pipeline).
"""

import os
import subprocess
import time


_CLIPS_DIR = "/home/pi/clips"


def play_clip(filename: str, stop_event=None) -> None:
    """Play a WAV file from the clips directory.
    Uses aplay via plug:dmixed (shares the software mixer with PyAudio/dmix paths).
    Falls back to 'default' device if plug:dmixed exits non-zero.
    Polls stop_event every 50 ms and terminates aplay if set. Logs which device played.
    Raises on file-not-found; logs and swallows aplay errors."""
    path = os.path.join(_CLIPS_DIR, filename)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"[CLIP] Not found: {path}")

    def _run(device: str):
        """Start aplay on device; return (stopped, returncode)."""
        proc = subprocess.Popen(["aplay", "-D", device, "-q", path])
        while proc.poll() is None:
            if stop_event and stop_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                print(f"[CLIP] Stopped: {filename}", flush=True)
                return True, 0
            time.sleep(0.05)
        return False, proc.returncode

    try:
        stopped, rc = _run("plug:dmixed")
        if stopped:
            return
        if rc == 0:
            print(f"[CLIP] Played: {filename} (device=plug:dmixed)", flush=True)
            return
        print(f"[CLIP] plug:dmixed failed (rc={rc}), retrying with default", flush=True)
        stopped, _ = _run("default")
        if not stopped:
            print(f"[CLIP] Played: {filename} (device=default)", flush=True)
    except Exception as e:
        print(f"[CLIP] aplay error ({filename}): {e}", flush=True)
