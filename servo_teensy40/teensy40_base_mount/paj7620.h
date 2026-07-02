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

extern bool pajOk;

bool paj7620Init();
void pollGesture();
