#pragma once
#include <Arduino.h>

// Person Sensor I2C address
#define PERSON_SENSOR_I2C_ADDRESS 0x62

// ===== CODEX DIAGNOSTIC INSERT BEGIN: Person Sensor packet definition =====
// The Person Sensor returns four fixed face slots in every 39-byte result packet.
#define PERSON_SENSOR_FACE_MAX 4
#define PS_EXPECTED_BYTES (4 + 1 + (PERSON_SENSOR_FACE_MAX * 8) + 2)
// ===== CODEX DIAGNOSTIC INSERT END: Person Sensor packet definition =====

// Pause between sensor polls (ms)
#define PERSON_SENSOR_DELAY 50

// Compile-time DEFAULT for the Person Sensor LED (0 = off). Runtime state can be
// changed live via the PSLED=0/1 serial command (see setPersonSensorLed).
#define PERSON_SENSOR_LED_ENABLED 0

// Live LED state + setter. Lighting the LED is an I2C write to the sensor, so a
// lit LED is positive proof the sensor is powered and reachable on the bus.
extern bool personSensorLedEnabled;
void setPersonSensorLed(bool on);

// Result of one poll cycle.
//   ok          = a full valid packet was read this cycle. When false the
//                 caller must skip pan dispatch entirely (matches the original
//                 short-read early-return: pan is held, not driven).
//   faceVisible = an accepted, facing, high-confidence face is present.
struct PersonResult {
  bool    ok;
  bool    faceVisible;
  float   faceCenterX;
  uint8_t confidence;
  bool    isFacing;
};

void setupPersonSensor();
PersonResult pollPersonSensor();
