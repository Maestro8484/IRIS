"""
core/config.py - IRIS assistant configuration constants
All static config and iris_config.json overrides live here.
Import with: from core.config import *
"""

import json as _json
import os as _os
import re

# ── Network ───────────────────────────────────────────────────────────────────
GANDALF        = "192.168.0.20"
WHISPER_PORT   = 10300
PIPER_PORT     = 10200
OLLAMA_PORT    = 11434
OWW_PORT       = 10400
CMD_PORT       = 10500  # web UI → Teensy command bridge

# ── Models ────────────────────────────────────────────────────────────────────
# Always name the BASE alongside the tag: an Ollama tag alone says nothing about
# what the model can do, which is how VISION_MODEL="iris" read as "some separate
# vision model" for sessions (S216). Both personas are thin SYSTEM-prompt layers
# over the SAME base blob -- verified live via `ollama show --modelfile`, both
# resolve to sha256-41a5b0c3, and `ollama show iris` reports
# `architecture mistral3 / 24.0B / Capabilities: vision`.
#   iris        -> mistral-small3.2:24b (multimodal, vision baked in)
#   iris-kids   -> mistral-small3.2:24b (same base, kids SYSTEM persona)
# Bases live in ollama/iris_modelfile.txt + ollama/iris-kids_modelfile.txt.
OLLAMA_MODEL_ADULT = "iris"        # base: mistral-small3.2:24b (vision-capable)
OLLAMA_MODEL_KIDS  = "iris-kids"   # base: mistral-small3.2:24b (vision-capable)

# ── Personality continuum (S215) ──────────────────────────────────────────────
# Semantic sliders, 0.0-1.0 snapped to 5 detents by core/persona_tuning.py.
# 0.5 (center) composes an EMPTY steering clause = exactly pre-S215 behavior.
# Steering rides the USER-turn context stamp — NEVER a role:system message
# (S134). Strings + design: docs/personality_continuum_design.md.
PERSONA_TONE_ADULT = 0.5   # serious & mellow <-> playful & spiky
PERSONA_TONE_KIDS  = 0.5   # calm & gentle <-> silly & cheeky
PERSONA_ENGAGE     = 0.5   # reflective & listening <-> active & game-ready (both personas)
WAKE_WORD      = "hey_iris"
PIPER_VOICE    = "en_US-ryan-high"

# ── Chatterbox TTS (rollback only — Kokoro is primary since S38) ──────────────
CHATTERBOX_BASE_URL     = "http://192.168.0.20:8004"
CHATTERBOX_VOICE        = "iris_voice.wav"
CHATTERBOX_EXAGGERATION = 0.45
CHATTERBOX_ENABLED      = True

# ── Kokoro TTS ────────────────────────────────────────────────────────────────
KOKORO_BASE_URL  = "http://192.168.0.20:8004"
KOKORO_VOICE     = "bf_lily(0.8)+bf_emma(0.2)"  # "M" blend v3 all-female (S178); was bf_lily(0.8)+bm_george(0.2) (S167 male-tinged, unintended)
KOKORO_ENABLED   = True
KOKORO_SPEED       = 1.05  # S242: default raised 0.95 -> 1.05 to match the live
                           # override set at S218. The default only bites if
                           # iris_config.json is lost or wiped, and at 0.95 that
                           # silently slowed her speech AND left cached quips
                           # sounding slower than her live sentences (S238).
KOKORO_SPEED_QUIPS = 1.1  # slightly faster for wakeword quip cache (S178, was 1.15)

# ── F5-TTS (voice-clone voice DNA on GandalfAI 8005) ──────────────────────────────
# Persistent F5-TTS HTTP server on GandalfAI port 8005 (C:\GPU_BOX\f5tts_server.py,
# IRIS_F5TTS scheduled task). Same /v1/audio/speech contract as Kokoro. When
# F5_ENABLED, synthesize() routes F5 -> Kokoro(8004) -> Piper(10200).
# S160b: DEFAULT REVERTED TO FALSE (Kokoro primary). Vetting found F5 adds
# ~4-12 s time-to-first-audio under concurrent-LLM GPU load vs Kokoro's ~0.2 s
# (docs/S160_f5tts_pipeline_vetting.md). F5 to be re-enabled only after the
# latency/nfe_step tuning + hardening session. Flip True (here or via
# iris_config.json F5_ENABLED) to put the voice-clone live voice back on.
F5_BASE_URL  = "http://192.168.0.20:8005"
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
KIDS_SILENCE_SECS     = 2.0   # S201 (A5): default aligned to the live override (was 3.5). The
                              # measured cap-runs happened AT 2.0s live, so the fix is the leaky
                              # counter below, not this value.
KIDS_SILENCE_RMS      = 150

# S199 T6: adaptive endpoint. The fixed *_SILENCE_RMS floors sat below real-room
# ambient, so the trailing-silence close never fired (every 72h-log recording ran
# to its cap, both modes). The recorder now measures an ambient baseline
# (20th-percentile chunk RMS) and closes relative to it, the same noise-relative
# pattern as the S195 barge-in bleed calibration. *_SILENCE_RMS keeps its role as
# the MINIMUM floor. Full contract: docs/S199_kids_tempo_contract.md
ENDPOINT_BASELINE_MS   = 300    # ambient sampling window informing the estimate
ENDPOINT_SPEECH_MULT   = 3.0    # speech onset: rms > baseline * this (sustained)
ENDPOINT_ONSET_MIN_MS  = 120    # onset must sustain this long to arm the close
ENDPOINT_SILENCE_MULT  = 1.6    # silence floor: max(*_SILENCE_RMS, baseline * this)
ENDPOINT_NOSPEECH_SECS = 4.0    # no onset by now -> early exit (false-wake recovery)
# S201 (A5): the trailing-silence counter used to ZERO on any single chunk above the
# silence floor, so intermittent room noise (sibling/TV/dog/breath) within the
# KIDS_SILENCE_SECS window kept resetting it and the recording ran to cap -- measured
# 53% of 72h recordings hit cap, incl. `10.1s RMS=1816 endpoint=cap` on a quiet room.
# The counter is now LEAKY: a loud chunk costs this many quiet-chunks of progress
# instead of all of it, so a mostly-quiet room still closes. 1 = old hard-reset behavior.
ENDPOINT_SILENCE_DECAY = 4      # quiet-chunks subtracted per above-floor chunk (leaky close)
ENDPOINT_DEBUG         = False  # S201 (A5): one-line-per-recording [REC-DBG] endpoint trace
                                # (baseline/sil_rms/sil_peak/reason), default OFF. Flip on to
                                # diagnose a cap -- intermittent noise (leaky fixes) vs room tone
                                # above the silence floor (needs a floor/mult change). RD-031: bounded.

# Kids mode engagement (S144) -- fill the dead air while IRIS thinks so a
# low-attention child stays engaged. A short pre-cached "thinking" clip plays
# over the LLM/STT gap ONLY when first real audio is genuinely late.
KIDS_GAP_FILLERS      = 1      # 1=enable kids "thinking" gap fillers
KIDS_THINK_FILLER_MS  = 1200   # fire a filler only if first real audio is later than this (ms)
# RD-047: a second, longer-wait filler. The first covers a normal think gap; a
# genuinely slow turn (cold GandalfAI, long reply) leaves 4s+ of silence after
# it, which reads to a child as "she stopped". 0 disables the second stage.
KIDS_THINK_FILLER2_MS = 5000
# RD-047: endpoint-close cue. A short local earcon the instant record_command()
# returns, before STT is even submitted -- turns the longest unlabeled silence
# of the turn into a "got it" confirmation. 0 disables.
KIDS_ENDPOINT_CUE     = 1
# S199 T4: reciprocity + never-silent mode transitions.
KIDS_FOLLOWUP_CUE     = 1   # kids: rising "your turn" earcon + CURIOUS face replaces the double-beep
KIDS_MODE_OFF_SPOKEN  = 1   # kids: speak a cached sign-off when kids mode auto-offs

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
LED_KIDS_FLOOR     = 3      # S201 (A6): was hardcoded 1 in led.py; now matches adult LED_IDLE_FLOOR
LED_KIDS_PERIOD    = 5.0    # S201 (A6): 4.0 -> 5.0 to match adult LED_IDLE_PERIOD cadence (same breathe, different hue)
LED_SLEEP_PEAK     = 8      # indigo breathe sleep max (0-255 color value)
LED_SLEEP_FLOOR    = 1
LED_SLEEP_PERIOD   = 8.0
LED_SLEEP_BRIGHT   = 0xE3   # APA102 global brightness byte: 0xE0|(0-31); 0xE3=3/31≈10%, 0xFF=31/31=max

# ── Interrupt / loud-stop ─────────────────────────────────────────────────────
# RMS threshold for instant stop-playback trigger during TTS.
# Must be calibrated ABOVE speaker bleed at current volume.
# S88 bleed observed at 9k-18k RMS with SPEAKER_VOLUME=117; raised to 25000.
LOUD_STOP_THRESHOLD = 25000

# ── Barge-in AEC (S214 A8) ────────────────────────────────────────────────────
# Software echo cancellation for conversational barge-in. Default OFF: zero
# live-behavior change until the operator flips BARGEIN_AEC_ENABLED. The
# presence tier only means anything once AEC is on AND converged (ERLE gate).
# Design + fail-open contract: docs/S214_A8_design.md.
BARGEIN_AEC_ENABLED      = False
AEC_DETECT_MULT          = 1.5    # Vosk feed floor = cancellation residual x this
AEC_MIN_FLOOR            = 1500   # absolute minimum Vosk feed gate on the clean signal
BARGEIN_PRESENCE_ENABLED = True   # presence-STOP tier (inert until AEC on + converged)
BARGEIN_PRESENCE_MULT    = 2.0    # presence floor = feed floor x this
BARGEIN_PRESENCE_MS      = 700    # sustained near-end speech required to fire STOP
BARGEIN_PRESENCE_KIDS    = False  # kids talk-along protection: presence stays off in kids mode
AEC_DEBUG                = False  # bounded per-second [AEC-DBG] trace

# ── Volume ────────────────────────────────────────────────────────────────────
# The wm8960 "Speaker" control is 0-127 and declares its own curve in the ALSA
# TLV: dBscale-min=-121.00dB, step=1.00dB. So it is ALREADY logarithmic, one dB
# per step, and dB = register - 121. It does not need a log/linear conversion;
# what it needs is for the software to stop treating a dB register as a linear
# percentage. Read off the live card (S240c):
#     register 127 = +6 dB      117 = -4 dB      100 = -21 dB
#              121 =  0 dB      110 = -11 dB      92 = -29 dB
#               88 = -33 dB      73 = -48 dB      60 = -61 dB
# Everything below ~88 is inaudible across a kitchen, which is why the operator
# reported the scale as useless at "around 73" and the usable band as 90-120.
VOL_CONTROL    = "Speaker"
VOL_MIN        = 60
VOL_MAX        = 127
# S240c: bottom of the AUDIBLE band (-33 dB). Spoken percentages map onto
# [VOL_USABLE_MIN, VOL_MAX] instead of [0, VOL_MAX] -- the old linear mapping put
# "set volume to 50 percent" at register 63, which is -58 dB, i.e. silence.
# NOT a clamp: VOL_MIN stays the hard floor so the WebUI slider can still be
# dragged down as a mute, which is how the operator silences her.
VOL_USABLE_MIN = 88
# S240c: 10 -> 3. Each step is one dB on this control, so VOL_STEP=10 made every
# "louder" a 10 dB jump (about a doubling in perceived loudness) and left no way
# to land between 110 and 120. 3 dB is a clearly audible but controllable step.
VOL_STEP       = 3
SPEAKER_VOLUME = 121   # register 121 = 0 dB; overridden by iris_config.json

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
# S240 (RD-065): 3 -> 1. Only governs the session-machine-OFF path (with
# CONVO_SESSION_ENABLED on, CONVO_SESSION_MAX_TURNS rules instead), so this is
# the reciprocity floor the operator falls back to when they use the WebUI
# relief valve. A live iris_config.json override wins over this default.
FOLLOWUP_MAX_TURNS    = 1
CONTEXT_TIMEOUT_SECS  = 300
# S201 (A4): in kids mode, DON'T fully clear conversation history when the context
# watchdog fires at CONTEXT_TIMEOUT_SECS -- keep the last N exchanges so a child who
# wanders off and returns within the 30-min kids window (KIDS_MODE_INACTIVITY_TIMEOUT)
# still gets continuity instead of a cold start. Bounded to respect num_ctx 6144.
# 0 = old behavior (full clear in kids mode too). Adult mode always full-clears. This is
# the LIVE carry-forward only; the recurring-pattern recall injection is Session B's
# kids_profile/kids_recall, folded into the USER turn separately.
KIDS_HISTORY_TURNS    = 6      # exchanges (user+assistant pairs) kept across a kids context-timeout
# Camera-game cadence (S168) -- keep the reciprocal game loop alive across
# guesses without re-waking, and suppress the RPQR quip cascade for a short
# grace window after a game ends so a follow-up wakeword doesn't get a
# non-sequitur quip mid-game.
GAME_FOLLOWUP_TURNS   = 10    # extra follow-up turns kept alive after a game clue
                              # (S210: 5 -> 10; game turns are now LLM-free and a
                              # best-of-3 RPS match + rematch needs the headroom)
GAME_REENTRY_GRACE_S  = 20    # suppress RPQR quips this long after a game ends
# ── Conversation session (S221: S220b plan A+E) ──────────────────────────────
# A: a wakeword inside a live conversation (IRIS spoke within this many seconds)
# suppresses the whole RPQR quip cascade -- short earcon, straight to listen.
# Mirrors the S168 game-grace mechanism. Adult mode only (kids keep their S199
# kid-register wake ack). 0 = off (byte-identical to pre-S221 behavior).
CONVO_REENTRY_GRACE_S = 120
# E: conversation-session state machine -- while enabled, the follow-up window
# opens after EVERY conversational reply (not just implies_followup() hits), so
# IRIS holds the floor by default. Exit: two consecutive silent windows or a
# polite dismissal (spoken wind-down line), or STOP (silent). False =
# byte-identical to the pre-S221 follow-up gate.
CONVO_SESSION_ENABLED   = True
CONVO_SESSION_WINDOW_S  = 7.0  # adult in-session wait after a NON-question reply
                               # (question replies keep FOLLOWUP_TIMEOUT_QUESTION;
                               # kids keep KIDS_FOLLOWUP_TIMEOUT untouched)
CONVO_SESSION_MAX_TURNS = 25   # in-session safety cap (replaces FOLLOWUP_MAX_TURNS
                               # while a session is live; games keep GAME_FOLLOWUP_TURNS)
# ── Trajectory steering (S221 Phase 2: S220b plan C+F) -- SHIPS DARK ─────────
# core/trajectory.py emits a per-turn speed bias (tier router) + a one-line
# steering directive (rides the "(Context, not spoken:)" clause). ALL default
# False: with the flags off nothing is computed and prompts are byte-identical.
# Flip TRAJECTORY_ENABLED only after the judged directive sweep + operator call.
TRAJECTORY_ENABLED         = False
TRAJECTORY_THREADS_ENABLED = False  # H2 2026-07-21: corpus <24h old -> DEGRADE-TO-SEED;
                                    # re-validate threads once conversations.jsonl has
                                    # real history (docs/S221_h2_extractor_validation.md)
TRAJECTORY_DEBUG           = False  # per-turn [TRAJ] journal line (RD-031 gate)
# ── Episodic recall (S224c, RD-051 Phase D) -- SHIPS DARK ────────────────────
# core/recall.py notices a recall question ("how did that story end", "who won"),
# asks the GandalfAI retrieval service for the matching exchange, and folds it
# into the CURRENT USER TURN as a "(Context, not spoken: ...)" clause -- never a
# role:system message (S134/S197). Flag OFF = no network call, no work, prompts
# byte-identical to pre-S224c.
RECALL_ENABLED   = False
# NOT overridable on purpose: this is where household transcripts are requested
# from, and a config file should not be able to repoint it.
#
# PORT MOVED 8006 -> 8021 at RD-057. 8006 is Proxmox VE's own web UI port (which
# is why four Proxmox guests on this LAN probed it within minutes at S231) and it
# is separately claimed by scripts/chatterbox_turbo_server.py in this same repo.
# The corpus receiver on GandalfAI moved with it; both sides must agree or recall
# silently returns nothing -- see scripts/iris_corpus_server.py.
RECALL_URL       = "http://192.168.0.20:8021/search"
# The retrieval namespace. "prod" is the live corpus. A test harness passes its own
# name and gets an isolated index, so a scripted test conversation can never become
# tomorrow's confabulation -- which matters here more than usual, because the bug
# under test IS "her own output came back as evidence" (RD-063).
RECALL_NS        = "prod"
# Shared secret for the corpus endpoint (RD-057). Sent as the X-IRIS-Auth header.
# A missing file means no header is sent, which is exactly the pre-RD-057 behavior;
# the server then decides whether it will serve an unauthenticated caller. Kept out
# of this file on purpose -- a secret in the repo is a published secret.
RECALL_SECRET_FILE = "/home/pi/corpus_secret.txt"
# ── RD-063: the fact/artifact ledger ─────────────────────────────────────────
# OFF = pre-RD-063 behavior exactly: one /search call, the episode clause, the
# S224d miss clause. No /recall call is made and the composed prompt is
# byte-identical (proven by a urlopen tripwire in core/recall.py --selftest).
# ON = ask the ledger FIRST. A confident fact answers from a verbatim span
# somebody actually said; anything else falls through to the episode path, so
# turning this on can only add an answer, never remove one.
RECALL_LEDGER_ENABLED = False
RECALL_LEDGER_URL     = "http://192.168.0.20:8021/recall"
# Facts are embedded on their evidence SPAN, which is short, so the similarity
# distribution is not the episode distribution and one threshold for both would be
# a guess. Separate knob, same starting value, to be measured independently.
RECALL_FACT_MIN_SCORE = 0.70
# Code-decided refusal lines for a miss or a conflict, bypassing the LLM entirely.
# OFF keeps the S224e-measured _NO_MEMORY instruction. See core/recall.py.
RECALL_NO_ANSWER_BANK = False
RECALL_K         = 2      # episodes asked for; the story case wants both halves
# Measured, not guessed. scripts/iris_recall_gate.py scores a fixed query set with
# known answers against known-absent questions. 2026-07-21 run at 0.58: keeps
# 12/12 hits, rejects 4/5 misses. Below this, inject NOTHING -- the miss path
# ("I don't remember that one") is the default.
#
# The gate's own Youden sweep prefers 0.61 (10/12 hits, 5/5 misses, J=0.83 vs
# 0.80). Deliberately NOT taken: Youden weights a false accept and a false reject
# equally, and here they are not equal. The two hits 0.61 discards are both
# drawing questions at 0.5995 and 0.5872 -- the first of which is the operator's
# own motivating question, "what did you think about the drawing". The one miss
# 0.58 lets through is a football score matching a rock-paper-scissors score,
# which the clause's honesty instruction is built to absorb ("use this only if it
# actually answers what was asked"). A soft-landed false accept beats a flat "I
# don't remember" on a question she can demonstrably answer.
RECALL_MIN_SCORE = 0.58
# num_ctx is LLM_NUM_CTX (S226: 12288, was 6144) and a camera frame alone is
# ~4570 tokens. 700 chars is ~175
# tokens, on top of a stamp+history text turn. Vision turns cannot reach this code
# at all -- ask_vision()/ask_vision_game() build their own prompts and never call
# _build_messages() -- so the camera case has no path to overflow.
# S224d raised this from 700. At 700 with k=2 the clause was truncated mid-word
# inside the FIRST episode, so "how did that unicorn story end" got the story's
# setup and none of its ending, and she invented one. Episodes are now admitted
# whole or dropped whole (core/recall.py), and the budget fits both halves of a
# two-part story. 1400 chars is ~350 tokens, on a text turn that never carries
# an image, and it is inside the S226 token budget.
RECALL_MAX_CHARS = 1400
# When a recall question retrieves nothing, TELL her so instead of injecting
# nothing. Silence is not neutral: the S224d voice bench measured her inventing on
# every ungrounded prompt, including a confident "Yes." to a hotel she had never
# been told about. False = the pre-S224d behavior (bare question, no clause).
RECALL_MISS_CLAUSE = True
RECALL_TIMEOUT_S = 2.0    # a memory is never worth stalling her reply for
RECALL_DEBUG     = False  # per-turn [RECALL] journal line (RD-031 gate)
# ── RPS tempo (S216) ─────────────────────────────────────────────────────────
# Measured on the live 2026-07-20 match: ~21 s round-to-round for ~1 s of play.
# Breakdown per round -- countdown 3.5 s, capture 1.1 s, classify+filler 1.7 s,
# result line 5.1 s, waiting for the player to say "again" ~5 s. The two levers
# below attack the two ends of the operator's report: she talks too fast, the
# game moves too slow.
GAME_COUNTDOWN_SPEED  = 0.90  # Kokoro speed for the RPS countdown ONLY (normal
                              # speech stays KOKORO_SPEED). S218: raised from
                              # 0.80 because the rhythm is no longer made of
                              # stretched punctuation -- RPS_BEAT_PERIOD_S owns
                              # the tempo now, so this only has to keep the spoken
                              # coaching deliberate. At 0.80 against a 1.05 base
                              # the lead-in ran ~24% slower than her normal
                              # voice, which is a lot of sludge for a sentence
                              # a child hears every single match.
RPS_BEAT_PERIOD_S     = 1.00  # S218: seconds from the START of one throw beat
                              # to the start of the next ("Rock." / "Paper." /
                              # "Scissors." / "Shoot!"). THE tempo knob, and an
                              # ear call: raise for a slower throw, lower for a
                              # snappier one. It is a PERIOD, not a gap, because
                              # onset spacing is what the ear reads as rhythm --
                              # measured on the Pi at speed 0.90 the trimmed
                              # beats run 0.72/0.87/0.85/0.74 s, so a constant
                              # gap would space the onsets 1.07/1.22/1.20 s and
                              # come out audibly uneven, which is exactly the
                              # defect S216 shipped. Silence is inserted per
                              # beat to make up the difference. Keep it above
                              # the longest beat (~0.87 s here) or the throw
                              # falls back to a 0.12 s floor and warns.
RPS_AUTO_CONTINUE     = True  # roll straight into the next round instead of
                              # asking "Again?" and burning a full listen +
                              # endpoint cycle every round. A barge-in "stop"
                              # still ends the match at any point. Set False to
                              # restore the S210 ask-every-round turn-taking.
RPS_MAX_ROUNDS        = 9     # safety bound on one auto-continued match: ties
                              # score nothing, so a best-of-2 can run long.
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
NUM_PREDICT_MEDIUM    = 224   # normal conversational reply            (~52 s)  (S221: was 160 -- headroom for the plan-E conversational length lean; S134: was 90)
NUM_PREDICT_LONG      = 340   # detailed-but-still-chat answers        (~78 s)  (S134: was 180)
NUM_PREDICT_MAX       = 900   # story tier ONLY -- explicit requests   (~3.5 min) (S217: was 640 -- stories were guillotined mid-arc; sized to match TTS_MAX_CHARS=4000 so token-budget and char-cap endings land together. Longer stories chunk into parts via the S217 "keep going" bridge in assistant.py)
# S217 story resume: after a truncated/interrupted story, the context watchdog
# keeps the last 2 exchanges across the CONTEXT_TIMEOUT_SECS clear for this many
# seconds, so "keep telling the story" still has the story in history. 0 = off
# (pre-S217 behavior: adult history fully cleared at 5 min idle).
STORY_RESUME_WINDOW_SECS = 1800
# ── Context budget (S226) ─────────────────────────────────────────────────────
# num_ctx bounds prompt AND generation TOGETHER, and only one half of that is
# enforced anywhere: Ollama trims the oldest messages until prompt <= num_ctx,
# then hands whatever is left to the reply. Nothing reserves room to speak.
# Measured 2026-07-22 against the deployed model: a ten-exchange history of
# 224-token replies is a 6,380-token prompt, which at num_ctx 6144 was silently
# truncated to 6,136 and left eval_tokens=8. That is the live evening failure --
# prompt_tokens=6141, "Well, here", "Night."
# LLM_PERSONA_TOKENS is the SYSTEM block under the model's own tokenizer
# (/api/generate raw=true), not an estimate. RE-MEASURE BOTH whenever
# ollama/iris_modelfile.txt changes -- scripts/s226_ctx_bench.py.
LLM_NUM_CTX           = 12288 # MUST match PARAMETER num_ctx in ollama/iris_modelfile.txt
                              # (S226: was 6144). Costs +522 MiB of q8_0 KV cache.
LLM_PERSONA_TOKENS    = 3921  # measured S226; 15,890-char SYSTEM block
LLM_CTX_SAFETY_TOKENS = 256   # slack for the stamp, the recall/tuning clauses,
                              # and the char-based estimator's error
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
TTS_MAX_CHARS         = 4000  # ~4.4 min hard ceiling, all tiers (S217: was 2400 -- raised with NUM_PREDICT_MAX=900 so the story tier finishes a part at a sentence boundary; S134 was 1500, S117 was 2500)
CONVERSATION_LOG      = "/home/pi/logs/conversations.jsonl"
BENCH_LOG             = "/home/pi/logs/iris_bench.jsonl"
SD_BENCH_LOG          = "/media/root-ro/home/pi/logs/iris_bench.jsonl"

# ── Camera / Vision ───────────────────────────────────────────────────────────
CAMERA_ENABLED = True
CAMERA_WIDTH   = 1024
CAMERA_HEIGHT  = 768
CAMERA_TIMEOUT = 5000   # only used when CAMERA_AUTOFOCUS is off (--immediate path)
# ── Camera autofocus (S216) ──────────────────────────────────────────────────
# The camera is an IMX708 (Raspberry Pi Camera Module 3) and HAS autofocus --
# `rpicam-still --list-cameras` confirms the sensor, and --autofocus-mode /
# --autofocus-range are supported. The pre-S216 capture passed --immediate,
# which captures instantly WITHOUT running an AF cycle, so the lens stayed
# parked wherever it last sat (typically room distance). Live proof: in the
# S216 labeled RPS frames the background was sharp enough to read product text
# while the player's own hand, held near the camera as our countdown told them
# to, was pure blur. That is the dominant cause of RPS hand misreads (production
# prompt scored 2/9 on those frames, below the 1/3 you would get guessing) and
# it degrades every other vision answer too.
# Cost measured on the Pi: capture goes 0.63s -> 2.33s (+1.7s). Affordable --
# the cached "Let's seeee..." filler already covers the post-SHOOT beat.
CAMERA_AUTOFOCUS  = True     # run an AF cycle before each capture
CAMERA_AF_RANGE   = "macro"  # macro | normal | full -- macro favours near subjects
                             # (a hand held up for a game); "normal" if room-scale
                             # describe-what-you-see ever regresses
CAMERA_AF_TIMEOUT = 2000     # ms given to the AF sweep; this replaces CAMERA_TIMEOUT
                             # as -t on the AF path. Do NOT reuse CAMERA_TIMEOUT
                             # (5000) here -- without --immediate, -t is a real
                             # wait and every capture would cost 5s.
# NOT a separate vision model: this is the SAME persona model as
# OLLAMA_MODEL_ADULT. base: mistral-small3.2:24b, multimodal (Pixtral vision
# baked in), so image queries keep IRIS's persona and cost no model swap. A
# dedicated VLM is not an option here: iris occupies ~20.3 of the 3090's 24 GB
# when loaded, so nothing else co-resides without evicting her (measured S216).
VISION_MODEL   = "iris"   # base: mistral-small3.2:24b (vision-capable)
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

# ── Weather (RD-068) ─────────────────────────────────────────────────────────
# She had no weather input at all and invented one, in celsius. services/weather.py
# fetches the real conditions from Open-Meteo (no key, no account) when the router
# sees a weather-shaped utterance, and core/prompt.py folds them into the context
# stamp so she answers in her own voice rather than from a canned string.
#
# LOCATION IS THE WHOLE FEATURE. A wrong one makes this worse than nothing, so it
# lives here as a WebUI knob and never needs a redeploy to change. The default is
# the household's: ZIP 84037, Kaysville, Utah, resolved S243 against two
# independent sources (zippopotam.us and Open-Meteo's own geocoder, which lists
# postcode 84037 for this point).
WEATHER_ENABLED    = True
WEATHER_LAT        = 41.0352
WEATHER_LON        = -111.9386
WEATHER_TZ         = "America/Denver"
# S243e, measured not guessed: handed a temperature with no place attached, she
# supplies one. Over 18 production-composed samples she answered "It's July in
# Colorado." Nothing anywhere in the prompt says where the house is -- the
# HOUSEHOLD block names people and dogs and no location -- so the gap gets
# filled. Naming the place in the clause is the narrow fix; it travels with the
# reading it belongs to instead of becoming a standing fact she volunteers.
WEATHER_PLACE      = "Kaysville, Utah"
# The API's own `current` block refreshes on a 900 s interval, so asking more
# often than that buys nothing. 600 s keeps a follow-up question inside one fetch.
# S243e, and this one is a safety flag rather than a taste knob. With it Off, a
# failed fetch injects nothing -- and nothing is not a signal: measured, she
# invented a temperature in 18 of 18 samples against an actual 100. Same finding,
# same fix, same wording as RECALL_MISS_CLAUSE below. Leave it On.
WEATHER_MISS_CLAUSE = True
WEATHER_CACHE_SECS = 600
# This fetch sits on the speech critical path, before the LLM call. Measured 0.70 s
# from the Pi4 at S242; 3 s is headroom without stalling a reply. Same principle as
# RECALL_TIMEOUT_S -- the weather is never worth making her pause for.
WEATHER_TIMEOUT_S  = 3.0

# ── Sleep window ─────────────────────────────────────────────────────────────
SLEEP_WINDOW_START_HOUR = 21  # 9 PM
SLEEP_WINDOW_END_HOUR   = 7   # 7 AM -- RD-047 (S197): was 8. School breakfast is
                              # 07:00-07:45; at 8 a 7:15am "hey Iris" hit the sleep
                              # branch, got an inviting wake quip, and was ignored.
                              # Flat, same every day (operator decision). The wake
                              # cron moved 0 8 -> 0 7 to match.
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

# ── Quiet break (voice "do not disturb") ─────────────────────────────────────
# S202: "hey iris, take a break / bugger off / shut the front door" puts IRIS
# into a timed quiet break -- a local REFLEX (never touches the LLM). For
# BREAK_DURATION_SECS every wakeword IS IGNORED (record_command's mic feed to
# OpenWakeWord is simply not run, so OWW sees no audio and can't fire) and the
# face + APA LEDs sleep. A PTT button press is the manual early-cancel; the WebUI
# can cancel via the BREAK:CANCEL UDP command.
#
# When the window expires IRIS PROACTIVELY wakes, plays a couple of quips + a
# reciprocal "shall I get back to it?" question, then pulses the APA LEDs RED
# (mic active) and listens offline (Vosk yes/no grammar -- GandalfAI is normally
# re-asleep by now, so this must not depend on the network) for up to
# BREAK_CONFIRM_TIMEOUT_SECS:
#   * yes / no answer / silence -> resume normal awake functioning ("silence ->
#     resume" is the spec default),
#   * explicit "no / not yet / keep resting" -> re-arm another full break window.
# All spoken lines are pre-cached PCM (soundboard categories break_ack /
# break_resume / break_resume_ask), so they play with GandalfAI asleep -- same
# offline pattern as sleep_window. A break reuses _do_sleep() for the sleep face,
# then overrides the LEDs with the distinct amber break breathe (leds.show_break()).
# BREAK_DURATION_SECS + BREAK_CONFIRM_TIMEOUT_SECS are WebUI-tunable (bounds below).
BREAK_DURATION_SECS = 1200          # 20 minutes of enforced quiet
BREAK_CONFIRM_TIMEOUT_SECS = 8.0    # listen window for the resume yes/no answer
BREAK_TRIGGERS = {
    "take a break", "bugger off", "shut the front door",
    # S240 (RD-065): the operator said "go to take a nap" during the 2026-07-25
    # ear-test and got a conversational reply. Matching stays start-anchored
    # (core/intent_router._starts_phrase), so this catches "take a nap" and
    # "take a nap please" but NOT the phrase buried mid-utterance -- see the
    # S240 note in docs/iris_issue_log.md for the transcript that still misses.
    "take a nap",
}
# Offline Vosk grammar for the resume confirmation. Kept small so the recognizer
# is fast + robust; every word must exist in the vosk-model-small lexicon.
BREAK_YES_WORDS = {
    "yes", "yeah", "yep", "yup", "sure", "okay", "ok", "resume",
    "back", "please", "go", "ready", "wake",
}
BREAK_NO_WORDS = {
    "no", "nope", "not", "yet", "still", "rest", "resting",
    "later", "off", "quiet", "stay",
}
# Fallback ack text (the spoken ack normally comes from the break_ack soundboard
# category; this is only used if that category is empty and a live synth is done).
BREAK_SPOKEN = "Right, I'll make myself scarce for twenty minutes."

# ── WoL / GandalfAI ───────────────────────────────────────────────────────────
GANDALF_MAC      = "00:11:22:33:44:55"
# S222: was "192.168.0.20", GandalfAI's own unicast IP. Sending a magic packet to
# a unicast address requires an ARP resolution, and a SLEEPING machine does not
# answer ARP -- so once the Pi4's ARP entry aged out the wake failed exactly when
# it was needed, which is every time she actually had to wake him. Broadcast
# needs no ARP. This is why the operator's WakeMeOnLan (which broadcasts against
# a cached MAC) wakes him every time while IRIS's own wake was unreliable.
GANDALF_WOL_IP   = "192.168.1.255"
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
VALID_EMOTIONS = {"NEUTRAL", "HAPPY", "CURIOUS", "ANGRY", "SLEEPY", "SURPRISED", "SAD", "CONFUSED", "AMUSED",
                  "ANNOYED", "EXASPERATED"}
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
    # S241 wit registers. Both REUSE an existing baked sprite on purpose -- new
    # mouth art is a separate task and these tags carry their meaning in the
    # eyelids, not the mouth.
    "ANNOYED":     2,  # smirk: the jab is playful, so the mouth must not read ANGRY
    "EXASPERATED": 0,  # deadpan flat line; the eye-roll is the whole expression
}

# Highest EYE:n the firmware will accept, i.e. EYE_IDX_COUNT-1 in src/main.cpp.
# S241 wired the vendored library and took this from 7 entries to 25; the Pi-side
# validators below were NOT widened with it, so an emotion mapped to any of the 18
# new eyes was silently rejected on load. Keep this in step with EYE_IDX_COUNT.
EYE_IDX_MAX = 24

# Eye index override per emotion. -1 = no override (use userDefaultEye on Teensy).
# A value 0..EYE_IDX_MAX sends EYE:n before EMOTION:x, making emit_emotion() set
# the eye style per emotion. ANGRY/CONFUSED still trigger firmware eye swap;
# the value here controls the revert-to eye after the firmware timer expires.
#
# S242/RD-066 left these at -1 DELIBERATELY, which is a decision and not an
# omission. The operator's ask was "the eye change needs to be more common", and
# S241's lid choreographer already answers it: every emotion except NEUTRAL now
# drives a lid script, so raising the rate of non-NEUTRAL tags in the persona --
# which S242 did -- animates the face on its own, with no texture swap at all.
# Which TEXTURE means which emotion is explicitly reserved for the operator's
# taste pass (src/main.cpp:100-104), and the 18 new eyes are not flashed yet, so
# picking them here would be inventing taste against unflashed art. Set them from
# the WebUI Emotion Display card once the flash lands.
EMOTION_EYE_MAP = {e: -1 for e in VALID_EMOTIONS}

EMOTION_TAG_RE = re.compile(r'^\[EMOTION:([A-Z]+)\]\s*', re.IGNORECASE)

# Un-anchored variant: catches stray [EMOTION:X] tags the model emits mid-reply
# (EMOTION_TAG_RE only extracts the leading one) so they never reach TTS. (S175)
EMOTION_TAG_ANY_RE = re.compile(r'\[EMOTION:[A-Z]+\]\s*', re.IGNORECASE)

# ── iris_config.json loader (web UI overrides) ────────────────────────────────
_OVERRIDABLE = {
    "RECORD_SECONDS", "SILENCE_SECS", "SILENCE_RMS",
    "KIDS_RECORD_SECONDS", "KIDS_SILENCE_SECS", "KIDS_SILENCE_RMS",
    "ENDPOINT_BASELINE_MS", "ENDPOINT_SPEECH_MULT", "ENDPOINT_ONSET_MIN_MS",
    "ENDPOINT_SILENCE_MULT", "ENDPOINT_NOSPEECH_SECS", "ENDPOINT_SILENCE_DECAY", "ENDPOINT_DEBUG",
    "KIDS_GAP_FILLERS", "KIDS_THINK_FILLER_MS", "KIDS_THINK_FILLER2_MS", "KIDS_ENDPOINT_CUE",
    "KIDS_FOLLOWUP_CUE", "KIDS_MODE_OFF_SPOKEN",
    "GESTURE_AUDIO_CUE", "GESTURE_MOUTH_CUE",
    "OWW_THRESHOLD", "OWW_TRIGGER_LEVEL", "OWW_POST_PLAY_DRAIN_SECS", "FOLLOWUP_TIMEOUT", "FOLLOWUP_TIMEOUT_QUESTION", "SILENCE_SECS_FOLLOWUP", "KIDS_FOLLOWUP_TIMEOUT", "KIDS_MODE_INACTIVITY_TIMEOUT",
    "VISION_TIMEOUT", "SLEEP_DOUBLE_WAKE_WINDOW_S",
    "BREAK_DURATION_SECS", "BREAK_CONFIRM_TIMEOUT_SECS",
    "FOLLOWUP_MAX_TURNS", "GAME_FOLLOWUP_TURNS", "GAME_REENTRY_GRACE_S",
    "CONVO_REENTRY_GRACE_S", "CONVO_SESSION_ENABLED", "CONVO_SESSION_WINDOW_S", "CONVO_SESSION_MAX_TURNS",
    "TRAJECTORY_ENABLED", "TRAJECTORY_THREADS_ENABLED", "TRAJECTORY_DEBUG",
    "RECALL_ENABLED", "RECALL_K", "RECALL_MIN_SCORE", "RECALL_MAX_CHARS",
    "RECALL_TIMEOUT_S", "RECALL_DEBUG", "RECALL_MISS_CLAUSE",
    "RECALL_LEDGER_ENABLED", "RECALL_FACT_MIN_SCORE", "RECALL_NO_ANSWER_BANK",
    "WEATHER_ENABLED", "WEATHER_LAT", "WEATHER_LON", "WEATHER_TZ",
    "WEATHER_PLACE", "WEATHER_MISS_CLAUSE", "WEATHER_CACHE_SECS", "WEATHER_TIMEOUT_S",
    "GAME_COUNTDOWN_SPEED", "RPS_AUTO_CONTINUE", "RPS_MAX_ROUNDS", "RPS_BEAT_PERIOD_S",
    "CAMERA_AUTOFOCUS", "CAMERA_AF_RANGE", "CAMERA_AF_TIMEOUT", "CONTEXT_TIMEOUT_SECS", "KIDS_HISTORY_TURNS", "NUM_PREDICT", "NUM_PREDICT_SHORT", "NUM_PREDICT_MEDIUM", "NUM_PREDICT_LONG", "NUM_PREDICT_MAX", "TTS_MAX_CHARS", "STORY_RESUME_WINDOW_SECS",
    "LOUD_STOP_THRESHOLD", "DEFAULT_EYE_IDX",
    "BARGEIN_AEC_ENABLED", "AEC_DETECT_MULT", "AEC_MIN_FLOOR",
    "BARGEIN_PRESENCE_ENABLED", "BARGEIN_PRESENCE_MULT", "BARGEIN_PRESENCE_MS",
    "BARGEIN_PRESENCE_KIDS", "AEC_DEBUG",
    "CHATTERBOX_VOICE", "CHATTERBOX_EXAGGERATION", "CHATTERBOX_ENABLED",
    "KOKORO_VOICE", "KOKORO_ENABLED", "KOKORO_SPEED", "KOKORO_SPEED_QUIPS",
    "F5_ENABLED",
    "VOL_MAX", "VOL_STEP", "VOL_USABLE_MIN", "SPEAKER_VOLUME", "OLLAMA_MODEL_ADULT", "OLLAMA_MODEL_KIDS",
    "PERSONA_TONE_ADULT", "PERSONA_TONE_KIDS", "PERSONA_ENGAGE",
    "LED_IDLE_PEAK", "LED_IDLE_FLOOR", "LED_IDLE_PERIOD",
    "LED_KIDS_PEAK", "LED_KIDS_FLOOR", "LED_KIDS_PERIOD",
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
    "BARGEIN_AEC_ENABLED":     (bool,  None),         # S214 (A8): master AEC flag, default OFF
    "AEC_DETECT_MULT":         (float, (1.0, 5.0)),
    "AEC_MIN_FLOOR":           (int,   (200, 20000)),
    "BARGEIN_PRESENCE_ENABLED":(bool,  None),
    "BARGEIN_PRESENCE_MULT":   (float, (1.0, 10.0)),
    "BARGEIN_PRESENCE_MS":     (int,   (100, 5000)),
    "BARGEIN_PRESENCE_KIDS":   (bool,  None),
    "AEC_DEBUG":               (bool,  None),
    "RECALL_ENABLED":          (bool,  None),        # S224c: master recall flag, default OFF
    "RECALL_K":                (int,   (1, 5)),      # server caps at 5 as well
    "RECALL_MIN_SCORE":        (float, (0.0, 1.0)),
    "RECALL_MAX_CHARS":        (int,   (100, 2500)),
    "RECALL_TIMEOUT_S":        (float, (0.2, 10.0)),
    "RECALL_DEBUG":            (bool,  None),
    "RECALL_MISS_CLAUSE":      (bool,  None),   # S224d: tell her when nothing was found
    "RECALL_LEDGER_ENABLED":   (bool,  None),   # RD-063: fact-first lookup, default OFF
    "RECALL_FACT_MIN_SCORE":   (float, (0.0, 1.0)),
    "RECALL_NO_ANSWER_BANK":   (bool,  None),   # RD-063 P3: code-decided refusal lines
    "WEATHER_ENABLED":         (bool,  None),   # RD-068: master weather flag, default ON
    "WEATHER_MISS_CLAUSE":     (bool,  None),   # S243e: tell her the feed came back empty
    "WEATHER_LAT":             (float, (-90.0, 90.0)),
    "WEATHER_LON":             (float, (-180.0, 180.0)),
    "WEATHER_CACHE_SECS":      (int,   (60, 3600)),
    "WEATHER_TIMEOUT_S":       (float, (0.5, 10.0)),
    # WEATHER_TZ is an IANA name ("America/Denver") -- a string key, so it is
    # deliberately absent here and passes through _coerce_value untouched.
    "DEFAULT_EYE_IDX":         (int,   (0, 6)),
    "RECORD_SECONDS":          (int,   (1, 60)),
    "SILENCE_SECS":            (float, (0.1, 10.0)),
    "SILENCE_RMS":             (int,   (50, 5000)),
    "KIDS_RECORD_SECONDS":     (int,   (1, 60)),
    "KIDS_SILENCE_SECS":       (float, (0.1, 15.0)),
    "KIDS_SILENCE_RMS":        (int,   (50, 5000)),
    "ENDPOINT_BASELINE_MS":    (int,   (100, 2000)),
    "ENDPOINT_SPEECH_MULT":    (float, (1.5, 10.0)),
    "ENDPOINT_ONSET_MIN_MS":   (int,   (60, 1000)),
    "ENDPOINT_SILENCE_MULT":   (float, (1.0, 5.0)),
    "ENDPOINT_NOSPEECH_SECS":  (float, (1.0, 15.0)),
    "ENDPOINT_SILENCE_DECAY":  (int,   (1, 50)),     # S201 (A5): leaky trailing-silence decay
    "ENDPOINT_DEBUG":          (bool,  None),        # S201 (A5): [REC-DBG] endpoint trace toggle
    "KIDS_HISTORY_TURNS":      (int,   (0, 20)),     # S201 (A4): exchanges kept across kids context-timeout
    "KIDS_GAP_FILLERS":        (bool,  None),
    "KIDS_THINK_FILLER_MS":    (int,   (300, 5000)),
    "KIDS_THINK_FILLER2_MS":   (int,   (0, 20000)),   # 0 = disable second stage
    "KIDS_ENDPOINT_CUE":       (bool,  None),
    "KIDS_FOLLOWUP_CUE":       (bool,  None),
    "KIDS_MODE_OFF_SPOKEN":    (bool,  None),
    "GESTURE_AUDIO_CUE":       (bool,  None),
    "GESTURE_MOUTH_CUE":       (bool,  None),
    "OWW_THRESHOLD":           (float, (0.1, 1.0)),
    "OWW_TRIGGER_LEVEL":       (int,   (1, 5)),
    "FOLLOWUP_TIMEOUT":        (int,   (1, 60)),
    "VISION_TIMEOUT":          (int,   (5, 180)),   # S194 Rung5: vision POST budget
    "SLEEP_DOUBLE_WAKE_WINDOW_S":      (int,   (2, 60)),   # S194: double-wake sleep break-through
    "BREAK_DURATION_SECS":             (int,   (10, 7200)),   # S202: quiet-break window (10 = test, 1200 = live)
    "BREAK_CONFIRM_TIMEOUT_SECS":      (float, (2.0, 30.0)),  # S202: resume yes/no listen window
    "FOLLOWUP_TIMEOUT_QUESTION":       (float, (1.0, 30.0)),   # S194 Rung3: wait after IRIS asks a question
    "SILENCE_SECS_FOLLOWUP":          (float, (0.1, 10.0)),   # S194 Rung3: endpoint silence during that window
    "KIDS_FOLLOWUP_TIMEOUT":          (int,   (1, 120)),
    "KIDS_MODE_INACTIVITY_TIMEOUT":   (int,   (60, 7200)),
    "FOLLOWUP_MAX_TURNS":      (int,   (1, 20)),
    "CONVO_REENTRY_GRACE_S":   (int,   (0, 3600)),    # S221 (plan A): conversation re-wake grace; 0 = off
    "CONVO_SESSION_ENABLED":   (bool,  None),         # S221 (plan E): conversation-session master flag
    "CONVO_SESSION_WINDOW_S":  (float, (2.0, 30.0)),  # S221 (plan E): in-session non-question wait
    "CONVO_SESSION_MAX_TURNS": (int,   (1, 100)),     # S221 (plan E): in-session safety cap
    "TRAJECTORY_ENABLED":         (bool, None),       # S221 Phase 2 (dark): trajectory master flag
    "TRAJECTORY_THREADS_ENABLED": (bool, None),       # S221 Phase 2 (dark): thread callbacks (H2-gated)
    "TRAJECTORY_DEBUG":           (bool, None),       # S221 Phase 2: [TRAJ] journal line
    "CONTEXT_TIMEOUT_SECS":    (int,   (30, 3600)),
    "NUM_PREDICT":             (int,   (10, 2000)),
    "NUM_PREDICT_SHORT":       (int,   (10, 2000)),
    "NUM_PREDICT_MEDIUM":      (int,   (10, 2000)),
    "NUM_PREDICT_LONG":        (int,   (10, 2000)),
    "NUM_PREDICT_MAX":         (int,   (10, 2000)),
    "TTS_MAX_CHARS":           (int,   (100, 4000)),
    "STORY_RESUME_WINDOW_SECS": (int,  (0, 7200)),   # S217: story carry across context clear; 0 = off
    "PERSONA_TONE_ADULT":      (float, (0.0, 1.0)),   # S215 personality continuum
    "PERSONA_TONE_KIDS":       (float, (0.0, 1.0)),
    "PERSONA_ENGAGE":          (float, (0.0, 1.0)),
    "CHATTERBOX_EXAGGERATION": (float, (0.0, 2.0)),
    "CHATTERBOX_ENABLED":      (bool,  None),
    "KOKORO_ENABLED":          (bool,  None),
    "F5_ENABLED":              (bool,  None),
    "KOKORO_SPEED":            (float, (0.5, 2.0)),
    "KOKORO_SPEED_QUIPS":      (float, (0.5, 2.0)),
    "GAME_COUNTDOWN_SPEED":    (float, (0.5, 2.0)),   # S216: RPS throw rhythm
    "RPS_AUTO_CONTINUE":       (bool,  None),         # S216: no "Again?" per round
    "RPS_MAX_ROUNDS":          (int,   (1, 20)),      # S216: auto-continue bound
    "RPS_BEAT_PERIOD_S":       (float, (0.4, 3.0)),   # S218: throw beat onset period
    "CAMERA_AUTOFOCUS":        (bool,  None),         # S216: AF cycle before capture
    "CAMERA_AF_TIMEOUT":       (int,   (500, 6000)),  # S216: ms for the AF sweep
    # CAMERA_AF_RANGE is a string key -- passed through, see the note above.
    "VOL_MAX":                 (int,   (60, 127)),
    "SPEAKER_VOLUME":          (int,   (60, 127)),
    "VOL_STEP":                (int,   (1, 20)),      # S240c: dB per louder/quieter step
    "VOL_USABLE_MIN":          (int,   (60, 120)),    # S240c: bottom of the audible band

    "LED_IDLE_PEAK":           (int,   (0, 255)),
    "LED_IDLE_FLOOR":          (int,   (0, 255)),
    "LED_IDLE_PERIOD":         (float, (0.5, 30.0)),
    "LED_KIDS_PEAK":           (int,   (0, 255)),
    "LED_KIDS_FLOOR":          (int,   (0, 255)),
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
                if _e in VALID_EMOTIONS and isinstance(_idx, int) and -1 <= _idx <= EYE_IDX_MAX:
                    EMOTION_EYE_MAP[_e] = _idx
    except FileNotFoundError:
        print(f"[CFG]  iris_config.json not found, using defaults", flush=True)
    except _json.JSONDecodeError as _e:
        print(f"[CFG]  iris_config.json parse error: {_e} -- using defaults", flush=True)
    except Exception as _e:
        print(f"[CFG]  iris_config.json load failed: {_e} -- using defaults", flush=True)


reload_overrides()
