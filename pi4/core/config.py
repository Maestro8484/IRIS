"""
core/config.py - IRIS assistant configuration constants
All static config and iris_config.json overrides live here.
Import with: from core.config import *
"""

import json as _json
import os as _os
import re

# ── Network ───────────────────────────────────────────────────────────────────
GANDALF        = "192.168.1.3"
WHISPER_PORT   = 10300
PIPER_PORT     = 10200
OLLAMA_PORT    = 11434
OWW_PORT       = 10400
CMD_PORT       = 10500  # web UI → Teensy command bridge

# ── Models ────────────────────────────────────────────────────────────────────
OLLAMA_MODEL_ADULT = "iris"
OLLAMA_MODEL_KIDS  = "iris-kids"
WAKE_WORD      = "hey_iris"
PIPER_VOICE    = "en_US-ryan-high"

# ── Chatterbox TTS (rollback only — Kokoro is primary since S38) ──────────────
CHATTERBOX_BASE_URL     = "http://192.168.1.3:8004"
CHATTERBOX_VOICE        = "iris_voice.wav"
CHATTERBOX_EXAGGERATION = 0.45
CHATTERBOX_ENABLED      = True

# ── Kokoro TTS ────────────────────────────────────────────────────────────────
KOKORO_BASE_URL  = "http://192.168.1.3:8004"
KOKORO_VOICE     = "bf_lily(0.8)+bf_emma(0.2)"  # "M" blend v3 all-female (S178); was bf_lily(0.8)+bm_george(0.2) (S167 male-tinged, unintended)
KOKORO_ENABLED   = True
KOKORO_SPEED       = 0.95  # measured/dignified M pace (S178, ear-picked); was 0.9
KOKORO_SPEED_QUIPS = 1.1  # slightly faster for wakeword quip cache (S178, was 1.15)

# ── F5-TTS (voice-clone voice DNA on GandalfAI 8005) ──────────────────────────────
# Persistent F5-TTS HTTP server on GandalfAI port 8005 (C:\IRIS\f5tts_server.py,
# IRIS_F5TTS scheduled task). Same /v1/audio/speech contract as Kokoro. When
# F5_ENABLED, synthesize() routes F5 -> Kokoro(8004) -> Piper(10200).
# S160b: DEFAULT REVERTED TO FALSE (Kokoro primary). Vetting found F5 adds
# ~4-12 s time-to-first-audio under concurrent-LLM GPU load vs Kokoro's ~0.2 s
# (docs/S160_f5tts_pipeline_vetting.md). F5 to be re-enabled only after the
# latency/nfe_step tuning + hardening session. Flip True (here or via
# iris_config.json F5_ENABLED) to put the voice-clone live voice back on.
F5_BASE_URL  = "http://192.168.1.3:8005"
F5_ENABLED   = False
F5_TIMEOUT   = 12   # s; fail fast to Kokoro on hang. NB: legit F5 replies under
                    # load can exceed this (~12 s seen for short text) -> revisit
                    # alongside nfe_step tuning when F5 is re-enabled.

# ── Audio ─────────────────────────────────────────────────────────────────────
SAMPLE_RATE    = 16000
CHANNELS       = 2
CHUNK          = 1024
RECORD_SECONDS = 10
SILENCE_SECS   = 0.8   # RD-045 (S192h): was 1.5 -- guaranteed confirm-silence tax on every
                       # adult turn; measured ambient RMS floor (~1) is far below SILENCE_RMS,
                       # so the threshold itself wasn't miscalibrated -- this only trims the
                       # wait. record_command's silence counter already resets on any voice
                       # frame mid-count, so no separate "grace" logic was needed.
SILENCE_RMS    = 300

# Kids mode overrides -- applied dynamically when _kids_mode is True
KIDS_RECORD_SECONDS   = 14
KIDS_SILENCE_SECS     = 3.5
KIDS_SILENCE_RMS      = 150

# Kids mode engagement (S144) -- fill the dead air while IRIS thinks so a
# low-attention child stays engaged. A short pre-cached "thinking" clip plays
# over the LLM/STT gap ONLY when first real audio is genuinely late.
KIDS_GAP_FILLERS      = 1      # 1=enable kids "thinking" gap fillers
KIDS_THINK_FILLER_MS  = 1200   # fire a filler only if first real audio is later than this (ms)

# Gesture feedback (S144) -- in addition to the LED flash, acknowledge a
# gesture with a short spoken cue and a TFT mouth pulse so the action is
# obvious without looking at the LED ring.
GESTURE_AUDIO_CUE     = 1      # 1=speak a one-word confirmation on gesture
GESTURE_MOUTH_CUE     = 1      # 1=pulse the TFT mouth on gesture

# ── Hardware ──────────────────────────────────────────────────────────────────
BUTTON_PIN     = 17
NUM_LEDS       = 3
TEENSY_PORT    = "/dev/ttyIRIS_EYES"
TEENSY_BAUD    = 115200
BASE_MOUNT_ENABLED = True
BASE_MOUNT_PORT    = "/dev/ttyIRIS_SERVO"
BASE_MOUNT_BAUD    = 115200
DEFAULT_EYE_IDX    = 0      # Eye index sent to Teensy on startup after POST; 0=nordicBlue

# ── APA102 LED animations ─────────────────────────────────────────────────────
LED_IDLE_PEAK      = 65     # cyan breathe normal max (0-255)
LED_IDLE_FLOOR     = 3
LED_IDLE_PERIOD    = 5.0    # seconds per full cycle
LED_KIDS_PEAK      = 62     # yellow breathe kids mode max
LED_KIDS_PERIOD    = 4.0
LED_SLEEP_PEAK     = 8      # indigo breathe sleep max (0-255 color value)
LED_SLEEP_FLOOR    = 1
LED_SLEEP_PERIOD   = 8.0
LED_SLEEP_BRIGHT   = 0xE3   # APA102 global brightness byte: 0xE0|(0-31); 0xE3=3/31≈10%, 0xFF=31/31=max

# ── Interrupt / loud-stop ─────────────────────────────────────────────────────
# RMS threshold for instant stop-playback trigger during TTS.
# Must be calibrated ABOVE speaker bleed at current volume.
# S88 bleed observed at 9k-18k RMS with SPEAKER_VOLUME=117; raised to 25000.
LOUD_STOP_THRESHOLD = 25000

# ── Volume ────────────────────────────────────────────────────────────────────
VOL_CONTROL    = "Speaker"
VOL_MIN        = 60
VOL_MAX        = 127
VOL_STEP       = 10
SPEAKER_VOLUME = 121   # default 95%; overridden by iris_config.json

# ── Follow-up / context ───────────────────────────────────────────────────────
FOLLOWUP_TIMEOUT      = 2
# S194 Rung3: when IRIS's reply ended in '?' she just asked the user a question --
# a human pauses 2-4s to think before answering, so the 2s wait-for-speech-start
# above kills the exchange it was meant to continue. Give a longer speech-start
# window in that case (adult only; kids keeps KIDS_FOLLOWUP_TIMEOUT).
FOLLOWUP_TIMEOUT_QUESTION     = 6.0
# S194 Rung3: endpoint silence while the user answers a '?' reply -- mid-answer
# thinking pauses ("it's... umm... seven?") exceed the 0.8s command endpoint and
# clip the answer. Longer only for the answer-to-a-question case (adult only;
# kids keeps KIDS_SILENCE_SECS). Main-turn command snappiness (SILENCE_SECS) is
# untouched.
SILENCE_SECS_FOLLOWUP         = 1.4
KIDS_FOLLOWUP_TIMEOUT         = 15
KIDS_MODE_INACTIVITY_TIMEOUT  = 1800   # 30 min -- auto-return to adult mode
FOLLOWUP_SHORT_LEN    = 60
FOLLOWUP_MAX_TURNS    = 3
CONTEXT_TIMEOUT_SECS  = 300
# Camera-game cadence (S168) -- keep the reciprocal game loop alive across
# guesses without re-waking, and suppress the RPQR quip cascade for a short
# grace window after a game ends so a follow-up wakeword doesn't get a
# non-sequitur quip mid-game.
GAME_FOLLOWUP_TURNS   = 5     # extra follow-up turns kept alive after a game clue
GAME_REENTRY_GRACE_S  = 20    # suppress RPQR quips this long after a game ends
# ── Response length tiers (S117) ───────────────────────────────────────────────
# IRIS is a VOICE CONVERSATIONAL robot, not a book narrator. num_predict is a
# worst-case CEILING (token cap); a terse persona normally stops well short of it.
# Sizing basis (measured S116 on Kokoro @ KOKORO_SPEED=1.0): ~0.23 s of speech per
# generated token (700 tok -> ~160 s, repeatedly). So seconds ~= num_predict * 0.23.
#   tier    tokens  worst-case speech
#   SHORT     40     ~9 s    greetings, yes/no, time, one-fact
#   MEDIUM    90     ~21 s   normal conversational reply (1-3 sentences)
#   LONG     180     ~41 s   "explain / how does / describe" -- fuller chat answer
#   MAX      400     ~92 s   STORY tier ONLY -- explicit "tell me a story" / essay
# Lowered S117 from SHORT=120/MED=350/LONG=700/MAX=1200 (those were ~28/80/160/276 s
# -- narrator-length rambling, confirmed in the S116 bench). The MAX tier is now
# reached ONLY by explicit story/long-form triggers (see _MAX_PATTERNS in
# services/llm.py; the old word-count LONG->MAX promotion was removed S117).
# ROLLBACK (if replies become too clipped/short): restore the prior values
#   NUM_PREDICT=300 SHORT=120 MEDIUM=350 LONG=700 MAX=1200 TTS_MAX_CHARS=2500
# and revert the services/llm.py classifier change, then redeploy + restart.
# S134: tiers raised -- S117 values (40/90/180/400, default 100) were cutting
# normal replies off mid-sentence (a 4-sentence persona answer ~120-160 tok > the
# 90-tok MEDIUM ceiling -> hard truncation = "cutoff before completion"). The terse
# persona still normally stops well short of these ceilings; they are worst-case caps.
NUM_PREDICT           = 160   # default (followup loop + warmup) -- conversational (S134: was 100)
NUM_PREDICT_SHORT     = 64    # greetings, yes/no, time, simple facts  (~15 s)  (S134: was 40)
NUM_PREDICT_MEDIUM    = 160   # normal conversational reply            (~37 s)  (S134: was 90)
NUM_PREDICT_LONG      = 340   # detailed-but-still-chat answers        (~78 s)  (S134: was 180)
NUM_PREDICT_MAX       = 640   # story tier ONLY -- explicit requests   (~147 s) (S134: was 400)
# ── TTS ───────────────────────────────────────────────────────────────────────
# Absolute hard backstop: NO reply -- no tier, no runaway generation -- can exceed
# ~1.5 min of audio. ~15 chars/s measured, so 1500 chars ~= 100 s (~1.67 min).
# Enforced at TWO points (S122):
#   1. assistant.py streaming loop -- cumulative dispatched-char counter; once
#      exceeded, sentence dispatch AND LLM stream consumption stop. This is the
#      live enforcement for the main voice path (per-sentence synthesis since
#      S116 made the old single-call truncation a no-op there).
#   2. services/tts.py _truncate_for_tts -- still caps any single synthesize()
#      call (follow-up loop, utility replies, quips, vision replies).
# Lowered S117 from 2500 (~167 s).
# ROLLBACK: set back to 2500 if legitimate long answers are being cut short.
TTS_MAX_CHARS         = 2400  # ~160 s hard ceiling, all tiers (S134: was 1500 -- raised so MAX-tier replies finish; S117 was 2500)
CONVERSATION_LOG      = "/home/pi/logs/conversations.jsonl"
BENCH_LOG             = "/home/pi/logs/iris_bench.jsonl"
SD_BENCH_LOG          = "/media/root-ro/home/pi/logs/iris_bench.jsonl"

# ── Camera / Vision ───────────────────────────────────────────────────────────
CAMERA_ENABLED = True
CAMERA_WIDTH   = 1024
CAMERA_HEIGHT  = 768
CAMERA_TIMEOUT = 5000
VISION_MODEL   = "iris"
# S194 Rung5: per-call budget for the Ollama vision POST. Was a hardcoded
# timeout=120 in vision.py -- a hung/slow describe froze the whole turn up to
# 2 min before any fallback. Measured live vision latency: 3.2s cold, 1.8s warm;
# 40s gives generous headroom for concurrent-LLM 3090 contention while still
# catching a genuine hang. WebUI-tunable (see _OVERRIDABLE); read per-call.
VISION_TIMEOUT = 40

VISION_TRIGGERS = {
    # contracted forms
    "what's this", "what's in front of you", "what's that",
    # Whisper spells contractions out -- always add the expanded version
    "what is this", "what is in front of you", "what is that",
    "what do you see", "what can you see",
    "look at this", "look at that",
    "what am i holding",
    "can you see", "can you see this",
    "describe this", "describe what you see",
    "what do you think this is",
    "take a picture", "take a photo",
    "what are you looking at",
    "identify this", "identify what",
    "who is this", "who is that",
}

# ── Sleep window ─────────────────────────────────────────────────────────────
SLEEP_WINDOW_START_HOUR = 21  # 9 PM
SLEEP_WINDOW_END_HOUR   = 8   # 8 AM
# S194: during the sleep window a lone wakeword just plays a quip and re-sleeps
# (nights stay quiet). TWO wakewords within this many seconds break through to a
# full listen-and-respond turn on demand; IRIS re-sleeps automatically after the
# turn via the end-of-loop sleep-window check. WebUI-tunable; read once per loop.
SLEEP_DOUBLE_WAKE_WINDOW_S = 10

# ── Sleep animation CFG defaults (SLEEP_CFG: serial keys → Teensy sleepCfg) ─
SLEEP_ANIM_SPEED          = 0.85
SLEEP_ANIM_STAR_BRIGHT_MIN = 115
SLEEP_ANIM_STAR_BRIGHT_MAX = 205
SLEEP_ANIM_STAR_TWINKLE    = 140
SLEEP_ANIM_SHOOT_COUNT     = 4
SLEEP_ANIM_SHOOT_SPEED     = 38
SLEEP_ANIM_SHOOT_LEN       = 55
SLEEP_ANIM_SHOOT_BRIGHT    = 210
SLEEP_ANIM_WARP_COUNT      = 32
SLEEP_ANIM_WARP_SPEED      = 28
SLEEP_ANIM_WARP_BRIGHT     = 175
SLEEP_ANIM_MOON_R          = 28
SLEEP_ANIM_MOON_DRIFT      = 3
SLEEP_ANIM_SATURN_R        = 18
SLEEP_ANIM_SATURN_DRIFT    = 4
SLEEP_ANIM_NEBULA_ALPHA    = 44
SLEEP_ANIM_WAVE_AMP0       = 28
SLEEP_ANIM_WAVE_AMP1       = 18
SLEEP_ANIM_WAVE_AMP2       = 10
SLEEP_ANIM_WAVE_OSC_AMP    = 34
SLEEP_ANIM_MOUTH_PULSE_A   = 140
SLEEP_ANIM_ZZZ_ALPHA0      = 191
SLEEP_ANIM_ZZZ_ALPHA1      = 158
SLEEP_ANIM_ZZZ_ALPHA2      = 128

# ── Eye trigger phrases ───────────────────────────────────────────────────────
EYES_SLEEP_TRIGGERS = {
    "turn off your eyes", "turn off eyes", "turn off the eyes",
    "close your eyes", "close eyes", "eyes off", "eyes sleep",
    "sleep your eyes", "sleep eyes", "shut your eyes", "shut eyes",
    "deactivate your eyes", "disable your eyes"
}
EYES_WAKE_TRIGGERS = {
    "turn on your eyes", "turn on eyes", "turn on the eyes",
    "open your eyes", "open eyes", "eyes on", "eyes wake",
    "wake your eyes", "wake eyes", "wake up eyes",
    "activate your eyes", "enable your eyes"
}

# ── WoL / GandalfAI ───────────────────────────────────────────────────────────
GANDALF_MAC      = "A4:BB:6D:CA:83:20"
GANDALF_WOL_IP   = "192.168.1.3"
GANDALF_WOL_PORT = 7
WOL_BOOT_TIMEOUT  = 120
WOL_POLL_INTERVAL = 5

# ── Wake word ─────────────────────────────────────────────────────────────────
OWW_THRESHOLD          = 0.65
OWW_TRIGGER_LEVEL      = 2      # consecutive activations over threshold required to fire (S176: FP mitigation, no retrain)
OWW_DRAIN_SECS         = 0.15   # audio drained after wakeword before recording starts
OWW_POST_PLAY_DRAIN_SECS = 0.5  # mic audio discarded after TTS playback to clear speaker echo

# ── Mouth TFT brightness ─────────────────────────────────────────────────────
MOUTH_INTENSITY_AWAKE = 8   # ILI9341 TFT brightness, range 0-15
MOUTH_INTENSITY_SLEEP = 5   # level 5 = BL_MAP[5] = 16/255 ≈ 6% — dim but visible; was 1 (≈0.8%, appeared blank)
MOUTH_INTENSITY_IDLE  = 8   # resting level between interactions. BL_MAP[8]=40/255≈16% — clearly visible in daytime so the firmware idle animations (breathe/drift/blink/twitch) read. Was 3 (≈2.7%, near-black: mouth + idle anims invisible after inactivity, S130). Now WebUI-adjustable.

# ── Emotion ───────────────────────────────────────────────────────────────────
VALID_EMOTIONS = {"NEUTRAL", "HAPPY", "CURIOUS", "ANGRY", "SLEEPY", "SURPRISED", "SAD", "CONFUSED", "AMUSED"}
MOUTH_MAP = {
    "NEUTRAL":   0,
    "HAPPY":     1,
    "CURIOUS":   2,
    "ANGRY":     3,
    "SLEEPY":    4,
    "SURPRISED": 5,
    "SAD":       6,
    "CONFUSED":  7,
    "AMUSED":    2,  # reuses CURIOUS/smirk expression
}

# Eye index override per emotion. -1 = no override (use userDefaultEye on Teensy).
# Positive values 0-7 send EYE:n before EMOTION:x, making emit_emotion() set
# the eye style per emotion. ANGRY/CONFUSED still trigger firmware eye swap;
# the value here controls the revert-to eye after the firmware timer expires.
EMOTION_EYE_MAP = {e: -1 for e in VALID_EMOTIONS}

EMOTION_TAG_RE = re.compile(r'^\[EMOTION:([A-Z]+)\]\s*', re.IGNORECASE)

# Un-anchored variant: catches stray [EMOTION:X] tags the model emits mid-reply
# (EMOTION_TAG_RE only extracts the leading one) so they never reach TTS. (S175)
EMOTION_TAG_ANY_RE = re.compile(r'\[EMOTION:[A-Z]+\]\s*', re.IGNORECASE)

# ── iris_config.json loader (web UI overrides) ────────────────────────────────
_OVERRIDABLE = {
    "RECORD_SECONDS", "SILENCE_SECS", "SILENCE_RMS",
    "KIDS_RECORD_SECONDS", "KIDS_SILENCE_SECS", "KIDS_SILENCE_RMS",
    "KIDS_GAP_FILLERS", "KIDS_THINK_FILLER_MS", "GESTURE_AUDIO_CUE", "GESTURE_MOUTH_CUE",
    "OWW_THRESHOLD", "OWW_TRIGGER_LEVEL", "OWW_POST_PLAY_DRAIN_SECS", "FOLLOWUP_TIMEOUT", "FOLLOWUP_TIMEOUT_QUESTION", "SILENCE_SECS_FOLLOWUP", "KIDS_FOLLOWUP_TIMEOUT", "KIDS_MODE_INACTIVITY_TIMEOUT",
    "VISION_TIMEOUT", "SLEEP_DOUBLE_WAKE_WINDOW_S",
    "FOLLOWUP_MAX_TURNS", "GAME_FOLLOWUP_TURNS", "GAME_REENTRY_GRACE_S", "CONTEXT_TIMEOUT_SECS", "NUM_PREDICT", "NUM_PREDICT_SHORT", "NUM_PREDICT_MEDIUM", "NUM_PREDICT_LONG", "NUM_PREDICT_MAX", "TTS_MAX_CHARS",
    "LOUD_STOP_THRESHOLD", "DEFAULT_EYE_IDX",
    "CHATTERBOX_VOICE", "CHATTERBOX_EXAGGERATION", "CHATTERBOX_ENABLED",
    "KOKORO_VOICE", "KOKORO_ENABLED", "KOKORO_SPEED", "KOKORO_SPEED_QUIPS",
    "F5_ENABLED",
    "VOL_MAX", "SPEAKER_VOLUME", "OLLAMA_MODEL_ADULT", "OLLAMA_MODEL_KIDS",
    "LED_IDLE_PEAK", "LED_IDLE_FLOOR", "LED_IDLE_PERIOD",
    "LED_KIDS_PEAK", "LED_KIDS_PERIOD",
    "LED_SLEEP_PEAK", "LED_SLEEP_FLOOR", "LED_SLEEP_PERIOD", "LED_SLEEP_BRIGHT",
    "MOUTH_INTENSITY_AWAKE", "MOUTH_INTENSITY_SLEEP", "MOUTH_INTENSITY_IDLE",
    "OWW_DRAIN_SECS",
    "SLEEP_ANIM_SPEED",
    "SLEEP_ANIM_STAR_BRIGHT_MIN", "SLEEP_ANIM_STAR_BRIGHT_MAX", "SLEEP_ANIM_STAR_TWINKLE",
    "SLEEP_ANIM_SHOOT_COUNT", "SLEEP_ANIM_SHOOT_SPEED", "SLEEP_ANIM_SHOOT_LEN", "SLEEP_ANIM_SHOOT_BRIGHT",
    "SLEEP_ANIM_WARP_COUNT", "SLEEP_ANIM_WARP_SPEED", "SLEEP_ANIM_WARP_BRIGHT",
    "SLEEP_ANIM_MOON_R", "SLEEP_ANIM_MOON_DRIFT",
    "SLEEP_ANIM_SATURN_R", "SLEEP_ANIM_SATURN_DRIFT",
    "SLEEP_ANIM_NEBULA_ALPHA",
    "SLEEP_ANIM_WAVE_AMP0", "SLEEP_ANIM_WAVE_AMP1", "SLEEP_ANIM_WAVE_AMP2", "SLEEP_ANIM_WAVE_OSC_AMP",
    "SLEEP_ANIM_MOUTH_PULSE_A",
    "SLEEP_ANIM_ZZZ_ALPHA0", "SLEEP_ANIM_ZZZ_ALPHA1", "SLEEP_ANIM_ZZZ_ALPHA2",
}

# Type coercion and range bounds for overridable numeric/bool keys.
# String keys (CHATTERBOX_VOICE, OLLAMA_MODEL_*) are not listed -- passed through as-is.
# Range is (min_inclusive, max_inclusive). None = no range check (bool only).
_TYPE_COERCE = {
    "LOUD_STOP_THRESHOLD":     (int,   (5000, 50000)),
    "DEFAULT_EYE_IDX":         (int,   (0, 6)),
    "RECORD_SECONDS":          (int,   (1, 60)),
    "SILENCE_SECS":            (float, (0.1, 10.0)),
    "SILENCE_RMS":             (int,   (50, 5000)),
    "KIDS_RECORD_SECONDS":     (int,   (1, 60)),
    "KIDS_SILENCE_SECS":       (float, (0.1, 15.0)),
    "KIDS_SILENCE_RMS":        (int,   (50, 5000)),
    "KIDS_GAP_FILLERS":        (bool,  None),
    "KIDS_THINK_FILLER_MS":    (int,   (300, 5000)),
    "GESTURE_AUDIO_CUE":       (bool,  None),
    "GESTURE_MOUTH_CUE":       (bool,  None),
    "OWW_THRESHOLD":           (float, (0.1, 1.0)),
    "OWW_TRIGGER_LEVEL":       (int,   (1, 5)),
    "FOLLOWUP_TIMEOUT":        (int,   (1, 60)),
    "VISION_TIMEOUT":          (int,   (5, 180)),   # S194 Rung5: vision POST budget
    "SLEEP_DOUBLE_WAKE_WINDOW_S":      (int,   (2, 60)),   # S194: double-wake sleep break-through
    "FOLLOWUP_TIMEOUT_QUESTION":       (float, (1.0, 30.0)),   # S194 Rung3: wait after IRIS asks a question
    "SILENCE_SECS_FOLLOWUP":          (float, (0.1, 10.0)),   # S194 Rung3: endpoint silence during that window
    "KIDS_FOLLOWUP_TIMEOUT":          (int,   (1, 120)),
    "KIDS_MODE_INACTIVITY_TIMEOUT":   (int,   (60, 7200)),
    "FOLLOWUP_MAX_TURNS":      (int,   (1, 20)),
    "CONTEXT_TIMEOUT_SECS":    (int,   (30, 3600)),
    "NUM_PREDICT":             (int,   (10, 2000)),
    "NUM_PREDICT_SHORT":       (int,   (10, 2000)),
    "NUM_PREDICT_MEDIUM":      (int,   (10, 2000)),
    "NUM_PREDICT_LONG":        (int,   (10, 2000)),
    "NUM_PREDICT_MAX":         (int,   (10, 2000)),
    "TTS_MAX_CHARS":           (int,   (100, 4000)),
    "CHATTERBOX_EXAGGERATION": (float, (0.0, 2.0)),
    "CHATTERBOX_ENABLED":      (bool,  None),
    "KOKORO_ENABLED":          (bool,  None),
    "F5_ENABLED":              (bool,  None),
    "KOKORO_SPEED":            (float, (0.5, 2.0)),
    "KOKORO_SPEED_QUIPS":      (float, (0.5, 2.0)),
    "VOL_MAX":                 (int,   (60, 127)),
    "SPEAKER_VOLUME":          (int,   (60, 127)),
    "LED_IDLE_PEAK":           (int,   (0, 255)),
    "LED_IDLE_FLOOR":          (int,   (0, 255)),
    "LED_IDLE_PERIOD":         (float, (0.5, 30.0)),
    "LED_KIDS_PEAK":           (int,   (0, 255)),
    "LED_KIDS_PERIOD":         (float, (0.5, 30.0)),
    "LED_SLEEP_PEAK":          (int,   (0, 255)),
    "LED_SLEEP_FLOOR":         (int,   (0, 255)),
    "LED_SLEEP_PERIOD":        (float, (0.5, 30.0)),
    "LED_SLEEP_BRIGHT":        (int,   (225, 255)),   # 0xE1=1/31 (min useful) to 0xFF=31/31 (max)
    "MOUTH_INTENSITY_AWAKE":   (int,   (0, 15)),
    "MOUTH_INTENSITY_SLEEP":   (int,   (0, 15)),
    "MOUTH_INTENSITY_IDLE":    (int,   (0, 15)),
    "OWW_DRAIN_SECS":          (float, (0.05, 1.0)),
    "OWW_POST_PLAY_DRAIN_SECS":(float, (0.0,  2.0)),
    "SLEEP_ANIM_SPEED":          (float, (0.1,  3.0)),
    "SLEEP_ANIM_STAR_BRIGHT_MIN":(int,   (20,   200)),
    "SLEEP_ANIM_STAR_BRIGHT_MAX":(int,   (100,  255)),
    "SLEEP_ANIM_STAR_TWINKLE":   (int,   (20,   255)),
    "SLEEP_ANIM_SHOOT_COUNT":    (int,   (0,    10)),
    "SLEEP_ANIM_SHOOT_SPEED":    (int,   (5,    120)),
    "SLEEP_ANIM_SHOOT_LEN":      (int,   (10,   120)),
    "SLEEP_ANIM_SHOOT_BRIGHT":   (int,   (50,   255)),
    "SLEEP_ANIM_WARP_COUNT":     (int,   (0,    60)),
    "SLEEP_ANIM_WARP_SPEED":     (int,   (5,    100)),
    "SLEEP_ANIM_WARP_BRIGHT":    (int,   (40,   255)),
    "SLEEP_ANIM_MOON_R":         (int,   (10,   50)),
    "SLEEP_ANIM_MOON_DRIFT":     (int,   (0,    15)),
    "SLEEP_ANIM_SATURN_R":       (int,   (8,    35)),
    "SLEEP_ANIM_SATURN_DRIFT":   (int,   (0,    15)),
    "SLEEP_ANIM_NEBULA_ALPHA":   (int,   (0,    120)),
    "SLEEP_ANIM_WAVE_AMP0":      (int,   (5,    60)),
    "SLEEP_ANIM_WAVE_AMP1":      (int,   (3,    40)),
    "SLEEP_ANIM_WAVE_AMP2":      (int,   (2,    25)),
    "SLEEP_ANIM_WAVE_OSC_AMP":   (int,   (0,    60)),
    "SLEEP_ANIM_MOUTH_PULSE_A":  (int,   (20,   255)),
    "SLEEP_ANIM_ZZZ_ALPHA0":     (int,   (30,   255)),
    "SLEEP_ANIM_ZZZ_ALPHA1":     (int,   (30,   255)),
    "SLEEP_ANIM_ZZZ_ALPHA2":     (int,   (30,   255)),
}


def _coerce_value(key, val):
    """
    Coerce val to the type registered in _TYPE_COERCE[key].
    Returns (coerced_value, warn_message_or_None).
    Raises ValueError if the value cannot be coerced at all.
    """
    if key not in _TYPE_COERCE:
        return val, None  # string key -- pass through

    typ, bounds = _TYPE_COERCE[key]

    if typ is bool:
        if isinstance(val, bool):
            coerced = val
        elif isinstance(val, int) and val in (0, 1):
            coerced = bool(val)
        elif isinstance(val, str) and val.lower() in (
            "true", "false", "yes", "no", "on", "off", "y", "n"
        ):
            coerced = val.lower() in ("true", "yes", "on", "y")
        else:
            raise ValueError(f"cannot convert {val!r} to bool")
        return coerced, None

    # int or float
    coerced = typ(val)  # raises ValueError/TypeError on bad input

    if bounds is not None:
        lo, hi = bounds
        if coerced < lo:
            return lo, f"{key}={val!r} below minimum {lo}, clamped to {lo}"
        if coerced > hi:
            return hi, f"{key}={val!r} above maximum {hi}, clamped to {hi}"

    return coerced, None


_CONFIG_PATH = "/home/pi/iris_config.json"


def reload_overrides():
    """Re-read iris_config.json and re-apply _OVERRIDABLE keys to this module's
    globals. Runs once at import (below) and can be called again later (S192b
    AUD-5) so a WebUI save reaches an already-running process's config without
    a service restart -- see assistant.py CMD RELOAD_CONFIG."""
    try:
        with open(_CONFIG_PATH) as _f:
            _cfg = _json.load(_f)
        _applied = []
        _ignored = []
        for _k, _v in _cfg.items():
            if _k in _OVERRIDABLE:
                try:
                    _coerced, _warn = _coerce_value(_k, _v)
                    if _warn:
                        print(f"[CFG]  WARN: {_warn}", flush=True)
                    globals()[_k] = _coerced
                    _applied.append(f"{_k}={_coerced!r}")
                except (ValueError, TypeError) as _ce:
                    print(f"[CFG]  WARN: bad value for {_k}={_v!r} ({_ce}) -- keeping default", flush=True)
            else:
                _ignored.append(_k)
        print(f"[CFG]  iris_config.json loaded: {', '.join(_applied) if _applied else 'no overrides'}", flush=True)
        if _ignored:
            print(f"[CFG]  iris_config.json ignored unknown keys: {_ignored}", flush=True)
        # Dict overrides: EMOTION_MOUTH_MAP and EMOTION_EYE_MAP
        _emm = _cfg.get("EMOTION_MOUTH_MAP")
        if isinstance(_emm, dict):
            for _e, _m in _emm.items():
                if _e in VALID_EMOTIONS and isinstance(_m, int) and 0 <= _m <= 15:
                    MOUTH_MAP[_e] = _m
        _eem = _cfg.get("EMOTION_EYE_MAP")
        if isinstance(_eem, dict):
            for _e, _idx in _eem.items():
                if _e in VALID_EMOTIONS and isinstance(_idx, int) and -1 <= _idx <= 7:
                    EMOTION_EYE_MAP[_e] = _idx
    except FileNotFoundError:
        print(f"[CFG]  iris_config.json not found, using defaults", flush=True)
    except _json.JSONDecodeError as _e:
        print(f"[CFG]  iris_config.json parse error: {_e} -- using defaults", flush=True)
    except Exception as _e:
        print(f"[CFG]  iris_config.json load failed: {_e} -- using defaults", flush=True)


reload_overrides()
