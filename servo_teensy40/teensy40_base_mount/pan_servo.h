#pragma once
#include <Arduino.h>

// Servo tracking constants
// PAN_SPEED: delta scale; (faceCenterX-128)*PAN_SPEED = degrees of correction needed per tick
#define PAN_SPEED            0.02
#define PAN_TRACK_SPEED      6.0    // deg/sec — startEaseTo() speed (was 8.0)
#define PAN_FILTER_ALPHA     0.08   // RETIRED S208: the filteredPan low-pass command layer was removed. It was one of
                                    // several stacked filters that fought the real bug (integrator wind-up) instead of
                                    // fixing it, and below the move threshold it could stall commands entirely. The
                                    // settle-gate in updatePanFromFace() is the correct smoothing/rate-limit now. Do NOT
                                    // re-introduce a command-side low-pass — kept only as a historical marker.
#define PAN_DEAD_ZONE_DEG    0.20   // ignore corrections < 0.20° (≈ 10 px sensor jitter); replaces old PAN_DEAD_ZONE/90 (was 2.8 px)
#define PAN_MOVE_THRESHOLD_DEG 0.8  // RETIRED S208: was the filteredPan-vs-physical gate; unused now. The dead zone above
                                    // is the single jitter gate. Kept only as a historical marker.
#define FACE_POS_FILTER_ALPHA 0.12  // low-pass on raw sensor faceCenterX (px), applied BEFORE the dead-zone gate.
                                    // Without this, single-frame face-box jitter from the Person Sensor's own
                                    // detector (common even on a static, perpendicular face) fed straight into
                                    // the desiredPan integrator below, so noisy frames that happened to exceed
                                    // the dead zone nudged desiredPan permanently — a random walk that showed up
                                    // as continuous side-to-side hunting on a subject who wasn't moving at all.

// Face-lost timing (ms)
#define FACE_HOLD_MS   2500
#define FACE_RETURN_MS 30000

// Pan servo rotation limits
#define PAN_MIN 65.0
#define PAN_MAX 115.0

// Current pan target (degrees). Exposed for diagnostic telemetry.
extern float desiredPan;

void setupPanServo();
void updatePanFromFace(float faceCenterX);
void updatePanIdle(unsigned long faceLostMs);

// Attach/detach the pan servo (manage PWM output). Used by the servo-rail sense
// soft-start (SERVO_PWR_SENSE) to freeze the commanded angle while the rail is off.
void attachPanServo();
void detachPanServo();

// Handles PAN / PAN? serial commands. Returns true if cmd was consumed.
// Active only when SERIAL_DIAG is enabled (matches original gating).
bool handleSerialPanCmd(String cmd);
