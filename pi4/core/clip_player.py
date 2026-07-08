"""
core/clip_player.py - WAV clip playback for IRIS quip responses.
Uses aplay directly on the wm8960 soundcard (not the Kokoro pipeline).
"""

import os
import subprocess
import time


_CLIPS_DIR = "/home/pi/clips"


def play_clip(filename: str, stop_event=None) -> bool:
    """Play a WAV file from the clips directory.
    Uses aplay via plug:dmixed (shares the software mixer with PyAudio/dmix paths).
    Falls back to 'default' device if plug:dmixed exits non-zero.
    Polls stop_event every 50 ms and terminates aplay if set. Logs which device played.
    Raises on file-not-found; logs and swallows aplay errors.
    Returns True if a clip actually played (or was interrupted mid-play), False if
    BOTH devices failed to play it. Backward-compatible: existing statement callers
    ignore the return; speak_error() uses it so its fail-quiet contract is honest."""
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
            return True
        if rc == 0:
            print(f"[CLIP] Played: {filename} (device=plug:dmixed)", flush=True)
            return True
        print(f"[CLIP] plug:dmixed failed (rc={rc}), retrying with default", flush=True)
        stopped, rc2 = _run("default")
        if stopped:
            return True
        if rc2 == 0:
            print(f"[CLIP] Played: {filename} (device=default)", flush=True)
            return True
        print(f"[CLIP] default also failed (rc={rc2}) -- clip did not play", flush=True)
        return False
    except Exception as e:
        print(f"[CLIP] aplay error ({filename}): {e}", flush=True)
        return False
