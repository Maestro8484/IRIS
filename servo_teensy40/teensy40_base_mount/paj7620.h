#pragma once
#include <Arduino.h>

// Set to 0 to disable serial diagnostic output across gesture + servo modules.
#define SERIAL_DIAG 1

// Physical mount orientation of PAJ7620U2 (degrees CW from default datasheet orientation).
//   0  = connector down  90 = rotated CW  180 = upside-down  270 = rotated CCW
#define PAJ7620_ADDR          0x73
#define GESTURE_MOUNT_DEGREES 0

// Minimum time (ms) between same-gesture re-fires (per-gesture debounce).
#define GESTURE_DEBOUNCE_MS 400
// Minimum time (ms) between any-gesture fires (global cooldown).
#define GESTURE_COOLDOWN_MS 200
// ---- RD-059 range floor (S243b) -------------------------------------------
// The countertop complaint ("anything within ~2 feet fires it") is a RANGE
// problem, and the chip has no proximity gate available while recognizing
// gestures: proximity is a separate, MUTUALLY EXCLUSIVE mode. Datasheet v1.5
// section 8.3 "Change to PS Mode" writes 0x42=0x02 and masks gesture interrupts
// with 0x41=0x00, against gesture mode's 0x42=0x01 / 0x41=0xFF. So the WebUI's
// GESTURE_PROXIMITY_THRESHOLD can never be a true proximity gate on gestures.
//
// The control that DOES work in gesture mode is object SIZE (datasheet 7.6).
// R_GestureStartTh: "when the object size is larger than this, state machine
// goes to has object state". A hand subtends a larger object the closer it is,
// so raising this is a distance floor. R_LightThd is a second lever: a pixel
// counts toward the object only if brighter than this, and reflected IR falls
// off sharply with distance.
//
// These sat at PixArt's defaults for the sensor's entire life -- the init array
// below writes 0x83=0x20 / 0x84=0x20 / 0x86=0x10, which ARE the defaults. No
// range floor was ever set. That, not an overshooting sensitivity fix, is why
// it sees the whole kitchen.
//
// NEEDS BENCH CALIBRATION. Object size scales roughly with 1/distance^2, so
// cutting usable range by 3x implies raising the start threshold by roughly 9x
// (32 -> ~288). 96 is a deliberately conservative first cut: it should trim the
// far tail while leaving a deliberate close swipe far above threshold. Walk it
// up if 2 feet still fires; walk it down if close swipes get missed.
// Set any of these to 0 to leave PixArt's default untouched.
#define GESTURE_START_TH  96   // R_GestureStartTh, 10-bit, PixArt default 32
#define GESTURE_END_TH    48   // R_GestureEndTh,   10-bit, PixArt default 16
                               // (kept at half of start, matching the stock ratio)
#define GESTURE_LIGHT_TH   0   // R_LightThd, 8-bit, PixArt default 0x20. Second
                               // lever, left at default until start-th is tuned.

// Minimum time (ms) before the OPPOSITE of a gesture may fire (RD-059).
// One hand swipe crosses the sensor twice: once outbound, once as the hand is
// withdrawn, and the return pass is reported as the opposite direction. Neither
// guard above catches it -- the debounce is keyed per bit, and opposites occupy
// different bits, so only the 200ms cooldown spans them and a return stroke is
// slower than that. 1500ms chosen from 45 days of logged events (RD-059
// forensics): it suppresses ~8% of all events, and 3000ms only reaches ~10%.
// Long enough to eat the return stroke, short enough that a deliberate
// correction ("up, too far, down") still gets through.
#define GESTURE_REVERSAL_MS 1500

extern bool pajOk;

bool paj7620Init();
void pollGesture();
