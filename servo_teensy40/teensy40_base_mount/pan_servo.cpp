#include "pan_servo.h"
#include <ServoEasing.hpp>   // ServoEasing implementation — included in exactly ONE translation unit
#include "diag.h"            // SERIAL_DIAG gating for PAN command

static ServoEasing panServo;
static bool servoAttached = true;
static float filteredPan = 90.0;  // low-pass smoothed command sent to servo
static float filteredFaceCenterX = 128.0;  // low-pass smoothed raw sensor readout (see FACE_POS_FILTER_ALPHA)

float desiredPan = 90.0;

void setupPanServo() {
  panServo.attach(2);
  panServo.write(desiredPan);
  filteredPan = desiredPan;
  panServo.setEasingType(EASE_CUBIC_IN_OUT);
  enableServoEasingInterrupt();
  servoAttached = true;
}

void updatePanFromFace(float faceCenterX) {
  // Smooth the raw sensor reading first — the Person Sensor's own face-box detector
  // jitters frame to frame even on a static, perpendicular face. Feeding that noise
  // straight into the dead-zone gate below let occasional noisy frames exceed the
  // threshold and permanently nudge desiredPan, which random-walked into visible hunting.
  filteredFaceCenterX += (faceCenterX - filteredFaceCenterX) * FACE_POS_FILTER_ALPHA;

  float panDelta = (filteredFaceCenterX - 128) * PAN_SPEED;

  // Dead zone: ignore sub-threshold sensor jitter (~10 px)
  if (abs(panDelta) > PAN_DEAD_ZONE_DEG) {
    desiredPan -= panDelta;
    desiredPan = constrain(desiredPan, PAN_MIN, PAN_MAX);
  }

  // Low-pass filter glides toward desiredPan every tick
  filteredPan += (desiredPan - filteredPan) * PAN_FILTER_ALPHA;

  // Only command servo when it has fully settled AND filtered target is far enough from physical position.
  // isMoving() is the natural gate — no arbitrary timer, no staircase of mid-convergence commands.
  if (!panServo.isMoving() &&
      abs(filteredPan - panServo.getCurrentAngle()) > PAN_MOVE_THRESHOLD_DEG) {
    if (!servoAttached) {
      panServo.attach(2);
      servoAttached = true;
    }
    panServo.setEasingType(EASE_LINEAR);
    panServo.startEaseTo(filteredPan, PAN_TRACK_SPEED);
  }
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
