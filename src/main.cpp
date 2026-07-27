// Define if you wish to debug memory usage.  Only works on T4.x
//#define DEBUG_MEMORY

#include <SPI.h>
#include <array>
#include <Wire.h>
#include <Entropy.h>

#include "config.h"
#include "mouth_tft.h"
#include "sleep_renderer.h"
#include "util/logging.h"
#include "sensors/LightSensor.h"

// Gaze sensor transport, selected by USE_SEN0626 / USE_PERSON_SENSOR_I2C in
// config.h (S212). Include exactly ONE: both headers typedef person_sensor_face_t
// as an anonymous struct, so pulling in both is a redefinition error. The
// GazeSensor alias lets the call sites below name Mode without hardcoding either
// class -- the two Mode enums share values but are distinct types.
#ifdef USE_SEN0626
#include "sensors/SEN0626Sensor.h"
using GazeSensor = SEN0626Sensor;
#else
#include "sensors/PersonSensor.h"
using GazeSensor = PersonSensor;
#endif

// RD-033 tracking debug — set to 1 to log per-read face state to Serial.
// DEFAULT OFF (0): one line per sensor read at ~14 Hz would fill journald.
// Compile with DEBUG_FACE=1, flash via scripts\flash_t41.ps1, then watch:
//   journalctl -u assistant -f | grep DBG-F
// or open a serial monitor on /dev/ttyIRIS_EYES at 115200.
#ifndef DEBUG_FACE
#define DEBUG_FACE 0
#endif

// RD-033 FIX (S133): the "noticed you" greet (RD-030 #3) is DISABLED in the
// face-acquisition path. mouthGreet() calls mouthTFTShow(5) — a full-screen SWSPI
// surprised-oval redraw (~300 ms blocking, +BOING phase redraws at ~300/600 ms) —
// SYNCHRONOUSLY inside reportFaceState(), immediately before setTargetPosition().
// That starves the eye-tracking loop at the exact moment a face is acquired, which
// is the operator-observed "eyes lock on, then drop/redirect after ~0.5 s". Gating
// the greet out of this path removes the stall. Set ENABLE_FACE_GREET=1 to restore
// the old blocking behavior for A/B testing. To bring the greet back for real,
// rework mouthGreet() to render non-blocking (incremental SWSPI chunks driven by
// mouthIdleTick), then re-enable.
#ifndef ENABLE_FACE_GREET
#define ENABLE_FACE_GREET 0
#endif

// ---------------------------------------------------------------------------
// JARVIS SERIAL BRIDGE CONFIG
// ---------------------------------------------------------------------------
// Serial (USB) is used to communicate with Pi4 assistant.py.
// Pi4 -> Teensy:  "EMOTION:HAPPY\n", "EMOTION:NEUTRAL\n", etc.
//                 "EYES:SLEEP\n"      -- blank both displays (black)
//                 "EYES:WAKE\n"       -- restore current eye definition
//                 "EYE:n\n"           -- switch default eye to index n (web UI)
// Teensy -> Pi4:  "FACE:1\n" (face locked), "FACE:0\n" (face lost)
//
// EYE INDEX MAP (matches eyeDefinitions in config.h):
//   0 = nordicBlue  (default)
//   1 = flame          (ANGRY -- managed by emotion system + web UI)
//   2 = hypnoRed       (CONFUSED -- managed by emotion system + web UI)
//   3 = hazel
//   4 = blueFlame1
//   5 = dragon
//   6 = strikingBlue
//   7 = cat            8 = doomSpiral*   9 = anime*       10 = doe*
//  11 = demon*        12 = skull        13 = leopard*     14 = toonstripe
//  15 = fizzgig       16 = newt         17 = snake        18 = fish
//  19 = brown         20 = bigBlue      21 = spikes       22 = firebox
//  23 = blueFlame2    24 = doomRed
//   (* = authored as an asymmetric {left, right} pair, not one shared eye)
//
// EYE:n selectable range: 0-24 (bounded by EYE_IDX_COUNT below)
//
// PERSON SENSOR: stock chrismiller behavior -- always active, eyes always
// track the largest detected face. autoMove resumes when no face is present.

static constexpr uint32_t FACE_LOST_TIMEOUT_MS =  5000;
static constexpr uint32_t FACE_COOLDOWN_MS      = 30000;
static constexpr uint32_t SERIAL_BUF_SIZE       =    40;  // SLEEP_CFG:mouthPulseAlpha=255 = 29 chars

// ANGRY eye swap: flame (index 1) held for this duration then auto-reverts
static constexpr uint32_t ANGRY_EYE_DURATION_MS   = 9000;
// CONFUSED eye swap: hypnoRed (index 2) held for this duration then auto-reverts
static constexpr uint32_t CONFUSED_EYE_DURATION_MS = 7000;

// Eye definition indices (matches eyeDefinitions array in config.h)
static constexpr uint32_t EYE_IDX_DEFAULT      = 0; // nordicBlue
static constexpr uint32_t EYE_IDX_ANGRY        = 1; // flame        (emotion swap + web UI)
static constexpr uint32_t EYE_IDX_CONFUSED     = 2; // hypnoRed     (emotion swap + web UI)
static constexpr uint32_t EYE_IDX_HAZEL        = 3; // web UI
static constexpr uint32_t EYE_IDX_BLUEFLAME1   = 4; // web UI
static constexpr uint32_t EYE_IDX_DRAGON       = 5; // web UI
static constexpr uint32_t EYE_IDX_STRIKINGBLUE = 6; // web UI (was 7; bigBlue removed)
// S241: indices 7-24 are the vendored library, selectable but deliberately NOT
// given firmware emotion defaults. Which texture means which emotion is DATA on
// the Pi (EMOTION_EYE_MAP), decided by the operator's taste pass -- only the two
// historical swaps below are baked, and they stay baked so ANGRY/CONFUSED work
// even with an empty map. Named constants stop at the emotion-bearing ones on
// purpose; the rest are addressed by number from the WebUI.
static constexpr uint32_t EYE_IDX_COUNT        = 25; // total entries in eyeDefinitions

// ---------------------------------------------------------------------------
// EMOTION -> EYE PARAMETER MAPPING
// ---------------------------------------------------------------------------

struct EmotionParams {
  float    pupilRatio;
  bool     doBlink;
  uint32_t maxGazeMs;
};

// S241: ANNOYED and EXASPERATED appended (never inserted) so every existing ID
// keeps its numeric value -- the [DBG] EMOTION cmd line prints the id and the
// S47/RD-002 AMUSED precedent added its tag the same way. Unknown tags degrade
// to NEUTRAL in parseEmotion below and in the Pi extractor, so this firmware and
// RD-066's modelfile half can land in either order without breaking anything.
enum EmotionID { NEUTRAL=0, HAPPY, CURIOUS, ANGRY, SLEEPY, SURPRISED, SAD, CONFUSED, AMUSED,
                 ANNOYED, EXASPERATED, EMOTION_COUNT };

static const EmotionParams emotionTable[EMOTION_COUNT] = {
  { 0.40f, false, 3000 }, // NEUTRAL
  { 0.75f, true,  1500 }, // HAPPY
  { 0.60f, false, 4000 }, // CURIOUS
  { 0.15f, false,  800 }, // ANGRY
  { 0.85f, true,  5000 }, // SLEEPY
  { 0.95f, true,   600 }, // SURPRISED
  { 0.25f, true,  4000 }, // SAD
  { 0.70f, true,  2000 }, // CONFUSED
  { 0.55f, false, 3000 }, // AMUSED
  // ANNOYED: the theatrical squint that rides a witty jab. doBlink=false on
  // purpose -- the squeeze IS the gesture and an immediate blink muddies it.
  // Blink RATE stays normal (the choreographer does not suppress it), which is
  // one of the things that keeps this readable as playful rather than as ANGRY.
  { 0.45f, false, 2500 }, // ANNOYED
  // EXASPERATED: the eye-roll. doBlink=false because the script fires its own
  // settle-back blink at the end of the arc; a blink at t=0 would hide the roll.
  { 0.50f, false, 3000 }, // EXASPERATED
};

// ── Per-emotion eye texture swap + auto-revert (S241) ────────────────────────
// Generalizes the two hard-coded ANGRY/CONFUSED revert timers into one table.
// eyeIdx < 0 means "no swap, keep whatever the user selected".
// The two live rows below reproduce the previous constants EXACTLY (flame @
// 9000 ms, hypnoRed @ 7000 ms) -- that equivalence is the acceptance test for
// this refactor (H6 in docs/S239_eyeset_continuum_audit.md), not a nicety.
// Deliberately NOT extended to the new indices: emotion-to-eye mapping is data
// on the Pi (EMOTION_EYE_MAP), which sends EYE:n before EMOTION:x.
struct EmotionEyeSwap {
  int16_t  eyeIdx;
  uint32_t revertMs;
};

static const EmotionEyeSwap emotionEyeSwap[EMOTION_COUNT] = {
  { -1, 0 },                                            // NEUTRAL
  { -1, 0 },                                            // HAPPY
  { -1, 0 },                                            // CURIOUS
  { (int16_t)EYE_IDX_ANGRY,    ANGRY_EYE_DURATION_MS }, // ANGRY
  { -1, 0 },                                            // SLEEPY
  { -1, 0 },                                            // SURPRISED
  { -1, 0 },                                            // SAD
  { (int16_t)EYE_IDX_CONFUSED, CONFUSED_EYE_DURATION_MS }, // CONFUSED
  { -1, 0 },                                            // AMUSED
  { -1, 0 },                                            // ANNOYED  (no flame swap: that is what makes it read playful, not hostile)
  { -1, 0 },                                            // EXASPERATED
};

// ---------------------------------------------------------------------------
// STATE
// ---------------------------------------------------------------------------

// userDefaultEye tracks the web UI selection -- the eye to revert to after
// emotion eye swaps end. Starts at nordicBlue, updated by EYE:n command.
static uint32_t userDefaultEye{EYE_IDX_DEFAULT};
static uint32_t defIndex{EYE_IDX_DEFAULT};

LightSensor  lightSensor(LIGHT_PIN);
#ifdef USE_SEN0626
GazeSensor    personSensor(SEN0626_SERIAL);  // DFRobot SEN0626 — Modbus RTU over UART (Serial4 = RX 16 / TX 17)
#else
GazeSensor    personSensor(Wire);            // Useful Sensors SEN-21231 — I2C 0x62 (SDA 18 / SCL 19)
#endif
bool         personSensorFound = USE_PERSON_SENSOR;

static bool     faceWasPresent  = false;
static uint32_t lastFace1SentMs = 0;

static char    serialBuf[SERIAL_BUF_SIZE];
static uint8_t serialBufLen = 0;

// Emotion eye-swap revert timer (S241). One generic slot replaces the separate
// angry/confused pairs; which texture and how long now come from emotionEyeSwap.
static bool     swapEyeActive   = false;
static uint32_t swapEyeStartMs  = 0;
static uint32_t swapEyeRevertMs = 0;

// Eyes sleep state: when true, displays are blanked and renderFrame is skipped
static bool     eyesSleeping = false;

// Eyes speaking state: when true, Person Sensor position updates are suppressed
// and autoMove is active, giving smooth wandering during TTS instead of jitter.
static bool     eyesSpeaking = false;

// ── Runtime-tunable Person Sensor config (PS_CFG: serial commands, S141) ──────
// Defaults match the previous compile-time constants so behavior is unchanged
// until the operator tunes via the WebUI. A Teensy reboot reverts these to the
// defaults below; assistant.py re-sends the saved values on serial open
// (mirrors the SLEEP_CFG startup push) so tuning survives a power cycle.
static uint8_t  psConfGate       = 45;                    // PS_CFG:CONF=n   box_confidence gate. S153c: default 60→45 (S150c known-good).
static bool     psFacingRequired = false;                 // PS_CFG:FACING=0/1  require face.is_facing. S153c: default true→FALSE — the is_facing bit flickers with normal head movement, disqualifying the face → eyes drop the lock → autoMove (random gaze) resumes. S150c proved FACING=0 is the durable fix; baking it as the firmware default makes tracking correct AUTONOMOUSLY on power-up instead of depending on the Pi4 pushing ps_config.json after boot.
static bool     psLedEnabled     = false;                 // PS_CFG:LED=0/1  on-sensor LED indicator. S185: default FALSE, matching the operator's actual saved preference (ps_config.json LED:0) — same "bake the known-good value as the autonomous default" pattern as CONF/FACING above. S153's TRUE default fought that preference on every Teensy reset (power cycle, brownout, or a soft reset that doesn't trigger a USB reconnect the Pi4 can detect and correct), which is why the LED kept relighting "no matter the WebUI setting" (RD-040 follow-up). enableLED() is still called live on every PS_CFG:LED command and at (re)detection, so turning it ON when wanted still works exactly as before.
static uint32_t psLostMs         = FACE_LOST_TIMEOUT_MS;  // PS_CFG:LOST_MS=n  autoMove resume delay
static float    psYBias          = 0.0f;                  // PS_CFG:Y_BIAS=f  additive Y target offset
// ── S212c gaze shaping: signed gain + bias per axis ──────────────────────────
// targetN = rawN * gain + bias, where rawN is the sensor-space target in -1..+1.
// The SIGN of the gain is the direction and the MAGNITUDE is the range, so one
// knob covers both "it's mirrored" and "it barely moves".
//   X_GAIN default -1.0 reproduces the historical negation EXACTLY, so the Person
//   Sensor rollback path is bit-identical at defaults. Flip to +1.0 to un-mirror.
//   |gain| > 1 amplifies: at a close conversational distance (~18-24 in) a normal
//   head movement only crosses a fraction of the sensor's 85 deg FOV, so raw
//   deflection is small (~0.4 of full) and the eyes barely move. Gain 2.0-2.5 makes
//   that read as real gaze. EyeController::constrainEyeCoord() normalises (x,y) to
//   the unit circle, so over-gain saturates gracefully instead of clipping wrong.
// X_GAIN default is TRANSPORT-CONDITIONAL, following the project's established
// "bake the known-good value as the autonomous default" pattern (S153c CONF/FACING,
// S185 LED): the sensor should be correct on power-up without depending on the Pi4
// pushing ps_config.json after boot. The two parts report X in OPPOSITE conventions.
// Operator-observed on the S212b bench (2026-07-17): with the historical -1.0 the
// SEN0626 tracked MIRRORED (eyes went the wrong way relative to real head movement).
// The Person Sensor's -1.0 is field-proven over many sessions and is left untouched
// on the rollback path.
#ifdef USE_SEN0626
static float    psXGain          =  1.0f;                 // PS_CFG:X_GAIN=f  SEN0626: un-mirrored (operator-observed S212b, NOT instrument-measured)
#else
static float    psXGain          = -1.0f;                 // PS_CFG:X_GAIN=f  Person Sensor: historical negation, field-proven
#endif
static float    psYGain          =  1.0f;                 // PS_CFG:Y_GAIN=f  signed: sign = U/D direction, magnitude = U/D range
static float    psXBias          =  0.0f;                 // PS_CFG:X_BIAS=f  additive X target offset (horizontal centering)

// Mouth sleep frame throttle — min interval between frames to prevent flicker
static uint32_t srMouthLastMs = 0;
static constexpr uint32_t MOUTH_SLEEP_FRAME_MS = 60;

// Idle animation auto-start: trigger after this many ms of no serial commands
static uint32_t lastCommandMs = 0;
static constexpr uint32_t IDLE_AUTO_MS = 120000UL; // 2 min inactivity

// Decouple mouth TFT (blocking SWSPI) from the eye render loop.
// MOUTH: commands queue here; rendered after eyes->renderFrame(), rate-limited
// so TTS mouth animation never stalls the eye loop.
static uint8_t  pendingMouthIdx    = 0;
static bool     mouthUpdatePending = false;
static uint32_t lastMouthRenderMs  = 0;
static constexpr uint32_t MOUTH_RENDER_MIN_MS = 55;  // S186: dirty-rect redraws are cheaper, so bursts can render a touch more often without stalling the eye loop

// MOUTHGEST (S144): non-blocking gesture acknowledgment. Shows the SILLY face
// (idx 9) for MOUTH_GESTURE_MS, then auto-restores to NEUTRAL. 0 = inactive.
static uint32_t mouthGestureRestoreMs = 0;
static constexpr uint32_t MOUTH_GESTURE_MS = 550;

// ---------------------------------------------------------------------------
// HELPERS
// ---------------------------------------------------------------------------

bool hasBlinkButton() { return BLINK_PIN >= 0; }
bool hasLightSensor() { return LIGHT_PIN >= 0; }
bool hasJoystick()    { return JOYSTICK_X_PIN >= 0 && JOYSTICK_Y_PIN >= 0; }
bool hasPersonSensor(){ return personSensorFound; }

static void setEyeDefinition(uint32_t idx) {
  if (idx != defIndex) {
    defIndex = idx;
    eyes->updateDefinitions(eyeDefinitions[defIndex]);
  }
}

static void blankDisplays() {
#ifdef USE_GC9A01A
  if (displayLeft)  displayLeft->fillBlack();
  if (displayRight) displayRight->fillBlack();
#endif
  Serial.println("[DBG] EYES:SLEEP -- displays blanked");
}

static EmotionID parseEmotion(const char *name) {
  if (strcmp(name, "HAPPY")     == 0) return HAPPY;
  if (strcmp(name, "CURIOUS")   == 0) return CURIOUS;
  if (strcmp(name, "ANGRY")     == 0) return ANGRY;
  if (strcmp(name, "SLEEPY")    == 0) return SLEEPY;
  if (strcmp(name, "SURPRISED") == 0) return SURPRISED;
  if (strcmp(name, "SAD")       == 0) return SAD;
  if (strcmp(name, "CONFUSED")  == 0) return CONFUSED;
  if (strcmp(name, "AMUSED")    == 0) return AMUSED;
  if (strcmp(name, "ANNOYED")     == 0) return ANNOYED;      // S241
  if (strcmp(name, "EXASPERATED") == 0) return EXASPERATED;  // S241
  return NEUTRAL;
}

// ---------------------------------------------------------------------------
// LID CHOREOGRAPHER (S241, RD-067 / RD-066 item 3)
// ---------------------------------------------------------------------------
// Human observers read eyelid emotion from TIMING and COORDINATION, not from
// static positions: surprise is the SPEED of the upper-lid raise, a genuine
// smile narrows the eye from the LOWER lid, attention SUPPRESSES blinking,
// drowsiness SLOWS blink closure. So this drives an envelope over time rather
// than parking the lids at a per-emotion pose.
//
// Everything here uses the EXISTING public EyeController API. EyeController.h
// is a protected file and is NOT touched.
//
// Three gates, all checked by reading EyeController.h this session:
//  1. COMPOSITION: renderEye() applies blink as a MULTIPLIER over the damped
//     openness (`upperF = upperFactor * (1.0f - blinkFactor)`, EyeController.h
//     :309-310) while renderFrame() takes the override in place of
//     computeEyelids() (:581-588). Override and blink therefore COMPOSE -- a
//     blink still fully closes a scripted lid. No fallback needed.
//  2. SLEW RATE: the built-in damping is an IIR (`f = f*0.7 + target*0.3`,
//     :306-307) applied per eye per renderEye, and renderFrame renders one eye
//     per call, so a step reaches ~90% in ~13 renderFrame calls -- too slow for
//     a sub-150 ms snap on its own. Rather than edit the protected damping
//     constant, snapIn below uses clearLidOverride(), which SNAPS every eye's
//     stored lid factors to 1.0 outright (:504-508), and then immediately
//     re-arms the override at the target. Exact for a fully-open first key,
//     which is precisely what SURPRISED needs. No protected-file edit earned.
//  3. COST: one setLidOpenness() per loop is two float assignments plus a
//     constrain; the interpolation below is a handful of adds and multiplies.
//
// Idempotence: re-sending the SAME emotion while its script is still running
// does NOT restart the envelope (see lidScriptStart). Journal evidence this
// session shows one EMOTION per reply, but a restart-on-repeat would be a
// latent bug the moment that changes.
//
// Lid semantics: 1.0 = fully open, 0.0 = fully closed. Raising the LOWER lid
// (the Duchenne cheek push) therefore means a SMALLER lower value.

struct LidKey  { uint16_t tMs; float upper; float lower; };
struct GazeKey { uint16_t tMs; float x; float y; uint16_t moveMs; };

struct LidScript {
  const LidKey  *lids;
  uint8_t        lidCount;
  const GazeKey *gaze;        // nullptr unless the expression moves the eyes
  uint8_t        gazeCount;
  uint16_t       durationMs;  // total length; lids release at the end
  bool           snapIn;      // instant first key (fully-open keys only)
  bool           suppressBlink;
  uint16_t       blinkAtMs;   // 0 = none; one blink() fired at this offset
};

// SURPRISED: the snap IS the expression. Full wide instantly, hold, then settle
// to slightly-wide rather than all the way back.
static const LidKey LK_SURPRISED[] = {
  {   0, 1.00f, 1.00f }, { 700, 1.00f, 1.00f }, { 950, 0.85f, 0.80f },
};
// HAPPY: upper relaxed, LOWER lid raises -- the squint of a genuine smile. The
// closure at 1500-2100 ms is a deliberate slow warm blink (the cat "I like you"
// blink kids read instinctively), scripted as an envelope rather than blink()
// because closure SPEED is the whole character of it and blink() is fixed at
// 50-100 ms. That is also why auto-blink is suppressed across this script.
static const LidKey LK_HAPPY[] = {
  {   0, 0.95f, 1.00f }, { 350, 0.95f, 0.65f }, { 1500, 0.95f, 0.65f },
  {1800, 0.10f, 0.65f }, { 2100, 0.95f, 0.65f }, { 2900, 0.95f, 0.65f },
  {3300, 1.00f, 0.90f },
};
// AMUSED: the same Duchenne raise, pulsing at laugh rhythm. This is the S238
// "laughing eyes" ask done through lid dynamics instead of a render-loop hack.
static const LidKey LK_AMUSED[] = {
  {   0, 0.95f, 1.00f }, { 300, 0.95f, 0.62f }, {  600, 0.95f, 0.80f },
  { 900, 0.95f, 0.62f }, {1200, 0.95f, 0.80f }, { 1500, 0.95f, 0.62f },
  {1900, 1.00f, 0.90f },
};
// CURIOUS: slight widen and, mostly, NOT blinking at you. Attention is the
// suppression; it costs nothing and reads immediately.
static const LidKey LK_CURIOUS[] = {
  {   0, 1.00f, 1.00f }, { 300, 1.00f, 0.95f }, { 3500, 1.00f, 0.95f },
};
// ANGRY: upper lid DROPS and lower tightens up -- the glare. Blinks rare and
// sharp, so auto-blink is off and exactly one blink() fires mid-hold.
static const LidKey LK_ANGRY[] = {
  {   0, 0.95f, 1.00f }, { 250, 0.55f, 0.70f }, { 4000, 0.55f, 0.70f },
  {4400, 0.90f, 0.90f },
};
// ANNOYED: symmetric partial squeeze, both lids toward each other, held a beat,
// then a QUICK release back to the warm baseline. Deliberately distinct from
// ANGRY: symmetric rather than an upper-lid glare, no eye swap, normal blink
// rate, and short. The playfulness lives in the release -- a squint that lets
// go is a joke, a squint that holds is a threat.
static const LidKey LK_ANNOYED[] = {
  {   0, 0.95f, 1.00f }, { 180, 0.60f, 0.55f }, { 620, 0.60f, 0.55f },
  { 780, 1.00f, 0.95f },
};
// SLEEPY: heavy droop, and the blinks are slow CLOSURES rather than blink()
// calls -- 600 ms down and 600 ms up is what makes it read as drowsy.
static const LidKey LK_SLEEPY[] = {
  {   0, 0.90f, 1.00f }, { 600, 0.40f, 0.85f }, { 1800, 0.40f, 0.85f },
  {2400, 0.12f, 0.85f }, {3000, 0.40f, 0.85f }, { 4600, 0.40f, 0.85f },
  {5200, 0.10f, 0.85f }, {5800, 0.40f, 0.85f },
};
// SAD: lids heavy but less collapsed than SLEEPY, with one slow blink.
static const LidKey LK_SAD[] = {
  {   0, 0.90f, 1.00f }, { 700, 0.50f, 0.95f }, { 2600, 0.50f, 0.95f },
  {3200, 0.20f, 0.95f }, {3900, 0.50f, 0.95f }, { 5200, 0.50f, 0.95f },
};
// CONFUSED: lids uneven-ish over time (a controller-global override cannot do
// per-eye asymmetry -- that needs a baked asymmetric definition, which indices
// 8/9/10/11/13 now provide for a later taste pass), so the "what?" reads as a
// slow uncertain drift plus a widen.
static const LidKey LK_CONFUSED[] = {
  {   0, 0.95f, 1.00f }, { 400, 0.80f, 0.85f }, { 1400, 1.00f, 0.90f },
  {2400, 0.80f, 0.85f }, {3400, 0.95f, 0.95f },
};
// EXASPERATED: the eye-roll. Brisk is a CORRECTNESS requirement, not taste -- a
// fast roll is comedy, a slow roll is contempt. Lids ride at ~0.7 through the
// arc and a settle-back blink lands at the end.
static const LidKey LK_EXASPERATED[] = {
  {   0, 0.95f, 1.00f }, { 120, 0.70f, 0.80f }, { 700, 0.70f, 0.80f },
  { 820, 0.95f, 0.95f },
};
// Up, across, down and back: ~750 ms of travel. setTargetPosition() takes its
// own move duration, so each leg is eased by the engine rather than stepped.
static const GazeKey GK_EXASPERATED[] = {
  {   0,  0.00f,  0.90f, 220 }, { 230,  0.85f,  0.55f, 230 },
  { 470,  0.30f, -0.25f, 240 }, { 720,  0.00f,  0.00f, 200 },
};

// NEUTRAL and the two texture-swap emotions with no lid character get a null
// script, which releases any override and hands the lids back to tracking.
static const LidScript lidScripts[EMOTION_COUNT] = {
  { nullptr,         0, nullptr, 0,    0, false, false,    0 }, // NEUTRAL
  { LK_HAPPY,        7, nullptr, 0, 3500, false, true,     0 }, // HAPPY
  { LK_CURIOUS,      3, nullptr, 0, 3700, false, true,     0 }, // CURIOUS
  { LK_ANGRY,        4, nullptr, 0, 4600, false, true,  2200 }, // ANGRY
  { LK_SLEEPY,       8, nullptr, 0, 6000, false, true,     0 }, // SLEEPY
  { LK_SURPRISED,    3, nullptr, 0, 1200, true,  false,    0 }, // SURPRISED
  { LK_SAD,          6, nullptr, 0, 5400, false, true,     0 }, // SAD
  { LK_CONFUSED,     5, nullptr, 0, 3600, false, false,    0 }, // CONFUSED
  { LK_AMUSED,       7, nullptr, 0, 2100, false, true,     0 }, // AMUSED
  { LK_ANNOYED,      4, nullptr, 0,  900, false, false,    0 }, // ANNOYED
  { LK_EXASPERATED,  4, GK_EXASPERATED, 4, 950, false, true, 780 }, // EXASPERATED
};

static const LidScript *lidActive       = nullptr;
static EmotionID        lidActiveId     = NEUTRAL;
static uint32_t         lidStartMs      = 0;
static bool             lidBlinkFired   = false;
static uint8_t          lidGazeNext     = 0;
static bool             lidSavedAutoBlink = true;
static bool             lidSavedAutoMove  = true;
static bool             lidTookGaze     = false;

// Release the lids back to normal tracking and restore whatever autoBlink /
// autoMove were before the script took them.
static void lidScriptRelease() {
  if (!lidActive) return;
  eyes->clearLidOverride();
  if (lidActive->suppressBlink) eyes->setAutoBlink(lidSavedAutoBlink);
  if (lidTookGaze)              eyes->setAutoMove(lidSavedAutoMove);
  lidActive   = nullptr;
  lidTookGaze = false;
}

static void lidScriptStart(EmotionID id) {
  const LidScript *s = &lidScripts[id];

  // Idempotent on a repeat of the SAME emotion: never restart a running
  // envelope, or a re-sent tag would visibly stutter the expression.
  if (lidActive == s && lidActiveId == id && s->lids != nullptr) return;

  lidScriptRelease();
  if (s->lids == nullptr || s->lidCount == 0) return;

  lidActive     = s;
  lidActiveId   = id;
  lidStartMs    = millis();
  lidBlinkFired = false;
  lidGazeNext   = 0;

  if (s->suppressBlink) {
    lidSavedAutoBlink = eyes->autoBlinkEnabled();
    eyes->setAutoBlink(false);
  }
  if (s->gaze != nullptr && s->gazeCount > 0) {
    lidSavedAutoMove = eyes->autoMoveEnabled();
    eyes->setAutoMove(false);   // autoMove would fight a scripted gaze arc
    lidTookGaze = true;
  }

  // A fully-open first key can bypass the engine's lid damping outright:
  // clearLidOverride() snaps the stored factors to 1.0, then re-arming the
  // override at 1.0 leaves the damping filter already at its target.
  if (s->snapIn) {
    eyes->clearLidOverride();
  }
  eyes->setLidOpenness(s->lids[0].upper, s->lids[0].lower);
}

// Drive the active envelope. Called once per loop, before renderFrame().
static void lidScriptTick() {
  if (!lidActive) return;

  const uint32_t t = millis() - lidStartMs;
  if (t >= lidActive->durationMs) { lidScriptRelease(); return; }

  const LidKey *k = lidActive->lids;
  const uint8_t n = lidActive->lidCount;

  float upper = k[n - 1].upper;
  float lower = k[n - 1].lower;
  if (t <= k[0].tMs) {
    upper = k[0].upper;
    lower = k[0].lower;
  } else {
    for (uint8_t i = 1; i < n; i++) {
      if (t <= k[i].tMs) {
        const uint32_t span = (uint32_t)(k[i].tMs - k[i - 1].tMs);
        const float    f    = span ? (float)(t - k[i - 1].tMs) / (float)span : 1.0f;
        upper = k[i - 1].upper + (k[i].upper - k[i - 1].upper) * f;
        lower = k[i - 1].lower + (k[i].lower - k[i - 1].lower) * f;
        break;
      }
    }
  }
  eyes->setLidOpenness(upper, lower);

  // Scripted gaze legs (the eye-roll). eyesSpeaking already suppresses Person
  // Sensor position updates during TTS (see the tracking block in loop()), and
  // autoMove was taken at script start, so nothing competes for the target.
  if (lidActive->gaze != nullptr) {
    while (lidGazeNext < lidActive->gazeCount &&
           t >= lidActive->gaze[lidGazeNext].tMs) {
      const GazeKey &g = lidActive->gaze[lidGazeNext];
      eyes->setTargetPosition(g.x, g.y, g.moveMs);
      lidGazeNext++;
    }
  }

  if (lidActive->blinkAtMs && !lidBlinkFired && t >= lidActive->blinkAtMs) {
    eyes->blink();
    lidBlinkFired = true;
  }
}

static void applyEmotion(EmotionID id) {
  const EmotionParams &p = emotionTable[id];
  eyes->setTargetPupil(p.pupilRatio, 300);
  eyes->setMaxGazeMs(p.maxGazeMs);
  if (p.doBlink) eyes->blink();

  // S241: table-driven texture swap. Behaviour is identical to the previous
  // hard-coded if/else-if/else -- ANGRY takes flame for 9 s, CONFUSED takes
  // hypnoRed for 7 s, each cancels the other, and any other emotion reverts a
  // live swap to the user's selected eye.
  const EmotionEyeSwap &sw = emotionEyeSwap[id];
  if (sw.eyeIdx >= 0) {
    setEyeDefinition((uint32_t)sw.eyeIdx);
    swapEyeActive   = true;
    swapEyeStartMs  = millis();
    swapEyeRevertMs = sw.revertMs;
  } else if (swapEyeActive) {
    setEyeDefinition(userDefaultEye);
    swapEyeActive = false;
  }

  // S241: hand the lids their envelope for this emotion. NEUTRAL has a null
  // script, so it releases any override and returns the lids to tracking.
  lidScriptStart(id);
}

static void processSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (serialBufLen > 0) {
        serialBuf[serialBufLen] = '\0';
        serialBufLen = 0;

        if (strncmp(serialBuf, "EMOTION:", 8) == 0) {
          mouthIdleStop();
          lastCommandMs = millis();
          EmotionID id = parseEmotion(serialBuf + 8);
          Serial.print("[DBG] EMOTION cmd: ");
          Serial.print(serialBuf + 8);
          Serial.print(" -> id=");
          Serial.println(id);
          applyEmotion(id);

        } else if (strncmp(serialBuf, "EYE:", 4) == 0) {
          lastCommandMs = millis();
          uint32_t idx = (uint32_t)atoi(serialBuf + 4);
          if (idx < EYE_IDX_COUNT) {
            userDefaultEye = idx;
            if (!swapEyeActive) {
              setEyeDefinition(idx);
            }
            Serial.print("[DBG] EYE cmd: switched default to index ");
            Serial.println(idx);
          } else {
            Serial.print("[DBG] EYE cmd: invalid index ");
            Serial.println(idx);
          }

        } else if (strcmp(serialBuf, "EYES:SLEEP") == 0) {
          mouthIdleStop();
          lastCommandMs = millis();
          if (!eyesSleeping) {
            eyesSleeping      = true;
            swapEyeActive     = false;
            lidScriptRelease();   // S241: never sleep holding a lid override
            // Disable changed-areas-only so the sleep renderer always sends full
            // frames. With updateChangedAreasOnly(true), fillScreen(black) on an
            // already-black framebuffer marks zero dirty areas, causing the SPI
            // DMA to receive an empty/corrupt region set and lock up the Teensy.
            if (displayLeft)  displayLeft->getDriver()->updateChangedAreasOnly(false);
            if (displayRight) displayRight->getDriver()->updateChangedAreasOnly(false);
            // blankDisplays() drains any pending eye-engine DMA before the starfield
            // renderer takes over. Skipping it risks a DMA race on the first fillScreen.
            blankDisplays();
            mouthSetSleepIntensity();
            sleepRendererInit();
            Serial.println("[DBG] EYES:SLEEP -- starfield starting");
          }

        } else if (strncmp(serialBuf, "MOUTH:", 6) == 0) {
          mouthIdleStop();
          lastCommandMs      = millis();
          pendingMouthIdx    = (uint8_t)atoi(serialBuf + 6);
          mouthUpdatePending = true;

        } else if (strncmp(serialBuf, "MOUTH_INTENSITY:", 16) == 0) {
          mouthIdleStop();
          lastCommandMs = millis();
          uint8_t lvl = (uint8_t)constrain(atoi(serialBuf + 16), 0, 15);
          mouthSetIntensity(lvl);
          Serial.print("[DBG] MOUTH_INTENSITY: ");
          Serial.println(lvl);

        } else if (strcmp(serialBuf, "MOUTHGEST") == 0) {
          // Gesture acknowledgment glyph: SILLY face now, auto-restore to
          // NEUTRAL after MOUTH_GESTURE_MS (handled non-blocking in loop()).
          mouthIdleStop();
          lastCommandMs         = millis();
          pendingMouthIdx       = 9;            // SILLY
          mouthUpdatePending    = true;
          mouthGestureRestoreMs = millis() + MOUTH_GESTURE_MS;
          Serial.println("[DBG] MOUTHGEST");

        } else if (strcmp(serialBuf, "EYES:WAKE") == 0) {
          mouthIdleStop();
          lastCommandMs = millis();
          if (eyesSleeping) {
            eyesSleeping = false;
            // Restore changed-areas-only for efficient eye engine rendering.
            if (displayLeft)  displayLeft->getDriver()->updateChangedAreasOnly(true);
            if (displayRight) displayRight->getDriver()->updateChangedAreasOnly(true);
            mouthSleepReset();
            mouthRestoreIntensity();
            uint32_t saved = defIndex;
            defIndex = UINT32_MAX;
            setEyeDefinition(saved);
            applyEmotion(NEUTRAL);
            Serial.println("[DBG] EYES:WAKE -- displays restored");
          }

        } else if (strcmp(serialBuf, "EYES:SPEAKING") == 0) {
          lastCommandMs = millis();
          eyesSpeaking  = true;
          Serial.println("[DBG] EYES:SPEAKING -- tracking frozen at last position");

        } else if (strcmp(serialBuf, "EYES:SPEAKING:STOP") == 0) {
          lastCommandMs = millis();
          eyesSpeaking  = false;
          Serial.println("[DBG] EYES:SPEAKING:STOP -- tracking resumed");

        } else if (strcmp(serialBuf, "VERSION") == 0) {
          Serial.print("[VER] IRIS-EYES firmware=");
          Serial.print(FIRMWARE_VERSION);
          Serial.print(" built=");
          Serial.print(__DATE__);
          Serial.print(" proto=");
          Serial.println(PROTOCOL_VERSION);

        } else if (strcmp(serialBuf, "IDLE:START") == 0) {
          lastCommandMs = 0; // force auto-start timer to treat this as immediate
          mouthIdleStart();
          Serial.println("[DBG] IDLE:START");

        } else if (strcmp(serialBuf, "IDLE:STOP") == 0) {
          mouthIdleStop();
          lastCommandMs = millis();
          Serial.println("[DBG] IDLE:STOP");

        } else if (strncmp(serialBuf, "SLEEP_CFG:", 10) == 0) {
          char* eq = strchr(serialBuf + 10, '=');
          if (eq) {
            *eq = '\0';
            const char* key = serialBuf + 10;
            float val = atof(eq + 1);
            if      (strcmp(key, "speed")          == 0) sleepCfg.speed          = val;
            else if (strcmp(key, "starBrightMin")  == 0) sleepCfg.starBrightMin  = (uint8_t)val;
            else if (strcmp(key, "starBrightMax")  == 0) sleepCfg.starBrightMax  = (uint8_t)val;
            else if (strcmp(key, "starTwinkleAmp") == 0) sleepCfg.starTwinkleAmp = (uint8_t)val;
            else if (strcmp(key, "shootCount")     == 0) sleepCfg.shootCount     = (uint8_t)val;
            else if (strcmp(key, "shootSpeed")     == 0) sleepCfg.shootSpeed     = (uint8_t)val;
            else if (strcmp(key, "shootLen")       == 0) sleepCfg.shootLen       = (uint8_t)val;
            else if (strcmp(key, "shootBright")    == 0) sleepCfg.shootBright    = (uint8_t)val;
            else if (strcmp(key, "warpCount")      == 0) sleepCfg.warpCount      = (uint8_t)val;
            else if (strcmp(key, "warpSpeed")      == 0) sleepCfg.warpSpeed      = (uint8_t)val;
            else if (strcmp(key, "warpBright")     == 0) sleepCfg.warpBright     = (uint8_t)val;
            else if (strcmp(key, "moonR")          == 0) sleepCfg.moonR          = (uint8_t)val;
            else if (strcmp(key, "moonDrift")      == 0) sleepCfg.moonDrift      = (uint8_t)val;
            else if (strcmp(key, "saturnR")        == 0) sleepCfg.saturnR        = (uint8_t)val;
            else if (strcmp(key, "saturnDrift")    == 0) sleepCfg.saturnDrift    = (uint8_t)val;
            else if (strcmp(key, "nebulaAlpha")    == 0) sleepCfg.nebulaAlpha    = (uint8_t)val;
            else if (strcmp(key, "waveAmp0")       == 0) sleepCfg.waveAmp0       = (uint8_t)val;
            else if (strcmp(key, "waveAmp1")       == 0) sleepCfg.waveAmp1       = (uint8_t)val;
            else if (strcmp(key, "waveAmp2")       == 0) sleepCfg.waveAmp2       = (uint8_t)val;
            else if (strcmp(key, "waveOscAmp")     == 0) sleepCfg.waveOscAmp     = (uint8_t)val;
            else if (strcmp(key,"mouthPulseAlpha") == 0) sleepCfg.mouthPulseAlpha= (uint8_t)val;
            else if (strcmp(key, "zzzAlpha0")      == 0) sleepCfg.zzzAlpha0      = (uint8_t)val;
            else if (strcmp(key, "zzzAlpha1")      == 0) sleepCfg.zzzAlpha1      = (uint8_t)val;
            else if (strcmp(key, "zzzAlpha2")      == 0) sleepCfg.zzzAlpha2      = (uint8_t)val;
          }

        } else if (strncmp(serialBuf, "PS_CFG:", 7) == 0) {
          // Runtime Person Sensor tuning (S141). KEY=value, mirrors SLEEP_CFG.
          char* eq = strchr(serialBuf + 7, '=');
          if (eq) {
            *eq = '\0';
            const char* key = serialBuf + 7;
            float val = atof(eq + 1);
            bool  known = true;   // S212c: gates the ack below to implemented keys only
            if      (strcmp(key, "CONF")    == 0) psConfGate       = (uint8_t)constrain((int)val, 0, 100);
            else if (strcmp(key, "FACING")  == 0) psFacingRequired = (val != 0.0f);
            else if (strcmp(key, "LOST_MS") == 0) psLostMs         = (uint32_t)val;
            else if (strcmp(key, "Y_BIAS")  == 0) psYBias          = val;
            else if (strcmp(key, "X_GAIN")  == 0) psXGain          = val;   // S212c
            else if (strcmp(key, "Y_GAIN")  == 0) psYGain          = val;   // S212c
            else if (strcmp(key, "X_BIAS")  == 0) psXBias          = val;   // S212c
            else if (strcmp(key, "LED")     == 0) { psLedEnabled = (val != 0.0f);
                                                    if (hasPersonSensor()) personSensor.enableLED(psLedEnabled); }
            else                                    known = false;
            // S212c: only ack keys this firmware ACTUALLY implements. The ack print
            // used to sit after the chain with no else, so an unimplemented key acked
            // identically to a real one: S212b cheerfully echoed "PS_CFG X_GAIN=1.0"
            // while having no psXGain at all. That is a false confirmation in the
            // WebUI, and worse, iris_post.py's CFG-DRIFT check compares saved config
            // against these acks, so an unimplemented key reported NO drift. An
            // UNKNOWN line is deliberately worded so it does NOT match iris_post.py's
            // ack regex '\[DBG\] PS_CFG (\w+)=(\S+)'.
            if (known) {
              Serial.print("[DBG] PS_CFG ");
              Serial.print(key); Serial.print("="); Serial.println(eq + 1);
            } else {
              Serial.print("[DBG] PS_CFG UNKNOWN key "); Serial.println(key);
            }
          }
        }
      }
    } else {
      if (serialBufLen < SERIAL_BUF_SIZE - 1) {
        serialBuf[serialBufLen++] = c;
      }
    }
  }
}

static void reportFaceState(bool facePresent) {
  uint32_t now = millis();
  if (facePresent && !faceWasPresent) {
    // S212c: log EVERY acquisition. FACE_COOLDOWN_MS (30 s) used to gate this
    // println as well as the greet, so FACE:1 was rate-limited to once per 30 s
    // while FACE:0 fired on every single loss. /api/ps/status decides "tracking" by
    // comparing the newest FACE:1 against the newest FACE:0, so any re-acquisition
    // inside the cooldown was invisible and the Vision Cal card sat on "no face in
    // view" while the eyes were actually locked. Live evidence (S212b, 30k lines):
    // FACE:1 x6 vs FACE:0 x13. The cooldown exists to rate-limit the GREET (an
    // expensive blocking redraw), not a log line, so it now gates only the greet.
    // Still bounded (one line per real transition) and symmetric with FACE:0, which
    // was never rate-limited.
    Serial.println("FACE:1");
    if ((now - lastFace1SentMs) >= FACE_COOLDOWN_MS) {
      lastFace1SentMs = now;
      // RD-030 #3 greet — DISABLED in the tracking path by default (RD-033 S133):
      // mouthGreet()'s synchronous SWSPI redraw blocked the loop here at acquisition,
      // starving eye tracking (~0.5 s drop). See the ENABLE_FACE_GREET note up top.
#if ENABLE_FACE_GREET
  #if DEBUG_FACE
      { uint32_t _t0 = millis(); mouthGreet();
        Serial.print("[DBG-F] greet block_ms="); Serial.println(millis() - _t0); }
  #else
      mouthGreet();
  #endif
#endif
    }
    faceWasPresent = true;
  } else if (!facePresent && faceWasPresent) {
    Serial.println("FACE:0");
    faceWasPresent = false;
  }
}

// ── I2C bus recovery (S153b) ─────────────────────────────────────────────────
// Person Sensor lives on the Teensy 4.1 default Wire bus: SDA=18, SCL=19.
// On a SIMULTANEOUS power-up the Teensy can probe the bus while the SEN-21231 is
// still loading its ML model; the sensor then holds SDA low (clock-stretch / latch-up)
// and the bus stays wedged. Retrying isPresent() alone CANNOT clear this — the master
// must manually clock SCL to flush the stuck device, issue a STOP, then re-init Wire.
// This is the step the S152 retry path was missing (it just re-probed a hung bus, which
// is why "reseat fixes it, power-cycle doesn't": a reseat physically releases SDA).
// S212: compiled ONLY on the I2C rollback path. UART has no SDA-latch failure
// mode, so there is nothing for this to recover on the SEN0626 build — but it is
// deliberately kept in-tree (not deleted) so re-landing the Person Sensor is a
// config.h toggle + reflash. See 09_IRIS_INTEGRATION_PLAN.md sections 3 and 7.
#ifndef USE_SEN0626
static constexpr uint8_t PS_SDA_PIN = 18;
static constexpr uint8_t PS_SCL_PIN = 19;

static void psI2cBusRecover() {
  Wire.end();
  pinMode(PS_SCL_PIN, OUTPUT);
  pinMode(PS_SDA_PIN, INPUT_PULLUP);
  // Up to 9 clock pulses to walk a stuck byte out of a slave holding SDA low.
  for (uint8_t i = 0; i < 9 && digitalRead(PS_SDA_PIN) == LOW; i++) {
    digitalWrite(PS_SCL_PIN, LOW);  delayMicroseconds(5);
    digitalWrite(PS_SCL_PIN, HIGH); delayMicroseconds(5);
  }
  // STOP condition: SDA low->high while SCL is high.
  pinMode(PS_SDA_PIN, OUTPUT);
  digitalWrite(PS_SDA_PIN, LOW);  delayMicroseconds(5);
  digitalWrite(PS_SCL_PIN, HIGH); delayMicroseconds(5);
  digitalWrite(PS_SDA_PIN, HIGH); delayMicroseconds(5);
  Wire.begin();
  Wire.setClock(100000);
}
#endif  // !USE_SEN0626

// ---------------------------------------------------------------------------
// SETUP
// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 2000);
  delay(200);
  DumpMemoryInfo();
  Serial.print("[VER] IRIS-EYES firmware=");
  Serial.print(FIRMWARE_VERSION);
  Serial.print(" built=");
  Serial.print(__DATE__);
  Serial.print(" proto=");
  Serial.println(PROTOCOL_VERSION);
  Serial.println("[DBG] Init -- nordicBlue default, flame/ANGRY, hypnoRed/CONFUSED, web eyes 3-24");
  Serial.flush();
  Entropy.Initialize();
  randomSeed(Entropy.random());

  if (hasBlinkButton()) pinMode(BLINK_PIN, INPUT_PULLUP);
  if (hasJoystick()) {
    pinMode(JOYSTICK_X_PIN, INPUT);
    pinMode(JOYSTICK_Y_PIN, INPUT);
  }

  if (hasPersonSensor()) {
#ifdef USE_SEN0626
    // SEN0626 (UART): begin() owns Serial4 bring-up + baud auto-detect and carries
    // its own BOOT_SETTLE_MS power-on wait plus a 3-pass baud sweep, so probe ONCE.
    // The I2C path's 10x isPresent() loop must NOT be reused here: the shim's
    // isPresent() lazily re-runs that entire ~2.7 s sweep on every call while the
    // sensor is absent, so 10 passes would hang boot for ~27 s. psI2cBusRecover()
    // is not called — UART has no SDA-latch failure mode
    // (09_IRIS_INTEGRATION_PLAN.md section 3).
    personSensorFound = personSensor.begin();
#else
    // Pi4 holds port open → Serial wait above skips; guarantee ≥2500ms from power-on before I2C probe.
    // SEN-21231 loads its ML model into SRAM on power-on; empirically needs 2-3s before it ACKs on I2C.
    while (millis() < 2500);
    Wire.begin();
    Wire.setClock(100000);
    personSensorFound = false;
    for (int attempt = 0; attempt < 10; attempt++) {
      if (personSensor.isPresent()) { personSensorFound = true; break; }
      psI2cBusRecover();  // S153b: clear a latched bus (sensor holding SDA low) before re-probing
      delay(200);
    }
#endif
    if (personSensorFound) {
      // These log strings are a CONTRACT with the Pi4: iris_web.py /api/ps/status
      // greps "Person Sensor detected" / "No Person Sensor" to drive the Vision Cal
      // status card. Do not reword either without updating that grep.
      Serial.println("[DBG] Person Sensor detected");
      personSensor.enableID(false);
      personSensor.setMode(GazeSensor::Mode::Continuous);
      delay(200); // settle, then set LED to the configured state
      personSensor.enableLED(psLedEnabled);  // no-op on SEN0626 — no LED register exists
    } else {
      Serial.println("[DBG] No Person Sensor found");
    }
  }

  mouthTFTInit();
  initEyes(!hasJoystick(), !hasBlinkButton(), !hasLightSensor());
  applyEmotion(NEUTRAL);
  mouthTFTShow(2); // CURIOUS on boot (S187c: default resting mouth, was NEUTRAL)
}

// ---------------------------------------------------------------------------
// MAIN LOOP
// ---------------------------------------------------------------------------

void loop() {
  processSerial();

  // RD-033 self-healing detection: the boot probe (setup) gives up after ~2s. On a
  // COLD power-up the Person Sensor can need longer than that to answer I2C, leaving
  // tracking dead for the whole session with no recovery. Keep re-probing here every
  // 1s until it ACKs at 0x62, then run the same init sequence and enable tracking.
  // Logging is BOUNDED (first 30 attempts only) so a permanently-absent sensor never
  // spams the journal (RD-031). If the sensor is truly dead/disconnected, this probes
  // silently after ~30s and never finds it — which is itself the diagnostic answer.
  if (!eyesSleeping && !personSensorFound) {
    static uint32_t lastReprobeMs = 0;
    static uint16_t reprobeCount  = 0;
    uint32_t nowProbe = millis();
#ifdef USE_SEN0626
    // S212: 30 s, NOT the I2C path's 1 s. An I2C probe is microseconds; the shim's
    // isPresent() lazily re-runs a full ~2.7 s baud sweep on every call while the
    // sensor is absent, and that blocks the render loop. At 1 s the eyes would
    // stall essentially continuously whenever the sensor is unplugged. 30 s keeps
    // hot-replug recovery while bounding the stall to ~2.7 s per 30 s — and only in
    // the already-degraded absent state. UART also has no I2C latch-up mode, so the
    // aggressive S135 cadence buys nothing here.
    const uint32_t reprobeIntervalMs = 30000;
#else
    const uint32_t reprobeIntervalMs = 1000;
#endif
    if (nowProbe - lastReprobeMs >= reprobeIntervalMs) {
      lastReprobeMs = nowProbe;
      if (personSensor.isPresent()) {
        personSensor.enableID(false);
        personSensor.setMode(GazeSensor::Mode::Continuous);
        delay(200); // settle, then set LED to the configured state
        personSensor.enableLED(psLedEnabled);
        personSensorFound = true;
        Serial.print("[DBG] Person Sensor detected (late re-probe #");
        Serial.print(reprobeCount); Serial.println(")");
      } else if (reprobeCount < 30) {
        // Absent-sensor log line. CONTRACT with the Pi4: iris_web.py /api/ps/status
        // greps this to drive the Vision Cal ABSENT state. It matches BOTH the
        // "no ACK at 0x62" (I2C) and "no UART reply" (SEN0626) wordings. Keep both
        // sides in sync or the card silently never reports ABSENT again.
#ifdef USE_SEN0626
        Serial.print("[DBG] Person Sensor search: no UART reply (#");
#else
        psI2cBusRecover();  // S153b: unstick a latched I2C bus between re-probes
        Serial.print("[DBG] Person Sensor search: no ACK at 0x62 (#");
#endif
        Serial.print(reprobeCount); Serial.println(")");
      }
      reprobeCount++;
    }
  }

  // Person sensor: skip during sleep to avoid I2C activity during heavy SPI load.
  if (!eyesSleeping && hasPersonSensor() && personSensor.read()) {
    // S212b: face selection must NOT require a non-zero box AREA.
    // This loop's job is "of the gated faces, pick the biggest"; `maxSize` is only
    // a ranking key. The old code started maxSize at 0 and tested `size > maxSize`,
    // which silently doubled as an "is there a face at all?" test. That works for the
    // Person Sensor (real bounding boxes, area > 0) but is FATAL for the SEN0626:
    // it reports a face CENTER, not a box, so the shim stores box_left==box_right==cx
    // and box_top==box_bottom==cy (a deliberate, correct choice: a synthetic width
    // was tried in CyclopsGaze and reverted at b84033d because it biased the recovered
    // center near frame edges). Area is therefore always 0*0 = 0, `0 > 0` is false,
    // maxSize never left 0, and EVERY face was discarded before reportFaceState() and
    // setTargetPosition(), which both gate on `maxSize > 0`. Live symptom (S212,
    // firmware=S212, SEN0626 found at 9600, PS_HEARTBEAT present=1): ZERO FACE:1/FACE:0
    // lines ever emitted, and the eyes never left autoMove, so they looked like they
    // were tracking (random idle gaze) while setTargetPosition() had never once run.
    // Fix: track "did any gated face survive" separately from the ranking key.
    // Ranking is unchanged for the Person Sensor rollback path (biggest still wins).
    int  maxSize   = -1;
    bool faceFound = false;
    person_sensor_face_t maxFace{};
    for (int i = 0; i < personSensor.numFacesFound(); i++) {
      const person_sensor_face_t face = personSensor.faceDetails(i);
      if ((!psFacingRequired || face.is_facing) && face.box_confidence > psConfGate) {
        int size = (face.box_right - face.box_left) * (face.box_bottom - face.box_top);
        if (!faceFound || size > maxSize) { maxSize = size; maxFace = face; faceFound = true; }
      }
    }
#if DEBUG_FACE
    {
      int _nf = personSensor.numFacesFound();
      Serial.print("[DBG-F] nF="); Serial.print(_nf);
      Serial.print(" mxSz="); Serial.print(maxSize);
      Serial.print(" fnd="); Serial.print(faceFound ? 1 : 0);  // S212b: the flag that actually gates tracking
      if (_nf > 0) {
        person_sensor_face_t _f0 = personSensor.faceDetails(0);
        Serial.print(" conf="); Serial.print(_f0.box_confidence);
        Serial.print(" fac="); Serial.print((int)_f0.is_facing);
      }
      if (faceFound) {
        // S212c: must mirror the live math below (raw -> gain/bias) or this readout lies.
        float _rx = (static_cast<float>(maxFace.box_left) +
                     static_cast<float>(maxFace.box_right - maxFace.box_left) / 2.0f) / 127.5f - 1.0f;
        float _ry = (static_cast<float>(maxFace.box_top) +
                     static_cast<float>(maxFace.box_bottom - maxFace.box_top) / 3.0f) / 127.5f - 1.0f;
        Serial.print(" rawXY="); Serial.print(_rx, 2); Serial.print(","); Serial.print(_ry, 2);
        Serial.print(" tX="); Serial.print(_rx * psXGain + psXBias, 2);
        Serial.print(" tY="); Serial.print(_ry * psYGain + psYBias, 2);
      }
      Serial.print(" aM="); Serial.print(eyes->autoMoveEnabled() ? 1 : 0);
      Serial.print(" spk="); Serial.print(eyesSpeaking ? 1 : 0);
      Serial.print(" tLost="); Serial.println(personSensor.timeSinceFaceDetectedMs());
    }
#endif
    reportFaceState(faceFound);
    if (!eyesSpeaking) {
      if (faceFound) {
        eyes->setAutoMove(false);
        // S212c: raw sensor-space target (-1..+1), then shaped by signed gain + bias.
        // The box-derived form is preserved verbatim so the Person Sensor rollback
        // path is unchanged: its (box_bottom-box_top)/3 term aims a third down the
        // box (eye level). SEN0626's box is center-only, so that term is 0 and this
        // collapses to the exact center. Defaults (X_GAIN=-1, Y_GAIN=+1, X_BIAS=0)
        // reproduce the historical math bit-for-bit.
        float rawX = (static_cast<float>(maxFace.box_left) + static_cast<float>(maxFace.box_right - maxFace.box_left) / 2.0f) / 127.5f - 1.0f;
        float rawY = (static_cast<float>(maxFace.box_top)  + static_cast<float>(maxFace.box_bottom - maxFace.box_top) / 3.0f) / 127.5f - 1.0f;
        float targetX = rawX * psXGain + psXBias;
        float targetY = rawY * psYGain + psYBias;
        eyes->setTargetPosition(targetX, targetY);
      } else if (personSensor.timeSinceFaceDetectedMs() > psLostMs && !eyes->autoMoveEnabled()) {
        eyes->setAutoMove(true);
      }
    }
  }

  // When sleeping: render starfield on eyes + animate mouth, skip eye engine.
  // renderSleepFrame() is self-throttled to SR_FRAME_MS (150ms).
  // mouthSleepFrame() runs on iterations where eye renderer skips (the ~140ms
  // gaps between eye frames). When eye renderer fires it blocks ~114ms on
  // updateScreen() — mouth skips that iteration. This gives mouth ~10 calls/sec.
  if (eyesSleeping) {
    uint32_t nowMs2 = millis();
    bool eyeWillRender = (nowMs2 - srLastFrameMs >= SR_FRAME_MS);
    renderSleepFrame(displayLeft->getDriver(), displayRight->getDriver());
    if (!eyeWillRender && (nowMs2 - srMouthLastMs >= MOUTH_SLEEP_FRAME_MS)) {
      mouthSleepFrame();
      srMouthLastMs = nowMs2;
    }
    return;
  }

  // Emotion eye-swap revert -> userDefaultEye after this emotion's revertMs
  // (S241: one generic timer; ANGRY=9000 and CONFUSED=7000 now come from
  // emotionEyeSwap rather than from two separate hard-coded blocks.)
  if (swapEyeActive && (millis() - swapEyeStartMs) >= swapEyeRevertMs) {
    setEyeDefinition(userDefaultEye);
    swapEyeActive = false;
    Serial.println("[DBG] EYE swap revert -> userDefaultEye");
  }

  if (hasBlinkButton() && digitalRead(BLINK_PIN) == LOW) eyes->blink();

  if (hasJoystick()) {
    auto x = analogRead(JOYSTICK_X_PIN);
    auto y = analogRead(JOYSTICK_Y_PIN);
    eyes->setPosition((x - 512) / 512.0f, (y - 512) / 512.0f);
  }

  if (hasLightSensor()) {
    lightSensor.readDamped([](float value) {
      eyes->setPupil(value);
    });
  }

  lidScriptTick();   // S241: advance the lid envelope before the frame is drawn

  eyes->renderFrame();

  if (mouthUpdatePending) {
    uint32_t nowMs = millis();
    if (nowMs - lastMouthRenderMs >= MOUTH_RENDER_MIN_MS) {
      mouthTFTShow(pendingMouthIdx);
      mouthUpdatePending = false;
      lastMouthRenderMs  = nowMs;
    }
  }

  // MOUTHGEST auto-restore: after the SILLY gesture glyph elapses, queue a
  // return to NEUTRAL (non-blocking; rendered by the block above next pass).
  if (mouthGestureRestoreMs && millis() >= mouthGestureRestoreMs) {
    mouthGestureRestoreMs = 0;
    pendingMouthIdx       = 0;   // NEUTRAL
    mouthUpdatePending    = true;
  }

  // Auto-start idle after IDLE_AUTO_MS of no serial commands
  uint32_t nowLoop = millis();
  if (!mouthIdleIsActive() && (nowLoop - lastCommandMs) >= IDLE_AUTO_MS) {
    mouthIdleStart();
    mouthApplyIdleTint(); // RD-030 #2: settle into the emotion-tinted resting face
  }
  mouthIdleTick(nowLoop);
}
