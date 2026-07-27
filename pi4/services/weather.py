"""
services/weather.py - the one outside weather source (RD-068).

Before this she had no weather input of any kind and invented one: asked what
the weather was doing she answered "It's raining, and it will continue for
another hour or so. Temperature is a chilly 10 degrees celsius." All of it made
up, and metric on top. That is the RD-063 confabulation class with a cheap fix.

WHAT THIS IS NOT. It is not an intercept. Nothing here composes a reply or
short-circuits the model. The router detects a weather-shaped utterance, this
module fetches the conditions, and the conditions ride into the prompt as one
factual clause so SHE answers, in her own voice, about anything the
conversation touches -- "do I need a coat?" as much as "what's the weather".
An intercepted canned string would answer the first and be useless for the
second.

WHY OPEN-METEO. Verified live from the Pi4 at S242, not assumed: HTTP 200 in
0.70 s, no API key, no account, and imperial natively via temperature_unit /
wind_speed_unit / precipitation_unit, which matches the American-units
directive with zero conversion code. There is nothing to build here and
nothing to buy.

FAIL CLOSED, and this is the load-bearing property. Any failure -- network,
timeout, HTTP error, malformed payload, missing field -- injects NOTHING and
lets her persona answer honestly that she does not know. A stale number is
worse than no number, because a number sounds true. So the cache serves only
inside its TTL; once it expires it is gone, and a failed refresh does not
resurrect it.

Everything is read live from core.config per call (never star-imported), so a
location change from the WebUI takes effect on the next turn with no restart
and no redeploy.

Run `python services/weather.py --selftest` (no network) for the detector, the
WMO table and the clause assembly. Add `--live` to also hit the real API.
"""
from __future__ import annotations

import re
import time

import requests

_API_URL = "https://api.open-meteo.com/v1/forecast"

# The fields the clause is built from. `is_day` is requested because it costs
# nothing and disambiguates a future "is it dark out" without another round trip.
_CURRENT_FIELDS = ("temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "precipitation,weather_code,wind_speed_10m,is_day")

# ── Detection ─────────────────────────────────────────────────────────────────
# Deliberately biased toward RECALL. A false positive costs one true sentence of
# context and a cached lookup; a false negative costs the accuracy this whole
# item exists for. Everything matched here is a fact she is handed, never an
# instruction, so over-matching cannot make her say anything wrong.

_WX_TOPIC_RE = re.compile(
    r"\bweather\b|\bforecast\b|\btemperature\b|\bhumidity\b|\bwind ?chill\b"
    r"|\bheat index\b|\bdew point\b"
    r"|\bhow (?:hot|cold|warm|cool|windy|humid)\b"
)

# Conditions. Bare "cloud"/"clouds" is left out on purpose (cloud storage,
# cloudy judgment) and "snow" excludes Snow White, who comes up in kids stories.
_WX_COND_RE = re.compile(
    r"\brain(?:ing|y|fall)?\b|\bsnow(?!\s*white)(?:ing|y|fall)?\b|\bsunny\b|\bsunshine\b"
    r"|\bcloudy\b|\bovercast\b|\bdrizzl(?:e|ing)\b|\bthunder(?:storm)?s?\b|\blightning\b"
    r"|\bhail(?:ing)?\b|\bsleet\b|\bfog(?:gy)?\b|\bwindy\b|\bmuggy\b|\bhumid\b"
    r"|\bfreezing\b|\bscorching\b|\bheat ?wave\b|\bblizzard\b|\bstorm(?:y|s)?\b"
)

# "do I need a coat", "should I bring an umbrella", "can I wear shorts".
_WX_GEAR_RE = re.compile(
    r"\b(?:need|needs|wear|wearing|bring|grab|take|want|pack)\b[^.?!]{0,40}?"
    r"\b(?:coat|jacket|umbrella|sunscreen|sunglasses|shorts|boots|gloves|mittens"
    r"|scarf|beanie|raincoat|hoodie|sweater|snow ?pants)\b"
)

# "is it cold out", "nice outside today", "how miserable is it out there".
_WX_OUT_RE = re.compile(
    r"\b(?:hot|cold|warm|cool|chilly|nice|nasty|miserable|pleasant|balmy|breezy|"
    r"gross|lovely|brutal)\b[^.?!]{0,24}?"
    r"\b(?:out|outside|outdoors|out there|today|tonight|this morning|"
    r"this afternoon|this evening)\b"
)


def is_weather_question(text: str) -> bool:
    """True when this utterance is one the outside conditions bear on.

    Never raises: an unusable input is simply not a weather question.
    """
    try:
        norm = (text or "").lower()
    except Exception:
        return False
    return bool(_WX_TOPIC_RE.search(norm) or _WX_COND_RE.search(norm)
                or _WX_GEAR_RE.search(norm) or _WX_OUT_RE.search(norm))


# ── WMO weather_code -> spoken English ────────────────────────────────────────
# The API returns a WMO code, not a word. These are the words she should be
# handed: what a person standing outside would say, not what a METAR says.

_WMO = {
    0:  "clear",
    1:  "mostly clear",
    2:  "partly cloudy",
    3:  "overcast",
    45: "foggy",
    48: "foggy with freezing fog",
    51: "drizzling lightly",
    53: "drizzling",
    55: "drizzling heavily",
    56: "drizzling freezing rain",
    57: "drizzling heavy freezing rain",
    61: "raining lightly",
    63: "raining",
    65: "raining hard",
    66: "raining freezing rain",
    67: "raining heavy freezing rain",
    71: "snowing lightly",
    73: "snowing",
    75: "snowing hard",
    77: "spitting snow grains",
    80: "with light rain showers",
    81: "with rain showers",
    82: "with violent rain showers",
    85: "with light snow showers",
    86: "with heavy snow showers",
    95: "thundering with a storm",
    96: "thundering with a hailstorm",
    99: "thundering with a heavy hailstorm",
}


def describe_code(code) -> str:
    """WMO code to the phrase that follows "and". Unknown code -> ""."""
    try:
        return _WMO.get(int(code), "")
    except (TypeError, ValueError):
        return ""


# ── Fetch + cache ─────────────────────────────────────────────────────────────
# One shared cache. `data` is served only while it is inside the TTL; on expiry
# it is dropped rather than fallen back on, which is what "fail closed" means
# here. `fail_t` bounds retries (and therefore log lines, RD-031) after an
# outage so a dead network cannot produce one failed call per weather turn.

_FAIL_BACKOFF_S = 60.0

_cache = {"t": 0.0, "data": None, "fail_t": 0.0}


def current(force: bool = False) -> dict | None:
    """This location's current conditions, or None.

    Returns the parsed `current` block from Open-Meteo. Serves the cache while
    it is inside WEATHER_CACHE_SECS. Returns None on ANY failure and on any
    expired cache that could not be refreshed -- never a stale reading.
    """
    import core.config as _cfg
    if not getattr(_cfg, "WEATHER_ENABLED", True):
        return None
    now = time.monotonic()
    ttl = float(getattr(_cfg, "WEATHER_CACHE_SECS", 600))
    if not force and _cache["data"] is not None and (now - _cache["t"]) < ttl:
        return _cache["data"]
    if not force and (now - _cache["fail_t"]) < _FAIL_BACKOFF_S:
        return None
    params = {
        "latitude":          getattr(_cfg, "WEATHER_LAT", 41.0352),
        "longitude":         getattr(_cfg, "WEATHER_LON", -111.9386),
        # "auto" derives the zone from the coordinates. It is the fallback rather
        # than a hardcoded zone because a blanked WebUI text field arrives as "",
        # and a wrong zone would silently shift every reading by hours.
        "timezone":          getattr(_cfg, "WEATHER_TZ", "") or "auto",
        "current":           _CURRENT_FIELDS,
        "temperature_unit":  "fahrenheit",
        "wind_speed_unit":   "mph",
        "precipitation_unit": "inch",
    }
    try:
        r = requests.get(_API_URL, params=params,
                         timeout=float(getattr(_cfg, "WEATHER_TIMEOUT_S", 3.0)))
        r.raise_for_status()
        cur = (r.json() or {}).get("current") or {}
        if "temperature_2m" not in cur:
            raise ValueError("payload carried no temperature_2m")
        _cache["data"] = cur
        _cache["t"] = now
        _cache["fail_t"] = 0.0
        # Bounded by the TTL: at most one line per WEATHER_CACHE_SECS.
        print(f"[WX]   fetched {cur.get('temperature_2m')}F code={cur.get('weather_code')} "
              f"in {r.elapsed.total_seconds():.2f}s", flush=True)
        return cur
    except Exception as e:
        _cache["data"] = None      # expired and unrefreshable -> gone, not stale
        _cache["fail_t"] = now
        print(f"[WX]   fetch failed ({e}) -- no reading available", flush=True)
        return None


# ── Clause assembly ───────────────────────────────────────────────────────────
# S229 rule (memory project_clause_world_state_beats_conditional): the clause
# STATES THE WORLD. It never says "if they ask about the weather, tell them..."
# -- a directive in this slot gets read aloud. It also names Fahrenheit once,
# because the invention this replaces was in celsius.

def format_clause(data: dict, place: str = "") -> str:
    """Build the "(Context, not spoken: ...)" clause, or "" if unusable.

    `place` names where the reading is FOR. Measured at S243e over 18 samples:
    handed a bare temperature she invents a location to attach it to ("It's July
    in Colorado" -- the house is in Utah), because nothing else in the prompt
    says where she is. "currently" is load-bearing in the same way: the same run
    produced "it's been in the high nineties all week" and "no severe weather
    forecasted" off a single instantaneous reading.
    """
    if not data:
        return ""
    try:
        temp = int(round(float(data["temperature_2m"])))
    except (KeyError, TypeError, ValueError):
        return ""
    cond = describe_code(data.get("weather_code"))
    where = f" outside this house in {place}" if place else " outside"
    parts = [f"the weather{where} is currently {temp} degrees Fahrenheit"]
    if cond:
        parts.append(f" and {cond}")
    # Feels-like only when it disagrees enough to matter to a person.
    try:
        feels = int(round(float(data["apparent_temperature"])))
        if abs(feels - temp) >= 5:
            parts.append(f", and it feels like {feels}")
    except (KeyError, TypeError, ValueError):
        pass
    # Wind only when it is actually a thing you would notice.
    try:
        wind = int(round(float(data["wind_speed_10m"])))
        if wind >= 12:
            parts.append(f", with the wind at {wind} miles an hour")
    except (KeyError, TypeError, ValueError):
        pass
    return "(Context, not spoken: " + "".join(parts) + ".)"


# THE MISS CLAUSE, and it is the whole safety property (S243e).
#
# The first cut of this module failed closed by injecting NOTHING when the fetch
# failed, on the reasoning that silence is safer than a stale number. Measured,
# that is exactly backwards: asked with nothing injected, she invented a
# temperature in 18 of 18 samples -- 87, 60, 83, 75, 69, 92 degrees, against an
# actual 100. Silence is not a signal. It is indistinguishable from nobody having
# asked, so it leaves an open prompt to invent into.
#
# This is the same lesson core.recall already paid for, in the same words:
# config.py on RECALL_MISS_CLAUSE says "with it Off she gets no signal that the
# search came back empty, and fills the silence with invention." So this is
# core.recall._NO_MEMORY's shape, which measured 8/10 against 2/10 for the
# alternative wording: state the world first, then ONE thin backstop, then the
# same scope limiter. Do not re-tune it by taste -- that spends a measurement.
_WX_NO_DATA = ("(Context, not spoken: you have just checked your weather feed and it "
               "did not answer. You genuinely do not know what it is doing outside "
               "right now. Say so plainly and briefly in your own voice, and do not "
               "estimate a temperature. Answer only from what is actually in front of "
               "you in this conversation.)")


def clause_for(text: str) -> str:
    """The whole job for one utterance: detect, fetch, format.

    Returns "" when the utterance is not weather-shaped -- no network, no cost.
    When it IS weather-shaped, this ALWAYS returns a clause: the conditions when
    they are known, and the explicit miss clause when they are not. Never raises;
    a weather clause is never worth losing a reply for (core.recall.prepare's
    contract).
    """
    try:
        if not is_weather_question(text):
            return ""
        import core.config as _cfg
        # OFF is not the same as BROKEN, and conflating them is a real trap: with
        # the feature switched off, current() also returns None, so a naive
        # fall-through would tell her "your weather feed did not answer" on every
        # weather question forever. Switched off she simply has no feed, which is
        # what the honesty half of the persona already covers.
        if not getattr(_cfg, "WEATHER_ENABLED", True):
            return ""
        data = current()
        if data:
            return format_clause(data, getattr(_cfg, "WEATHER_PLACE", "") or "")
        if getattr(_cfg, "WEATHER_MISS_CLAUSE", True):
            print("[WX]   no data -- injecting the miss clause", flush=True)
            return _WX_NO_DATA
        return ""
    except Exception as e:
        print(f"[WX]   clause skipped: {e}", flush=True)
        return ""


# ── selftest: offline unless --live ───────────────────────────────────────────

def _selftest(live: bool = False) -> int:
    ran, fails = [], []

    def check(label, cond):
        ran.append(label)
        print("%-64s %s" % (label, "PASS" if cond else "FAIL"))
        if not cond:
            fails.append(label)

    # 1. Detection: the utterances that must reach the fetch.
    for q in ("what's the weather doing out there", "what's the forecast",
              "is it cold outside", "do I need a coat today",
              "should I bring an umbrella", "is it raining",
              "how hot is it", "what's the temperature",
              "can I wear shorts today", "is it nice out",
              "is it going to snow", "how windy is it"):
        check(f"detects: {q!r}", is_weather_question(q))

    # 2. Detection: the ones that must NOT, because they only look weather-shaped.
    for q in ("tell me a story about snow white", "I think I'm getting a cold",
              "what's in the cloud storage", "let's brainstorm some ideas",
              "what time is it", "pick a random number",
              "what did the doctor say about my knee"):
        check(f"ignores: {q!r}", not is_weather_question(q))

    # 3. WMO table.
    check("WMO 0 is clear", describe_code(0) == "clear")
    check("WMO 3 is overcast", describe_code(3) == "overcast")
    check("WMO 95 is a thunderstorm", "thunder" in describe_code(95))
    check("unknown WMO code degrades to empty", describe_code(4242) == "")
    check("non-numeric WMO code degrades to empty", describe_code(None) == "")

    # 4. Clause assembly, including the thresholds.
    c = format_clause({"temperature_2m": 68.4, "weather_code": 3,
                       "apparent_temperature": 68.0, "wind_speed_10m": 4.0},
                      place="Kaysville, Utah")
    check("plain clause states the world in Fahrenheit",
          c == "(Context, not spoken: the weather outside this house in Kaysville, "
               "Utah is currently 68 degrees Fahrenheit and overcast.)")
    check("clause carries no conditional and no directive (S229)",
          " if " not in c and "tell them" not in c and "you should" not in c)
    # S243e: both of these were measured as real confabulations off a bare clause.
    check("clause names WHERE the reading is for (she invented 'Colorado')",
          "Kaysville, Utah" in c)
    check("clause says the reading is CURRENT (she invented a week and a forecast)",
          "currently" in c)
    check("clause with no place configured still reads correctly",
          format_clause({"temperature_2m": 68.4, "weather_code": 3})
          == "(Context, not spoken: the weather outside is currently 68 degrees "
             "Fahrenheit and overcast.)")
    c2 = format_clause({"temperature_2m": 99.5, "weather_code": 0,
                        "apparent_temperature": 106.2, "wind_speed_10m": 18.0})
    check("feels-like appears when it differs by 5+",
          "and it feels like 106" in c2)
    check("wind appears when it is 12 mph or more",
          "with the wind at 18 miles an hour" in c2)
    c3 = format_clause({"temperature_2m": 70.0, "weather_code": 0,
                        "apparent_temperature": 72.0, "wind_speed_10m": 6.0})
    check("feels-like suppressed when it is within 4",
          "feels like" not in c3)
    check("wind suppressed below 12 mph", "wind" not in c3)

    # 5. Fail closed -- meaning she is TOLD there is nothing, not left in silence.
    check("empty payload yields no conditions clause", format_clause({}) == "")
    check("None payload yields no conditions clause", format_clause(None) == "")
    check("payload with no temperature yields no conditions clause",
          format_clause({"weather_code": 0}) == "")
    check("a non-weather utterance never fetches", clause_for("what time is it") == "")

    # 6. THE MISS CLAUSE (S243e). Silence measured at 18/18 invented temperatures,
    #    so an unreachable feed must inject an explicit "you do not know", not "".
    _saved_url, _saved_cache = _API_URL, dict(_cache)
    try:
        globals()["_API_URL"] = "https://192.0.2.1/v1/forecast"   # unroutable TEST-NET-1
        _cache["data"] = None; _cache["t"] = 0.0; _cache["fail_t"] = 0.0
        miss = clause_for("what's the weather doing out there")
        check("an unreachable feed injects the miss clause, NOT silence", miss != "")
        check("the miss clause says outright that she does not know",
              "genuinely do not know" in miss)
        check("the miss clause forbids estimating a number",
              "do not estimate a temperature" in miss)
        check("the miss clause carries no invented reading",
              "degrees Fahrenheit" not in miss)
        check("a non-weather utterance still stays silent during an outage",
              clause_for("what time is it") == "")
        # OFF must NOT masquerade as BROKEN: switched off there is no feed to
        # report as failed, and saying otherwise would be a standing false
        # statement on every weather turn.
        import core.config as _cfg
        _was = getattr(_cfg, "WEATHER_ENABLED", True)
        try:
            _cfg.WEATHER_ENABLED = False
            check("switched OFF injects nothing, not a broken-feed clause",
                  clause_for("what's the weather doing out there") == "")
        finally:
            _cfg.WEATHER_ENABLED = _was
    finally:
        globals()["_API_URL"] = _saved_url
        _cache.clear(); _cache.update(_saved_cache)

    if live:
        _cache["data"] = None
        _cache["fail_t"] = 0.0
        t0 = time.time()
        cur = current(force=True)
        check("live: API returned a current block", isinstance(cur, dict))
        if isinstance(cur, dict):
            check("live: temperature present", "temperature_2m" in cur)
            print("       %s  ->  %s" % (round(time.time() - t0, 2),
                                         format_clause(cur)))

    print("\n%d/%d PASS" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if "--selftest" in sys.argv:
        sys.exit(_selftest(live="--live" in sys.argv))
    print("usage: python services/weather.py --selftest [--live]")
