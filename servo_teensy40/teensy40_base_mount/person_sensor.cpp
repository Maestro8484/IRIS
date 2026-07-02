#include "person_sensor.h"
#include <Wire.h>
#include "paj7620.h"     // pajOk + SERIAL_DIAG (telemetry only)
#include "pan_servo.h"   // desiredPan (telemetry only)

// Live LED state, seeded from the compile-time default.
bool personSensorLedEnabled = (PERSON_SENSOR_LED_ENABLED != 0);

// Write the Person Sensor LED (debug mode) register.
// Register 0x07 = PERSON_SENSOR_REG_DEBUG_MODE per SparkFun docs.
static void writePersonSensorLed(bool on) {
  Wire.beginTransmission(PERSON_SENSOR_I2C_ADDRESS);
  Wire.write(0x07);
  Wire.write(on ? 1 : 0);
  Wire.endTransmission();
}

void setPersonSensorLed(bool on) {
  personSensorLedEnabled = on;
  writePersonSensorLed(on);
}

void setupPersonSensor() {
  writePersonSensorLed(personSensorLedEnabled);
}

PersonResult pollPersonSensor() {
  PersonResult result = {false, false, 0.0f, 0, false};

  byte readData[PS_EXPECTED_BYTES];

#if SERIAL_DIAG
  // ===== CODEX DIAGNOSTIC INSERT BEGIN: rate limit sensor status output =====
  static unsigned long lastPsDiagMs = 0;
  // ===== CODEX DIAGNOSTIC INSERT END: rate limit sensor status output =====
#endif

  int available = Wire.requestFrom(PERSON_SENSOR_I2C_ADDRESS, PS_EXPECTED_BYTES);
  if (available < PS_EXPECTED_BYTES) {
#if SERIAL_DIAG
    // ===== CODEX DIAGNOSTIC INSERT BEGIN: failed sensor-read report =====
    if (millis() - lastPsDiagMs >= 500) {
      Serial.print("DIAG: Person Sensor read bytes=");
      Serial.println(available);
      lastPsDiagMs = millis();
    }
    // ===== CODEX DIAGNOSTIC INSERT END: failed sensor-read report =====
#endif
    delay(PERSON_SENSOR_DELAY);
    return result;  // ok=false — caller holds pan (matches original early return)
  }
  for (int i = 0; i < PS_EXPECTED_BYTES; i++) {
    readData[i] = Wire.read();
  }

  int offset = 0;

  // ===== CODEX DIAGNOSTIC INSERT BEGIN: safely decode packet metadata =====
  // Skip reserved header bytes and avoid undefined multiple increments in one expression.
  offset += 2;
  uint16_t payloadBytes = readData[offset] | (static_cast<uint16_t>(readData[offset + 1]) << 8);
  offset += 2;

  // Number of faces
  uint8_t numFaces = readData[offset++];

  struct Face {
    uint8_t boxConfidence;
    uint8_t boxLeft;
    uint8_t boxTop;
    uint8_t boxRight;
    uint8_t boxBottom;
    int8_t  idConfidence;
    uint8_t id;
    uint8_t isFacing;
  } faces[PERSON_SENSOR_FACE_MAX];

  // ===== CODEX DIAGNOSTIC INSERT BEGIN: consume the fixed four-face packet =====
  for (int i = 0; i < PERSON_SENSOR_FACE_MAX; i++) {
    faces[i].boxConfidence = readData[offset++];
    faces[i].boxLeft       = readData[offset++];
    faces[i].boxTop        = readData[offset++];
    faces[i].boxRight      = readData[offset++];
    faces[i].boxBottom     = readData[offset++];
    faces[i].idConfidence  = readData[offset++];
    faces[i].id            = readData[offset++];
    faces[i].isFacing      = readData[offset++];
  }
  // ===== CODEX DIAGNOSTIC INSERT END: consume the fixed four-face packet =====

  // Checksum
  uint16_t checksum = readData[offset] | (static_cast<uint16_t>(readData[offset + 1]) << 8);
  // ===== CODEX DIAGNOSTIC INSERT END: safely decode packet metadata =====

  // ===== CODEX DIAGNOSTIC INSERT BEGIN: reject invalid reported face count =====
  uint8_t reportedFaces = numFaces;
  if (numFaces > PERSON_SENSOR_FACE_MAX) {
    numFaces = 0;
  }
  // ===== CODEX DIAGNOSTIC INSERT END: reject invalid reported face count =====

  // Confidence + facing gate: reject low-confidence or non-facing detections
  if (numFaces > 0) {
    if (faces[0].boxConfidence < 60 || !faces[0].isFacing) {
      numFaces = 0;
    }
  }

  result.ok = true;

  if (numFaces > 0) {
    Face& mainFace = faces[0];
    result.faceVisible = true;
    result.faceCenterX = (mainFace.boxLeft + mainFace.boxRight) / 2.0;
    result.confidence  = mainFace.boxConfidence;
    result.isFacing    = mainFace.isFacing;
  }

#if SERIAL_DIAG
  // ===== CODEX DIAGNOSTIC INSERT BEGIN: visible face and pan telemetry =====
  if (millis() - lastPsDiagMs >= 500) {
    Serial.print("DIAG: PS bytes=");
    Serial.print(available);
    Serial.print(" reportedFaces=");
    Serial.print(reportedFaces);
    Serial.print(" acceptedFaces=");
    Serial.print(numFaces);
    Serial.print(" payloadBytes=");
    Serial.print(payloadBytes);
    Serial.print(" checksum=0x");
    Serial.print(checksum, HEX);
    if (reportedFaces > 0 && reportedFaces <= PERSON_SENSOR_FACE_MAX) {
      Serial.print(" conf=");
      Serial.print(faces[0].boxConfidence);
      Serial.print(" facing=");
      Serial.print(faces[0].isFacing);
    }
    Serial.print(" pan=");
    Serial.print((int)desiredPan);
    Serial.print(" pajOk=");
    Serial.println(pajOk ? 1 : 0);
    lastPsDiagMs = millis();
  }
  // ===== CODEX DIAGNOSTIC INSERT END: visible face and pan telemetry =====
#endif

  delay(PERSON_SENSOR_DELAY);
  return result;
}
