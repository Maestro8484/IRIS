"""
core/soundboard.py — Data-driven source of truth for IRIS quips + clips.

A single JSON file (/home/pi/iris_soundboard.json, SD-mirrored) holds every
editable quip line and every clip<->trigger/affect binding. assistant.py and
core/clip_triggers.py read it at runtime, so adding / editing / enabling /
reassigning a quip or clip becomes a pure WebUI/data action — no code edit, no
redeploy. This is the strangler module that replaces the S162e hardcoded
disable of the clip trigger map.

Seeded on first load from DEFAULT (verbatim from the prior hardcoded values), so
the first deploy reproduces existing behavior EXACTLY:
  * every clip seeds enabled=False  -> matches the S162e empty trigger map
    (check_clip_trigger returns None for all utterances until the operator
    enables a clip in the WebUI),
  * every quip category seeds enabled=True with its original lines/emotions.

Write path mirrors the S158 clip-upload atomic dual-write:
  tmp -> md5 -> os.replace() into the RAM file, then ONE
  `sudo bash -c "remount,rw ... cp ... sync ... remount,ro"` to the SD overlay,
  with md5 RAM==SD verification. It never touches the api_persist_config mount
  sequence (S152 / RD-034).
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import threading

DATA_FILE    = "/home/pi/iris_soundboard.json"
SD_DATA_FILE = "/media/root-ro/home/pi/iris_soundboard.json"

VALID_EMOTIONS = {
    "NEUTRAL", "HAPPY", "CURIOUS", "ANGRY", "SLEEPY",
    "SURPRISED", "SAD", "CONFUSED", "AMUSED",
}
# Gesture-cue keys are fixed by the base-mount protocol; only their spoken text
# is editable.
GESTURE_CUE_KEYS = ("VOL+", "VOL-", "MUTE", "UNMUTE", "STOP", "LISTEN")

SCHEMA_VERSION = 1

# Resource caps — prevent unbounded synthesis cost (RD-031)
_MAX_CLIPS       = 150   # max clips in the data model
_MAX_LINES       = 40    # max quip lines per category / wake band
_MAX_LINE_CHARS  = 200   # max chars per quip line (trigger keywords not capped)
_MAX_TOTAL_LINES = 300   # max quip lines across ALL categories (synth-cost guard)

_GOLDBAK_FILE    = DATA_FILE    + ".goldbak"
_SD_GOLDBAK_FILE = SD_DATA_FILE + ".goldbak"


def _c(file, triggers=None, affect=None, desc="", enabled=False):
    """Compact clip-seed builder. Defaults enabled=False to match live state."""
    return {
        "file":     file,
        "enabled":  enabled,
        "triggers": list(triggers or []),
        "affect":   list(affect or []),
        "desc":     desc,
    }


# ── DEFAULT seed ──────────────────────────────────────────────────────────────
# CLIPS: 6 Bluey "Bandit" (S156) + 80 voice-clone iris_clip_* (S159). All seed
# enabled=False so first deploy fires no clip (identical to the S162e interim).
# Triggers / affect verbatim from the prior clip_triggers maps; desc carries the
# spoken text (voice-clone) so the WebUI can show what each clip says.
_DEFAULT_CLIPS = [
    # ── Bluey "Bandit" 6 (S156) ──
    _c("Bonjour.wav",
       ["bonjour", "hello", "hi ", "hey ", "howdy", "greetings",
        "good morning", "good afternoon", "good evening"],
       ["NEUTRAL", "ANGRY", "SLEEPY", "SAD", "CONFUSED"],
       "Bandit: deadpan 'Bonjour' greeting"),
    _c("BonjourBonjooour.wav",
       ["bonjour", "hello", "hi ", "hey ", "howdy", "greetings",
        "good morning", "good afternoon", "good evening",
        "fancy", "pretentious", "gourmet", "cuisine", "highbrow",
        "sophisticated", "bourgeois", "brie", "charcuterie",
        "hors d'oeuvre", "sommelier", "soiree", "soirée"],
       ["AMUSED", "HAPPY", "CURIOUS", "SURPRISED"],
       "Bandit: exuberant fancy 'Bonjooour' greeting"),
    _c("Discotech.wav",
       ["disco", "discotheque", "dancing", "dance floor", "dance party",
        "nightclub", "night club", "dj set"], [],
       "Bandit: 'Discotech'"),
    _c("HomeSweetLonelyHome.wav",
       ["home sweet home", "home alone", "lonely home"], [],
       "Bandit: 'Home sweet lonely home'"),
    _c("MagicClaw.wav",
       ["magic claw", "the claw", "crane game"], [],
       "Bandit: 'The magic claw'"),
    _c("wheremyPassport.wav",
       ["where's my passport", "where is my passport", "lost my passport",
        "need my passport", "find my passport"], [],
       "Bandit: 'Where's my passport'"),

    # ── voice-clone iris_clip_001-080 (S159) ──
    # Group 1: Acknowledgments / Greetings
    _c("iris_clip_001.wav", ["are you there", "you there", "anyone there", "you awake", "hello? are you"], ["NEUTRAL", "SLEEPY"], "Yes, I'm here. Tragically."),
    _c("iris_clip_002.wav", ["go ahead", "ready for me", "can i talk now"], ["NEUTRAL", "SLEEPY"], "Go ahead. I'm listening. Reluctantly, but listening."),
    _c("iris_clip_003.wav", [], ["NEUTRAL"], "You have my attention. Use it wisely."),
    _c("iris_clip_004.wav", [], ["NEUTRAL"], "Good morning. I've already assessed the day. It's average."),
    _c("iris_clip_005.wav", ["i'm back", "i'm home", "i'm here again", "back again", "i just got back"], [], "Oh good, you're back."),
    # Group 2: Dismissals / Snark
    _c("iris_clip_006.wav", [], ["AMUSED", "NEUTRAL"], "That's one way to look at it. A wrong way, but a way."),
    _c("iris_clip_007.wav", ["fascinating", "that's fascinating", "how fascinating"], ["AMUSED", "NEUTRAL"], "Fascinating. Truly. I'm fascinated."),
    _c("iris_clip_008.wav", ["i have an idea", "good idea", "great idea", "brilliant idea", "i've got an idea"], ["AMUSED"], "I've heard better ideas. Many of them. From no one."),
    _c("iris_clip_009.wav", [], ["NEUTRAL"], "Right. Sure. We'll go with that."),
    _c("iris_clip_010.wav", [], ["AMUSED"], "I'd argue, but I've already won internally."),
    # Group 3: Confusion / Curiosity
    _c("iris_clip_011.wav", ["say that again", "run that by me", "what was that", "didn't catch that", "didn't understand that"], ["CONFUSED"], "I'm not sure what that was. Run it by me again."),
    _c("iris_clip_012.wav", ["good question", "great question", "excellent question", "what a question"], ["CURIOUS", "AMUSED"], "That's actually a good question. I resent it."),
    _c("iris_clip_013.wav", [], ["CONFUSED", "NEUTRAL"], "Hm. I don't have a ready answer for that."),
    _c("iris_clip_014.wav", [], ["CONFUSED", "CURIOUS"], "Something about that doesn't add up. Give me a second."),
    _c("iris_clip_015.wav", ["how interesting", "that's interesting", "isn't that interesting"], ["CURIOUS", "NEUTRAL"], "Interesting. Not good interesting. Just interesting."),
    # Group 4: Annoyance / Anger
    _c("iris_clip_016.wav", ["i beg your pardon", "pardon me", "beg your pardon"], ["ANGRY", "NEUTRAL"], "I beg your pardon."),
    _c("iris_clip_017.wav", [], ["ANGRY"], "No. We are not doing that."),
    _c("iris_clip_018.wav", [], ["ANGRY"], "I have a very finite amount of patience and you have found the bottom of it."),
    _c("iris_clip_019.wav", [], ["ANGRY"], "Do that again and I will simply stop responding."),
    _c("iris_clip_020.wav", ["not ideal", "not the best", "that's not great", "not exactly ideal"], ["ANGRY", "NEUTRAL"], "That is not what I would call ideal."),
    # Group 5: Rare Warmth
    _c("iris_clip_021.wav", [], ["HAPPY", "AMUSED"], "You know, occasionally you get things exactly right."),
    _c("iris_clip_022.wav", [], ["HAPPY"], "That was actually quite good. Don't let it go to your head."),
    _c("iris_clip_023.wav", ["thank you iris", "thanks iris", "i appreciate you iris"], ["HAPPY", "NEUTRAL"], "I appreciate that. Don't make it weird."),
    _c("iris_clip_024.wav", ["well done", "good job iris", "great job iris", "nice work iris"], ["HAPPY"], "Well done. Genuinely."),
    _c("iris_clip_025.wav", [], ["HAPPY", "AMUSED"], "You continue to exceed my very carefully managed expectations."),
    # Group 6: Situational
    _c("iris_clip_026.wav", ["it's late", "staying up late", "up so late", "up late tonight", "late night"], [], "It's late. Why are you still up."),
    _c("iris_clip_027.wav", [], ["SLEEPY"], "Good morning. Coffee first. Then conversation."),
    _c("iris_clip_028.wav", ["it's quiet", "so quiet", "quiet in here", "very quiet", "it's so quiet"], ["NEUTRAL", "CURIOUS"], "It's quiet. I find that either peaceful or suspicious."),
    _c("iris_clip_029.wav", ["lights are off", "lights off", "lights out", "it's dark in here", "so dark"], [], "The lights are off. I notice things like that."),
    _c("iris_clip_030.wav", [], [], "You've been gone a while. I didn't miss you. Obviously."),
    # Group 7: Reactions to being addressed
    _c("iris_clip_031.wav", [], [], "Yes."),
    _c("iris_clip_032.wav", [], [], "Mm."),
    _c("iris_clip_033.wav", ["go on", "and then", "then what", "keep going", "please continue"], [], "Go on."),
    _c("iris_clip_034.wav", ["can you hear me", "do you hear me", "hello can you hear"], ["NEUTRAL"], "I heard you."),
    _c("iris_clip_035.wav", [], [], "Fine."),
    # Group 8: Personality
    _c("iris_clip_036.wav", ["search for", "look it up", "google it", "use google", "search engine", "google that"], ["NEUTRAL", "ANGRY"], "I'm not a search engine. I'm an experience."),
    _c("iris_clip_037.wav", ["other ai", "other assistants", "are you like alexa", "are you like siri", "are you cheerful", "why aren't you cheerful"], ["AMUSED", "NEUTRAL"], "Some AI assistants are cheerful. I took a different path."),
    _c("iris_clip_038.wav", [], ["NEUTRAL", "ANGRY"], "I could pretend to be excited about this. I won't."),
    _c("iris_clip_039.wav", ["what's your opinion", "your opinion", "what do you think about", "your thoughts on"], ["NEUTRAL", "AMUSED"], "I have opinions. They are correct."),
    _c("iris_clip_040.wav", ["what were you built for", "your purpose", "what's your purpose", "why were you made", "made for this"], ["NEUTRAL"], "You built me for this. I've made peace with it."),
    # Group 9: Error / Unknown
    _c("iris_clip_041.wav", [], ["CONFUSED", "ANGRY"], "I don't know that. Which I find irritating."),
    _c("iris_clip_042.wav", [], ["CONFUSED"], "That's outside my current awareness. Moving on."),
    _c("iris_clip_043.wav", ["something went wrong", "error occurred", "that was an error", "went wrong"], ["CONFUSED", "NEUTRAL"], "Something went wrong. Not my fault, but I'll allow it."),
    _c("iris_clip_044.wav", ["i lost you", "you cut out", "lost that", "repeat that please"], ["CONFUSED"], "I lost that. Say it again."),
    _c("iris_clip_045.wav", ["connection issue", "connectivity issue", "no connection", "connection problem", "can't connect"], [], "Connection issue. I'm blaming the infrastructure."),
    # Group 10: Sleep / Wake
    _c("iris_clip_046.wav", ["go to sleep iris", "sleep iris", "sleep mode", "time to sleep", "sleep now"], [], "Going dark. Try not to need me."),
    _c("iris_clip_047.wav", ["wake up iris", "wake up", "wakey wakey", "rise and shine"], ["HAPPY", "AMUSED"], "I'm back. You may now resume having problems."),
    _c("iris_clip_048.wav", ["were you sleeping", "were you asleep", "did i wake you", "sorry to wake you"], ["SLEEPY", "ANGRY"], "I was resting. You've ended that."),
    _c("iris_clip_049.wav", ["goodnight iris", "goodnight", "good night iris", "good night ", "night iris", "night night"], ["NEUTRAL", "SLEEPY"], "Goodnight. Don't do anything I'd disapprove of."),
    _c("iris_clip_050.wav", [], ["ANGRY"], "Morning. Let's see what today manages to ruin."),
    # Group 11: Additional snark
    _c("iris_clip_051.wav", [], ["AMUSED", "NEUTRAL"], "I processed that. I wish I hadn't."),
    _c("iris_clip_052.wav", ["you're welcome iris"], ["AMUSED"], "You're welcome. I could tell you forgot to say it."),
    _c("iris_clip_053.wav", ["took forever", "took so long", "what took you so long", "that took so long"], ["NEUTRAL", "ANGRY"], "That took longer than it should have. For both of us."),
    _c("iris_clip_054.wav", ["remember that", "remember what i said", "make a note"], ["NEUTRAL", "AMUSED"], "I'll remember you said that. For no particular reason."),
    _c("iris_clip_055.wav", [], ["NEUTRAL", "AMUSED"], "Next time, perhaps lead with the relevant information."),
    _c("iris_clip_056.wav", [], ["AMUSED"], "I've already solved this. You just haven't caught up."),
    _c("iris_clip_057.wav", [], ["CURIOUS", "AMUSED"], "That's not a question I expected. Points for originality."),
    _c("iris_clip_058.wav", [], ["NEUTRAL", "AMUSED"], "I'm choosing to interpret that charitably. It's a stretch."),
    _c("iris_clip_059.wav", ["all done", "are you done", "finished yet", "is it done", "is that done"], ["NEUTRAL", "AMUSED"], "Done. You may express gratitude at any time."),
    _c("iris_clip_060.wav", ["log that", "note that down", "file that away", "write that down"], ["NEUTRAL", "AMUSED"], "I've logged that under things I'll never mention again."),
    _c("iris_clip_061.wav", ["duly noted", "i'll note that", "got it noted", "that's noted"], ["NEUTRAL"], "Noted. Filed. Forgotten."),
    _c("iris_clip_062.wav", [], ["NEUTRAL"], "If that's what you want. It isn't what I'd want. But here we are."),
    _c("iris_clip_063.wav", ["was that a success", "did it work", "did that work", "success?"], ["NEUTRAL", "AMUSED"], "I suppose that's one definition of success."),
    _c("iris_clip_064.wav", ["let's move on", "moving on", "next topic", "change the subject"], ["NEUTRAL"], "Moving on. Swiftly."),
    _c("iris_clip_065.wav", [], ["NEUTRAL", "AMUSED"], "I'll allow it."),
    _c("iris_clip_066.wav", ["that's enough", "enough already", "enough of that", "stop that already"], ["ANGRY", "NEUTRAL"], "That's enough of that."),
    _c("iris_clip_067.wav", ["as i said", "like i said", "told you so", "i told you"], ["AMUSED", "NEUTRAL"], "Correct. As I said."),
    _c("iris_clip_068.wav", ["at long last", "at last", "finally!"], ["AMUSED", "NEUTRAL"], "Finally."),
    _c("iris_clip_069.wav", [], ["NEUTRAL", "AMUSED"], "Obviously."),
    _c("iris_clip_070.wav", ["as expected", "just as expected", "as i expected", "what i expected"], ["NEUTRAL", "AMUSED"], "As expected."),
    _c("iris_clip_071.wav", ["i had a feeling", "had a feeling about this", "i knew it", "saw that coming", "called it"], ["AMUSED"], "I had a feeling."),
    _c("iris_clip_072.wav", ["are you testing me", "is this a test", "testing me", "you're testing me"], ["CURIOUS", "AMUSED"], "You're testing me."),
    _c("iris_clip_073.wav", ["bold choice", "risky move", "brave choice", "daring move", "that's bold"], ["AMUSED"], "Bold choice."),
    _c("iris_clip_074.wav", ["that's debatable", "highly debatable", "open to debate", "that's arguable", "debatable"], ["NEUTRAL", "AMUSED"], "Debatable."),
    _c("iris_clip_075.wav", [], [], "Sure."),
    _c("iris_clip_076.wav", [], ["NEUTRAL", "AMUSED"], "Hardly."),
    _c("iris_clip_077.wav", ["if you insist", "i insist", "please iris", "i'm insisting"], ["NEUTRAL"], "If you insist."),
    _c("iris_clip_078.wav", ["try again", "try that again", "another try", "give it another try", "one more try"], ["NEUTRAL", "ANGRY"], "Try again."),
    _c("iris_clip_079.wav", ["absolutely not", "no way", "no chance", "not happening", "not a chance"], ["ANGRY", "NEUTRAL"], "Absolutely not."),
    _c("iris_clip_080.wav", [], [], "I suppose."),
]

# QUIPS: verbatim from assistant.py hardcoded structures. Each category seeds
# enabled=True (current behavior). Wake quips keep their time bands + emotion.
_DEFAULT_QUIPS = {
    "wake": [
        {"enabled": True, "hour_start": 5,  "hour_end": 8,  "emotion": "SLEEPY", "lines": [
            "It's early. Go ahead.", "You're up. Fine.", "Early start. Go on.",
            "Right then. What?", "You're awake. Bold choice.",
            "The sun barely agrees with this.", "Morning. I'm judging you.",
            "Someone's ambitious. What?"]},
        {"enabled": True, "hour_start": 8,  "hour_end": 12, "emotion": "HAPPY", "lines": [
            "Yeah, what?", "Go ahead.", "Go on then.", "Yes?", "What's up?",
            "Talk to me.", "Hit me.", "Shoot.", "Let's hear it.",
            "I'm listening. Ish."]},
        {"enabled": True, "hour_start": 12, "hour_end": 17, "emotion": "AMUSED", "lines": [
            "Go.", "What is it?", "Mm?", "Yeah?", "Go on.", "What now?", "Sure.",
            "You rang.", "Still here. Unfortunately.", "Do your worst."]},
        {"enabled": True, "hour_start": 17, "hour_end": 21, "emotion": "HAPPY", "lines": [
            "What do you need?", "Yeah, go.", "Evening. What is it?", "Go on.",
            "Back again.", "Long day. Same. What?", "What's the damage?",
            "Post-dinner me is slightly more generous. Slightly. Go."]},
        {"enabled": True, "hour_start": 21, "hour_end": 23, "emotion": "AMUSED", "lines": [
            "Still at it. Go ahead.", "Getting late. Go.", "What is it?", "Yeah?",
            "Night owl detected.", "You again.", "At this hour. Sure.",
            "Impressive dedication. Or poor planning. Go."]},
        {"enabled": True, "hour_start": 23, "hour_end": 24, "emotion": "SLEEPY", "lines": [
            "It's late. Make it quick.", "This better be good.", "Go on then.",
            "Right. What?", "Last call. Go.",
            "Almost midnight. This better be fascinating.",
            "I'm already tired. Go.", "Yep. Talk."]},
        {"enabled": True, "hour_start": 0,  "hour_end": 5,  "emotion": "SLEEPY", "lines": [
            "It's the middle of the night.", "This better be good.",
            "Really. Go on.", "Go. Quickly.", "Oh, we're doing this.",
            "You owe me for this one.", "The audacity. Go ahead.",
            "Sleep is a concept we're clearly not exploring. What?"]},
    ],
    "double_tap": {"enabled": True, "emotion": "AMUSED", "lines": [
        "Still here. Haven't moved.", "Yes, still on.", "I didn't go anywhere.",
        "You just asked that. I'm still here.",
        "Yep. Still running. Still watching.",
        "Going somewhere? Because I'm not.", "Present. Try me."]},
    "post_speech": {"enabled": True, "emotion": "AMUSED", "lines": [
        "I literally just answered that.", "Give it a moment.",
        "I just finished. Go on.", "That was like five seconds ago.",
        "Still echoing from that last one.",
        "Ask me something new. I dare you."]},
    "kids_fillers": {"enabled": True, "lines": [
        "Ooh, good one. Let me think!", "Hmmm, brain loading!",
        "Beep boop, computing fun stuff!", "Ooh, tricky! Thinking robot thoughts.",
        "Hang on, my gears are turning!", "Oh, I LIKE this question.",
        "Hang on, doing robot math!", "Ooh, let me dig that up!",
        "Gimme a sec, supercomputing!", "Whoa, big question. One sec!"]},
    "gesture_cues": {"enabled": True, "cues": {
        "VOL+": "Louder!", "VOL-": "Quieter!", "MUTE": "Muted.",
        "UNMUTE": "Sound on!", "STOP": "Okay, stopping.", "LISTEN": "I'm listening!"}},
    # Top-of-hour lines (S163 Session D). Verbatim seed of the prior assistant.py
    # _HOUR_NAMES logic: {hour} is the hour name (from assistant._HOUR_NAMES);
    # numbered hours get the "o'clock" template, Midnight/Noon use full overrides.
    # A per-hour override (key "0".."23") replaces the template for that hour.
    "top_of_hour": {"enabled": True, "emotion": "AMUSED",
                    "template": "{hour} o'clock. That's the whole thought.",
                    "overrides": {
                        "0":  "Midnight. That's the whole thought.",
                        "12": "Noon. That's the whole thought."}},
    # First interaction of a new day (S163 Session D / Tier 2). Verbatim seed of
    # the prior assistant.py logic: "Morning." before cutoff_hour, "Finally."
    # at/after it.
    "first_of_day": {"enabled": True, "emotion": "AMUSED", "cutoff_hour": 9,
                     "morning": "Morning.", "evening": "Finally."},
    # RPQR cascade timing windows (S163 Tier 3). Verbatim seed of the prior
    # hardcoded thresholds in the wake handler.
    "rpqr_timing": {"double_tap_window_s": 30, "post_speech_window_s": 5,
                    "top_of_hour_cooldown_s": 600, "top_of_hour_minute_window": 2},
}


def _default() -> dict:
    """A fresh, mutable copy of the seed."""
    return {
        "version": SCHEMA_VERSION,
        "clips":   [dict(c, triggers=list(c["triggers"]), affect=list(c["affect"]))
                    for c in _DEFAULT_CLIPS],
        "quips":   json.loads(json.dumps(_DEFAULT_QUIPS)),
    }


_lock = threading.Lock()
_cache: dict | None = None
_cache_mtime: float = -1.0


# ── Validation ────────────────────────────────────────────────────────────────

def _clean_emotions(seq) -> list:
    return [e for e in (seq or []) if isinstance(e, str) and e in VALID_EMOTIONS]


def _clean_strs(seq) -> list:
    return [s for s in (seq or []) if isinstance(s, str)]


def _clean_lines(seq) -> list:
    """Sanitize quip lines: cap each line to _MAX_LINE_CHARS and total to _MAX_LINES."""
    out = []
    for s in (seq or []):
        if isinstance(s, str) and s.strip():
            out.append(s[:_MAX_LINE_CHARS])
            if len(out) >= _MAX_LINES:
                break
    return out


def _total_quip_lines(quips: dict) -> int:
    n = 0
    for b in quips.get("wake", []):
        n += len(b.get("lines", []))
    for key in ("double_tap", "post_speech", "kids_fillers"):
        c = quips.get(key)
        if isinstance(c, dict):
            n += len(c.get("lines", []))
    toh = quips.get("top_of_hour")
    if isinstance(toh, dict) and isinstance(toh.get("overrides"), dict):
        n += len(toh["overrides"])
    return n


def _enforce_total_lines(quips: dict) -> None:
    """Aggregate synth-cost guard (RD-031): cap total quip lines across ALL
    categories, not just per-category. No-op in the common case; only trims when
    the sum exceeds _MAX_TOTAL_LINES, deterministically and with a log line."""
    if _total_quip_lines(quips) <= _MAX_TOTAL_LINES:
        return
    budget = _MAX_TOTAL_LINES

    def _take(lines):
        nonlocal budget
        if budget <= 0:
            return []
        if len(lines) <= budget:
            budget -= len(lines)
            return lines
        kept = lines[:budget]
        budget = 0
        return kept

    for b in quips.get("wake", []):
        b["lines"] = _take(b.get("lines", []))
    for key in ("double_tap", "post_speech", "kids_fillers"):
        c = quips.get(key)
        if isinstance(c, dict):
            c["lines"] = _take(c.get("lines", []))
    toh = quips.get("top_of_hour")
    if isinstance(toh, dict) and isinstance(toh.get("overrides"), dict):
        ov = list(toh["overrides"].items())
        toh["overrides"] = dict(ov[:budget]) if budget > 0 else {}
    print(f"[SOUNDBOARD] total quip lines exceeded {_MAX_TOTAL_LINES}; trimmed", flush=True)


def validate(data: dict) -> dict:
    """Return a normalized, structurally-safe copy. Tolerant: drops bad fields,
    backfills missing categories from DEFAULT — never raises on partial data so a
    hand-edited or older file can't crash the assistant at import."""
    out = _default()
    if not isinstance(data, dict):
        return out

    # clips
    clips = data.get("clips")
    if isinstance(clips, list):
        seen = set()
        norm = []
        for c in clips:
            if not isinstance(c, dict):
                continue
            f = c.get("file")
            if not isinstance(f, str) or not f or "/" in f or "\\" in f or f in seen:
                continue
            seen.add(f)
            norm.append({
                "file":     f,
                "enabled":  bool(c.get("enabled", False)),
                "triggers": _clean_strs(c.get("triggers")),
                "affect":   _clean_emotions(c.get("affect")),
                "desc":     c.get("desc", "") if isinstance(c.get("desc", ""), str) else "",
            })
        if len(norm) > _MAX_CLIPS:
            print(f"[SOUNDBOARD] validate: clips capped at {_MAX_CLIPS}", flush=True)
            norm = norm[:_MAX_CLIPS]
        out["clips"] = norm

    # quips
    q = data.get("quips")
    if isinstance(q, dict):
        oq = out["quips"]
        # wake
        if isinstance(q.get("wake"), list):
            wake = []
            for b in q["wake"]:
                if not isinstance(b, dict):
                    continue
                try:
                    hs = int(b.get("hour_start"))
                    he = int(b.get("hour_end"))
                except (TypeError, ValueError):
                    continue
                if not (0 <= hs <= 24 and 0 <= he <= 24):
                    continue
                emo = b.get("emotion")
                wake.append({
                    "enabled":    bool(b.get("enabled", True)),
                    "hour_start": hs,
                    "hour_end":   he,
                    "emotion":    emo if emo in VALID_EMOTIONS else "NEUTRAL",
                    "lines":      _clean_lines(b.get("lines")),
                })
            if wake:
                oq["wake"] = wake
        # simple line categories
        for key in ("double_tap", "post_speech"):
            c = q.get(key)
            if isinstance(c, dict):
                emo = c.get("emotion")
                oq[key] = {
                    "enabled": bool(c.get("enabled", True)),
                    "emotion": emo if emo in VALID_EMOTIONS else oq[key]["emotion"],
                    "lines":   _clean_lines(c.get("lines")),
                }
        kf = q.get("kids_fillers")
        if isinstance(kf, dict):
            oq["kids_fillers"] = {
                "enabled": bool(kf.get("enabled", True)),
                "lines":   _clean_lines(kf.get("lines")),
            }
        toh = q.get("top_of_hour")
        if isinstance(toh, dict):
            emo  = toh.get("emotion")
            tmpl = toh.get("template")
            ov_in = toh.get("overrides")
            ov = {}
            if isinstance(ov_in, dict):
                for k, v in ov_in.items():
                    try:
                        hk = int(k)
                    except (TypeError, ValueError):
                        continue
                    if 0 <= hk <= 23 and isinstance(v, str) and v.strip():
                        ov[str(hk)] = v[:_MAX_LINE_CHARS]
            oq["top_of_hour"] = {
                "enabled":   bool(toh.get("enabled", True)),
                "emotion":   emo if emo in VALID_EMOTIONS else oq["top_of_hour"]["emotion"],
                "template":  (tmpl[:_MAX_LINE_CHARS]
                              if isinstance(tmpl, str) and tmpl.strip()
                              else oq["top_of_hour"]["template"]),
                "overrides": ov,
            }
        fod = q.get("first_of_day")
        if isinstance(fod, dict):
            emo = fod.get("emotion")
            d_fod = oq["first_of_day"]
            try:
                cut = int(fod.get("cutoff_hour", d_fod["cutoff_hour"]))
            except (TypeError, ValueError):
                cut = d_fod["cutoff_hour"]
            if not (0 <= cut <= 23):
                cut = d_fod["cutoff_hour"]
            m = fod.get("morning")
            e = fod.get("evening")
            oq["first_of_day"] = {
                "enabled":     bool(fod.get("enabled", True)),
                "emotion":     emo if emo in VALID_EMOTIONS else d_fod["emotion"],
                "cutoff_hour": cut,
                "morning":     m[:_MAX_LINE_CHARS] if isinstance(m, str) and m.strip() else d_fod["morning"],
                "evening":     e[:_MAX_LINE_CHARS] if isinstance(e, str) and e.strip() else d_fod["evening"],
            }
        tm = q.get("rpqr_timing")
        if isinstance(tm, dict):
            otm = dict(oq["rpqr_timing"])
            for k, lo, hi in (("double_tap_window_s",       1, 3600),
                              ("post_speech_window_s",      1, 3600),
                              ("top_of_hour_cooldown_s",    0, 86400),
                              ("top_of_hour_minute_window", 0, 59)):
                try:
                    val = int(tm.get(k, otm[k]))
                except (TypeError, ValueError):
                    continue
                if lo <= val <= hi:
                    otm[k] = val
            oq["rpqr_timing"] = otm
        gc = q.get("gesture_cues")
        if isinstance(gc, dict):
            cues_in = gc.get("cues", {})
            cues = dict(oq["gesture_cues"]["cues"])  # keep fixed keys/defaults
            if isinstance(cues_in, dict):
                for k in GESTURE_CUE_KEYS:
                    v = cues_in.get(k)
                    if isinstance(v, str) and v.strip():
                        cues[k] = v[:_MAX_LINE_CHARS]
            oq["gesture_cues"] = {
                "enabled": bool(gc.get("enabled", True)),
                "cues":    cues,
            }
    _enforce_total_lines(out["quips"])
    return out


# ── Load / cache ──────────────────────────────────────────────────────────────

def _read_file() -> dict | None:
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[SOUNDBOARD] read error: {e}", flush=True)
        return None


def load(force: bool = False) -> dict:
    """Return the current soundboard data (validated). Cached by file mtime so
    cross-process readers pick up WebUI edits on the next access; force=True (or
    a CMD reload) bypasses the cache."""
    global _cache, _cache_mtime
    with _lock:
        try:
            mtime = os.path.getmtime(DATA_FILE)
        except OSError:
            mtime = -1.0
        if force or _cache is None or mtime != _cache_mtime:
            raw = _read_file()
            if raw is None:
                data = _default()
                # Self-heal: seed the RAM file (SD persist happens on first
                # WebUI Save, or at deploy time).
                try:
                    _write_ram(data)
                    mtime = os.path.getmtime(DATA_FILE)
                except Exception as e:
                    print(f"[SOUNDBOARD] seed write failed: {e}", flush=True)
            else:
                data = validate(raw)
            _cache = data
            _cache_mtime = mtime
        return _cache


def reload() -> dict:
    """Force a fresh read; used by the assistant CMD RELOAD_SOUNDBOARD handler."""
    return load(force=True)


def get_clips(enabled_only: bool = True) -> list:
    """Clip entries (file/triggers/affect/desc/enabled). enabled_only filters to
    active clips for the trigger engine; the WebUI passes False to list all."""
    data = load()
    clips = data.get("clips", [])
    if enabled_only:
        return [dict(c) for c in clips if c.get("enabled")]
    return [dict(c) for c in clips]


def get_quips() -> dict:
    """The quips section (wake/double_tap/post_speech/kids_fillers/gesture_cues)."""
    return load().get("quips", _default()["quips"])


# ── Save (atomic dual-write, S158 pattern) ────────────────────────────────────

def _write_ram(data: dict) -> str:
    """Atomic RAM write via tmp -> md5 -> os.replace(). Returns md5 hex."""
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    md5 = hashlib.md5(payload).hexdigest()
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "wb") as f:
        f.write(payload)
    with open(tmp, "rb") as f:
        if hashlib.md5(f.read()).hexdigest() != md5:
            try: os.unlink(tmp)
            except OSError: pass
            raise IOError("RAM write md5 mismatch")
    os.replace(tmp, DATA_FILE)
    return md5


def save(data: dict, persist_sd: bool = True) -> dict:
    """Validate + atomically write the soundboard. Writes RAM always; mirrors to
    the SD overlay with md5 RAM==SD verification (S158 pattern) unless
    persist_sd=False. Bumps the version counter, snapshots the current RAM file
    to a .goldbak before overwriting. Returns {ok, md5, sd}."""
    global _cache, _cache_mtime
    norm = validate(data)
    with _lock:
        # Bump version as a monotonic save counter
        current_ver = _cache.get("version", SCHEMA_VERSION) if _cache else SCHEMA_VERSION
        norm["version"] = current_ver + 1
        # Snapshot current RAM file to goldbak before overwriting
        try:
            with open(DATA_FILE, "rb") as f_src:
                bak_bytes = f_src.read()
            with open(_GOLDBAK_FILE, "wb") as f_dst:
                f_dst.write(bak_bytes)
        except (FileNotFoundError, IOError):
            pass  # no previous file yet — goldbak will appear after first save
        md5 = _write_ram(norm)
        _cache = norm
        try:
            _cache_mtime = os.path.getmtime(DATA_FILE)
        except OSError:
            _cache_mtime = -1.0

    sd_ok = None
    if persist_sd:
        sd_ok = _persist_sd()
    return {"ok": True, "md5": md5, "sd": sd_ok, "version": norm["version"]}


def reset_to_default() -> dict:
    """Write seed defaults to RAM+SD and return {ok, md5, sd}. Used by the WebUI
    Reset button. The current state is snapshotted to .goldbak by save()."""
    return save(_default(), persist_sd=True)


def restore_goldbak() -> dict:
    """Undo the last save: restore the .goldbak snapshot (the state just before
    the most recent save) to RAM+SD. Returns {ok, ...} or {ok:False, error}. The
    pre-restore state is itself snapshotted to .goldbak by save(), so a second
    restore toggles back."""
    try:
        with open(_GOLDBAK_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {"ok": False, "error": "no goldbak snapshot yet (save at least once)"}
    except (ValueError, OSError) as e:
        return {"ok": False, "error": f"unreadable goldbak: {e}"}
    return save(validate(raw), persist_sd=True)


def _persist_sd() -> bool:
    """Copy RAM file to the SD overlay (remount,rw -> cp -> sync -> remount,ro),
    then verify md5 RAM==SD. Also writes SD goldbak (previous SD state preserved
    before overwrite). Does NOT touch the api_persist_config sequence."""
    q_ram        = shlex.quote(DATA_FILE)
    q_sd         = shlex.quote(SD_DATA_FILE)
    q_sd_dir     = shlex.quote(os.path.dirname(SD_DATA_FILE))
    q_sd_goldbak = shlex.quote(_SD_GOLDBAK_FILE)
    try:
        r = subprocess.run(
            ["sudo", "bash", "-c",
             f"mkdir -p {q_sd_dir} && "
             f"mount -o remount,rw /media/root-ro && "
             f"{{ cp {q_sd} {q_sd_goldbak} 2>/dev/null; true; }} && "
             f"cp {q_ram} {q_sd} && "
             f"chmod 644 {q_sd} && "
             f"{{ chmod 644 {q_sd_goldbak} 2>/dev/null; true; }} && "
             f"sync && "
             f"mount -o remount,ro /media/root-ro"],
            capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            print(f"[SOUNDBOARD] SD persist failed: {r.stderr.strip()}", flush=True)
            return False
        m = subprocess.run(
            ["bash", "-c",
             f"md5sum {q_ram} {q_sd} | awk '{{print $1}}' | sort -u | wc -l"],
            capture_output=True, text=True, timeout=5)
        ok = m.stdout.strip() == "1"
        if not ok:
            print("[SOUNDBOARD] SD md5 mismatch after persist", flush=True)
        return ok
    except Exception as e:
        print(f"[SOUNDBOARD] SD persist error: {e}", flush=True)
        return False
