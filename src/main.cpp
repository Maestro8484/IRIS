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
#include "sensors/PersonSensor.h"

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
//   6 = strikingBlue   (bigBlue removed)
//
// EYE:n selectable range: 0-6
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
static constexpr uint32_t EYE_IDX_COUNT        = 7; // total entries in eyeDefinitions

// ---------------------------------------------------------------------------
// EMOTION -> EYE PARAMETER MAPPING
// ---------------------------------------------------------------------------

struct EmotionParams {
  float    pupilRatio;
  bool     doBlink;
  uint32_t maxGazeMs;
};

enum EmotionID { NEUTRAL=0, HAPPY, CURIOUS, ANGRY, SLEEPY, SURPRISED, SAD, CONFUSED, AMUSED, EMOTION_COUNT };

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
};

// ---------------------------------------------------------------------------
// STATE
// ---------------------------------------------------------------------------

// userDefaultEye tracks the web UI selection -- the eye to revert to after
// emotion eye swaps end. Starts at nordicBlue, updated by EYE:n command.
static uint32_t userDefaultEye{EYE_IDX_DEFAULT};
static uint32_t defIndex{EYE_IDX_DEFAULT};

LightSensor  lightSensor(LIGHT_PIN);
PersonSensor personSensor(Wire);
bool         personSensorFound = USE_PERSON_SENSOR;

static bool     faceWasPresent  = false;
static uint32_t lastFace1SentMs = 0;

static char    serialBuf[SERIAL_BUF_SIZE];
static uint8_t serialBufLen = 0;

// Angry eye revert timer
static bool     angryEyeActive  = false;
static uint32_t angryEyeStartMs = 0;

// Confused eye revert timer
static bool     confusedEyeActive  = false;
static uint32_t confusedEyeStartMs = 0;

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
static bool     psLedEnabled     = true;                  // PS_CFG:LED=0/1  on-sensor LED indicator (liveness). S153: default TRUE so the Teensy-only init lights the LED autonomously at boot — a true alive/reachable signal independent of the Pi4 PS_CFG push (decoupled from the S150d Pi4 dependency).
static uint32_t psLostMs         = FACE_LOST_TIMEOUT_MS;  // PS_CFG:LOST_MS=n  autoMove resume delay
static float    psYBias          = 0.0f;                  // PS_CFG:Y_BIAS=f  additive Y target offset

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
static constexpr uint32_t MOUTH_RENDER_MIN_MS = 75;

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
  return NEUTRAL;
}

static void applyEmotion(EmotionID id) {
  const EmotionParams &p = emotionTable[id];
  eyes->setTargetPupil(p.pupilRatio, 300);
  eyes->setMaxGazeMs(p.maxGazeMs);
  if (p.doBlink) eyes->blink();

  if (id == ANGRY) {
    setEyeDefinition(EYE_IDX_ANGRY);
    angryEyeActive    = true;
    angryEyeStartMs   = millis();
    confusedEyeActive = false;
  } else if (id == CONFUSED) {
    setEyeDefinition(EYE_IDX_CONFUSED);
    confusedEyeActive  = true;
    confusedEyeStartMs = millis();
    angryEyeActive     = false;
  } else {
    if (angryEyeActive || confusedEyeActive) {
      setEyeDefinition(userDefaultEye);
      angryEyeActive    = false;
      confusedEyeActive = false;
    }
  }
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
            if (!angryEyeActive && !confusedEyeActive) {
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
            angryEyeActive    = false;
            confusedEyeActive = false;
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
          Serial.println(__DATE__);

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
            if      (strcmp(key, "CONF")    == 0) psConfGate       = (uint8_t)constrain((int)val, 0, 100);
            else if (strcmp(key, "FACING")  == 0) psFacingRequired = (val != 0.0f);
            else if (strcmp(key, "LOST_MS") == 0) psLostMs         = (uint32_t)val;
            else if (strcmp(key, "Y_BIAS")  == 0) psYBias          = val;
            else if (strcmp(key, "LED")     == 0) { psLedEnabled = (val != 0.0f);
                                                    if (hasPersonSensor()) personSensor.enableLED(psLedEnabled); }
            Serial.print("[DBG] PS_CFG ");
            Serial.print(key); Serial.print("="); Serial.println(eq + 1);
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
    if ((now - lastFace1SentMs) >= FACE_COOLDOWN_MS) {
      Serial.println("FACE:1");
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
  Serial.println(__DATE__);
  Serial.println("[DBG] Init -- nordicBlue default, flame/ANGRY, hypnoRed/CONFUSED, web eyes 3-7");
  Serial.flush();
  Entropy.Initialize();
  randomSeed(Entropy.random());

  if (hasBlinkButton()) pinMode(BLINK_PIN, INPUT_PULLUP);
  if (hasJoystick()) {
    pinMode(JOYSTICK_X_PIN, INPUT);
    pinMode(JOYSTICK_Y_PIN, INPUT);
  }

  if (hasPersonSensor()) {
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
    if (personSensorFound) {
      Serial.println("[DBG] Person Sensor detected");
      personSensor.enableID(false);
      personSensor.setMode(PersonSensor::Mode::Continuous);
      delay(200); // settle, then set LED to the configured state
      personSensor.enableLED(psLedEnabled);
    } else {
      Serial.println("[DBG] No Person Sensor found");
    }
  }

  mouthTFTInit();
  initEyes(!hasJoystick(), !hasBlinkButton(), !hasLightSensor());
  applyEmotion(NEUTRAL);
  mouthTFTShow(0); // NEUTRAL on boot
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
    if (nowProbe - lastReprobeMs >= 1000) {
      lastReprobeMs = nowProbe;
      if (personSensor.isPresent()) {
        personSensor.enableID(false);
        personSensor.setMode(PersonSensor::Mode::Continuous);
        delay(200); // settle, then set LED to the configured state
        personSensor.enableLED(psLedEnabled);
        personSensorFound = true;
        Serial.print("[DBG] Person Sensor detected (late re-probe #");
        Serial.print(reprobeCount); Serial.println(")");
      } else if (reprobeCount < 30) {
        psI2cBusRecover();  // S153b: unstick a latched I2C bus between re-probes
        Serial.print("[DBG] Person Sensor search: no ACK at 0x62 (#");
        Serial.print(reprobeCount); Serial.println(")");
      }
      reprobeCount++;
    }
  }

  // Person sensor: skip during sleep to avoid I2C activity during heavy SPI load.
  if (!eyesSleeping && hasPersonSensor() && personSensor.read()) {
    int maxSize = 0;
    person_sensor_face_t maxFace{};
    for (int i = 0; i < personSensor.numFacesFound(); i++) {
      const person_sensor_face_t face = personSensor.faceDetails(i);
      if ((!psFacingRequired || face.is_facing) && face.box_confidence > psConfGate) {
        int size = (face.box_right - face.box_left) * (face.box_bottom - face.box_top);
        if (size > maxSize) { maxSize = size; maxFace = face; }
      }
    }
#if DEBUG_FACE
    {
      int _nf = personSensor.numFacesFound();
      Serial.print("[DBG-F] nF="); Serial.print(_nf);
      Serial.print(" mxSz="); Serial.print(maxSize);
      if (_nf > 0) {
        person_sensor_face_t _f0 = personSensor.faceDetails(0);
        Serial.print(" conf="); Serial.print(_f0.box_confidence);
        Serial.print(" fac="); Serial.print((int)_f0.is_facing);
      }
      if (maxSize > 0) {
        float _tx = -((static_cast<float>(maxFace.box_left) +
                       static_cast<float>(maxFace.box_right - maxFace.box_left) / 2.0f) / 127.5f - 1.0f);
        float _ty =  (static_cast<float>(maxFace.box_top) +
                       static_cast<float>(maxFace.box_bottom - maxFace.box_top) / 3.0f) / 127.5f - 1.0f + psYBias;
        Serial.print(" tX="); Serial.print(_tx, 2);
        Serial.print(" tY="); Serial.print(_ty, 2);
      }
      Serial.print(" aM="); Serial.print(eyes->autoMoveEnabled() ? 1 : 0);
      Serial.print(" spk="); Serial.print(eyesSpeaking ? 1 : 0);
      Serial.print(" tLost="); Serial.println(personSensor.timeSinceFaceDetectedMs());
    }
#endif
    reportFaceState(maxSize > 0);
    if (!eyesSpeaking) {
      if (maxSize > 0) {
        eyes->setAutoMove(false);
        float targetX = -((static_cast<float>(maxFace.box_left) + static_cast<float>(maxFace.box_right - maxFace.box_left) / 2.0f) / 127.5f - 1.0f);
        float targetY = (static_cast<float>(maxFace.box_top) + static_cast<float>(maxFace.box_bottom - maxFace.box_top) / 3.0f) / 127.5f - 1.0f + psYBias;
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

  // Angry eye revert: flame -> userDefaultEye after ANGRY_EYE_DURATION_MS
  if (angryEyeActive && (millis() - angryEyeStartMs) >= ANGRY_EYE_DURATION_MS) {
    setEyeDefinition(userDefaultEye);
    angryEyeActive = false;
    Serial.println("[DBG] ANGRY revert -> userDefaultEye");
  }

  // Confused eye revert: hypnoRed -> userDefaultEye after CONFUSED_EYE_DURATION_MS
  if (confusedEyeActive && (millis() - confusedEyeStartMs) >= CONFUSED_EYE_DURATION_MS) {
    setEyeDefinition(userDefaultEye);
    confusedEyeActive = false;
    Serial.println("[DBG] CONFUSED revert -> userDefaultEye");
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
