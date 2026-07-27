#include "pan_servo.h"
#include <ServoEasing.hpp>   // ServoEasing implementation — included in exactly ONE translation unit
#include "diag.h"            // SERIAL_DIAG gating for PAN command

static ServoEasing panServo;
static bool servoAttached = true;
static float filteredFaceCenterX = 128.0;  // low-pass smoothed raw sensor readout (see FACE_POS_FILTER_ALPHA)

float desiredPan = 90.0;

void setupPanServo() {
  panServo.attach(2);
  panServo.write(desiredPan);
  panServo.setEasingType(EASE_CUBIC_IN_OUT);
  enableServoEasingInterrupt();
  servoAttached = true;
}

void attachPanServo() {
  // Re-attach at ServoEasing's stored currentAngle. When the servo-rail sense soft-start
  // holds this equal to the frozen physical angle, the first pulse matches the horn and
  // the servo does not jump. Idempotent.
  if (!servoAttached) {
    panServo.attach(2);
    servoAttached = true;
  }
}

void detachPanServo() {
  // Stop PWM output. ServoEasing keeps its last currentAngle, so a later attach resumes
  // from the same commanded angle. Idempotent.
  if (servoAttached) {
    panServo.detach();
    servoAttached = false;
  }
}

void updatePanFromFace(float faceCenterX) {
  // Smooth the raw sensor reading first — the Person Sensor's own face-box detector
  // jitters frame to frame even on a static, perpendicular face.
  filteredFaceCenterX += (faceCenterX - filteredFaceCenterX) * FACE_POS_FILTER_ALPHA;

  // ---- Anti-windup gate (S208 hunting fix) -----------------------------------------
  // The Person Sensor rides on the pan platform, so pan tracking is a CLOSED loop: a
  // pan move re-centers the face in the sensor frame. The servo is deliberately slow
  // (PAN_TRACK_SPEED). Previously `desiredPan -= panDelta` ran on EVERY 50 ms poll, so
  // while the slow servo was still gliding toward the last target, desiredPan kept
  // accumulating error the plant had not yet nulled — command rate far outran plant
  // response. That integrator wind-up overshot the face and drove a sustained
  // side-to-side limit cycle (the "hunting" prior sessions kept trying to bury under
  // extra low-pass layers). Only evaluating a new correction once the servo has FULLY
  // SETTLED makes every correction act on a fresh, post-move reading: a converging
  // staircase of ever-smaller steps instead of a wind-up oscillation.
  if (panServo.isMoving()) return;

  float panDelta = (filteredFaceCenterX - 128) * PAN_SPEED;

  // Dead zone: ignore sub-threshold sensor jitter (~10 px). This is now the single
  // jitter gate — a face within ~10 px of center holds position, no micro-moves.
  if (abs(panDelta) <= PAN_DEAD_ZONE_DEG) return;

  desiredPan -= panDelta;
  desiredPan = constrain(desiredPan, PAN_MIN, PAN_MAX);

  // Servo is settled (gate above) → issue the single new correction. Motion stays
  // "eerily slow and smooth" via the slow linear ease; the settle-gate guarantees we
  // never stack a command on top of an in-flight move.
  if (!servoAttached) {
    panServo.attach(2);
    servoAttached = true;
  }
  panServo.setEasingType(EASE_LINEAR);
  panServo.startEaseTo(desiredPan, PAN_TRACK_SPEED);
}

void updatePanIdle(unsigned long faceLostMs) {
  if (faceLostMs > FACE_RETURN_MS) {
    desiredPan += (90.0 - desiredPan) * 0.03;
    if (!panServo.isMoving()) {
      if (abs(desiredPan - 90.0) > 1.0) {
        // Still returning to center
        if (!servoAttached) {
          panServo.attach(2);
          servoAttached = true;
        }
        panServo.setEasingType(EASE_CUBIC_IN_OUT);
        panServo.startEaseToD(desiredPan, 100);
      } else if (servoAttached) {
        // At center — release holding torque
        panServo.detach();
        servoAttached = false;
      }
    }
  }
  // 0..FACE_HOLD_MS and FACE_HOLD_MS..FACE_RETURN_MS: hold position, do nothing
}

bool handleSerialPanCmd(String cmd) {
#if SERIAL_DIAG
  // ===== CODEX DIAGNOSTIC INSERT BEGIN: direct servo isolation command =====
  if (cmd.startsWith("PAN ")) {
    desiredPan = constrain(cmd.substring(4).toFloat(), PAN_MIN, PAN_MAX);
    if (!servoAttached) {
      panServo.attach(2);
      servoAttached = true;
    }
    panServo.startEaseTo(desiredPan, PAN_TRACK_SPEED);
    Serial.print("DIAG: manual pan target=");
    Serial.println((int)desiredPan);
    return true;
  } else if (cmd == "PAN?") {
    Serial.print("PAN=");
    Serial.println(panServo.getCurrentAngle());
    return true;
  }
  // ===== CODEX DIAGNOSTIC INSERT END: direct servo isolation command =====
#endif
  return false;
}
