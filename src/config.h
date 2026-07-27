#pragma once

// ── Firmware version — update this before every flash ────────────────────────
// Format: "S<session>[letter]"  e.g. "S87b"
// Printed on boot: [VER] IRIS-EYES firmware=S87b built=May 31 2026 proto=1
// Queried via serial command: "VERSION"
static constexpr char FIRMWARE_VERSION[] = "S241";

// ── Serial protocol contract version (RD-048 move 2, S213) ───────────────────
// Bump when the serial COMMAND CONTRACT changes (a command/emit token added,
// removed, or its meaning changed) — NOT on every flash. Baked into the [VER]
// line; pi4/core/protocol.py EXPECTED_EYES_PROTOCOL must be bumped in the same
// commit. scripts/version_guard.py fails the flash preflight if the contract
// drifts without a bump.
// S241: 1 -> 2. Three contract changes, all in the eyes T4.1 surface:
//   * EMOTION: accepts two new payload tokens, ANNOYED and EXASPERATED
//   * EYE:n valid range widened 0-6 -> 0-24 (EYE_IDX_COUNT in main.cpp)
//   * the "[DBG] ANGRY revert" / "[DBG] CONFUSED revert" emit tokens are
//     replaced by one generic "[DBG] EYE swap revert" (verified this session:
//     nothing under pi4/ or scripts/ greps the old strings)
// pi4/core/protocol.py EXPECTED_EYES_PROTOCOL is bumped in the same commit.
// A mismatch is warn-only and the link stays up (teensy_bridge.py:123-129), so
// the repo bump is safe in the window before the flash and the Pi deploy.
static constexpr int PROTOCOL_VERSION = 2;

#include "eyes/eyes.h"

// ── Emotion-linked eyes ───────────────────────────────────────────────────────
// Index 0: nordicBlue  (default)
// Index 1: flame       (ANGRY emotion swap)
// Index 2: hypnoRed    (CONFUSED emotion swap)
#include "eyes/240x240/nordicBlue.h"
#include "eyes/240x240/flame.h"
#include "eyes/240x240/hypnoRed.h"

// ── Web UI selectable eyes (compiled in, switchable via EYE:n serial command) ─
// Index 3: hazel
// Index 4: blueFlame1
// Index 5: dragon
// Index 6: strikingBlue
#include "eyes/240x240/hazel.h"
#include "eyes/240x240/blueFlame1.h"
#include "eyes/240x240/dragon.h"
#include "eyes/240x240/strikingBlue.h"

// ── S241: the vendored eye library, indices 7+ ───────────────────────────────
// These headers shipped with the chrismiller TeensyEyes port and sat on disk
// with zero #includes until now (S239 audit, claim 8). They are the taste
// palette for RD-066 item 3: the operator cycles them from the WebUI eye
// selector and decides which texture means which emotion, then EMOTION_EYE_MAP
// on the Pi drives the choice as data. Firmware's job is range, not mapping.
// Flash is not the constraint -- the S241 baseline build left 6,114,308 bytes
// free for files, and the whole set below fits inside that with room to spare
// (linker line quoted in the S241 CHANGELOG entry).
#include "eyes/240x240/cat.h"
#include "eyes/240x240/doomSpiral.h"
#include "eyes/240x240/anime.h"
#include "eyes/240x240/doe.h"
#include "eyes/240x240/demon.h"
#include "eyes/240x240/skull.h"
#include "eyes/240x240/leopard.h"
#include "eyes/240x240/toonstripe.h"
#include "eyes/240x240/fizzgig.h"
#include "eyes/240x240/newt.h"
#include "eyes/240x240/snake.h"
#include "eyes/240x240/fish.h"
#include "eyes/240x240/brown.h"
#include "eyes/240x240/bigBlue.h"
#include "eyes/240x240/spikes.h"
#include "eyes/240x240/firebox.h"
#include "eyes/240x240/blueFlame2.h"
#include "eyes/240x240/doomRed.h"

#include "eyes/EyeController.h"

#define USE_GC9A01A
//#define USE_ST7789

#ifdef USE_GC9A01A
#include "displays/GC9A01A_Display.h"
#elif defined USE_ST7789
#include "displays/ST7789_Display.h"
#ifdef ST7735_SPICLOCK
#undef ST7735_SPICLOCK
#endif
#define ST7735_SPICLOCK 30'000'000
#endif

// eyeDefinitions index map:
//   0 = nordicBlue    (default idle)
//   1 = flame         (ANGRY emotion + web UI)
//   2 = hypnoRed      (CONFUSED emotion + web UI)
//   3 = hazel         (web UI)
//   4 = blueFlame1    (web UI)
//   5 = dragon        (web UI)
//   6 = strikingBlue  (web UI)
//   7+ = S241 library (web UI; taste pass decides which emotion earns which)
// Rows are {left, right} pairs. They are identical here, but the pair form is
// what makes an asymmetric definition possible later without an API change
// (S239 audit claim 9) -- setLidOpenness is controller-global, so baked
// asymmetry is the only runtime-asymmetric route.
// EYE_IDX_COUNT in main.cpp MUST match this array's length.
std::array<std::array<EyeDefinition, 2>, 25> eyeDefinitions{{
    {nordicBlue::eye,    nordicBlue::eye},     // 0
    {flame::eye,         flame::eye},          // 1
    {hypnoRed::eye,      hypnoRed::eye},       // 2
    {hazel::eye,         hazel::eye},          // 3
    {blueFlame1::eye,    blueFlame1::eye},     // 4
    {dragon::eye,        dragon::eye},         // 5
    {strikingBlue::eye,  strikingBlue::eye},   // 6
    {cat::eye,           cat::eye},            // 7
    // S241: these five vendored sets ship as genuine asymmetric {left, right}
    // pairs rather than one shared `eye` -- the baked-asymmetry route the S239
    // audit (claim 9) called structurally supported. Wire them as authored;
    // collapsing them to one side would throw away the asymmetry.
    {doomSpiral::left,   doomSpiral::right},   // 8
    {anime::left,        anime::right},        // 9
    {doe::left,          doe::right},          // 10
    {demon::left,        demon::right},        // 11
    {skull::eye,         skull::eye},          // 12
    {leopard::left,      leopard::right},      // 13
    {toonstripe::eye,    toonstripe::eye},     // 14
    {fizzgig::eye,       fizzgig::eye},        // 15
    {newt::eye,          newt::eye},           // 16
    {snake::eye,         snake::eye},          // 17
    {fish::eye,          fish::eye},           // 18
    {brown::eye,         brown::eye},          // 19
    {bigBlue::eye,       bigBlue::eye},        // 20
    {spikes::eye,        spikes::eye},         // 21
    {firebox::eye,       firebox::eye},        // 22
    {blueFlame2::eye,    blueFlame2::eye},     // 23
    {doomRed::eye,       doomRed::eye},        // 24
}};

#ifdef USE_GC9A01A
GC9A01A_Config eyeInfo[] = {
    // CS DC MOSI SCK RST ROT MIRROR USE_FB ASYNC
    {0,  2, 26, 27, 3,  0, true,  true, false}, // Left
    {10, 9, 11, 13, -1, 0, false, true, false}, // Right
};
#elif defined USE_ST7789
ST7789_Config eyeInfo[] = {
    {-1,  2, 26, 27, 3, 0, true,  true, true},
    {-1,  9, 11, 13, 8, 0, false, true, true},
};
#endif

constexpr uint32_t EYE_DURATION_MS{10'000};
constexpr uint32_t SPI_SPEED{20'000'000};

constexpr int8_t BLINK_PIN{-1};
constexpr int8_t JOYSTICK_X_PIN{-1};
constexpr int8_t JOYSTICK_Y_PIN{-1};
constexpr int8_t LIGHT_PIN{-1};
constexpr bool USE_PERSON_SENSOR{true};

// ── Gaze sensor transport (S212) ─────────────────────────────────────────────
// The Useful Sensors Person Sensor (SEN-21231) is DISCONTINUED. The DFRobot
// SEN0626 stands in on this same eyes node: it emulates person_sensor_face_t and
// the PersonSensor method surface, but speaks Modbus RTU over UART rather than
// I2C. USE_PERSON_SENSOR above stays the master enable for whichever part is fitted.
//
// Define exactly ONE. Both sensor headers typedef person_sensor_face_t as an
// anonymous struct, so including both is a redefinition error, not a duplicate.
//
// ROLLBACK to the I2C Person Sensor: comment USE_SEN0626, uncomment
// USE_PERSON_SENSOR_I2C, reflash, and re-land the sensor on SDA 18 / SCL 19.
// PersonSensor.{h,cpp} and psI2cBusRecover() are kept in-tree for exactly this.
#define USE_SEN0626
//#define USE_PERSON_SENSOR_I2C

#ifdef USE_SEN0626
// Serial4 = RX 16 / TX 17 on Teensy 4.1 (verified against the core:
// cores/teensy4/HardwareSerial4.cpp, non-MicroMod branch). Sensor D/T -> pin 16,
// sensor C/R -> pin 17.
// NOT Serial1: CyclopsGaze 05_WIRING.md specifies pins 0/1, but pin 0 is IRIS's
// LEFT EYE CS (eyeInfo[0] above) -- that table was written against a bench rig
// whose display CS was remapped to pin 10. Serial2 (7/8) and Serial8 (34/35)
// collide with the ILI9341 mouth's DC and MOSI. Free alternates if the harness
// ever moves: Serial5 (21/20), Serial6 (25/24), Serial7 (28/29).
#define SEN0626_SERIAL Serial4
#endif

#ifdef USE_GC9A01A
EyeController<2, GC9A01A_Display> *eyes{};
// Global display pointers -- used by blankDisplays() in main.cpp
GC9A01A_Display *displayLeft{};
GC9A01A_Display *displayRight{};
#elif defined USE_ST7789
EyeController<2, ST7789_Display> *eyes{};
#endif

void initEyes(bool autoMove, bool autoBlink, bool autoPupils) {
  auto &defs = eyeDefinitions.at(0);
#ifdef USE_GC9A01A
  displayLeft  = new GC9A01A_Display(eyeInfo[0], SPI_SPEED);
  displayRight = new GC9A01A_Display(eyeInfo[1], SPI_SPEED);
  const DisplayDefinition<GC9A01A_Display> left{displayLeft,  defs[0]};
  const DisplayDefinition<GC9A01A_Display> right{displayRight, defs[1]};
  eyes = new EyeController<2, GC9A01A_Display>({left, right}, autoMove, autoBlink, autoPupils);
#elif defined USE_ST7789
  auto l = new ST7789_Display(eyeInfo[0]);
  auto r = new ST7789_Display(eyeInfo[1]);
  const DisplayDefinition<ST7789_Display> left{l, defs[0]};
  const DisplayDefinition<ST7789_Display> right{r, defs[1]};
  eyes = new EyeController<2, ST7789_Display>({left, right}, autoMove, autoBlink, autoPupils);
#endif
}
