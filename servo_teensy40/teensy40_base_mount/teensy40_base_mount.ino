/*
IRIS project — Teensy 4.0
Pan-only servo controller with Person Sensor + PAJ7620U2 gesture sensor
Last revised: 2026-05-29 joe schmidt

Pin#   Label
2      Pan servo PWM
18     SDA  I2C shared bus (Person Sensor 0x62, PAJ7620U2 0x73)
19     SCL  I2C shared bus

USB serial / Pi4 integration:
- USB CDC serial to Pi4 (/dev/ttyIRIS_SERVO, baud 115200)
- Commands sent: VOL+, VOL-, STOP, RIGHT, FORWARD, BACKWARD, CW, CCW
- Pi4 hardware/base_mount_bridge.py daemon thread reads and dispatches these.
- Commands triggered by PAJ7620U2:
    UP gesture       → VOL+
    DOWN gesture     → VOL-
    LEFT gesture     → STOP
    RIGHT gesture    → RIGHT
    FORWARD gesture  → FORWARD
    BACKWARD gesture → BACKWARD
    CW gesture       → CW
    CCW gesture      → CCW

Modules (TS40-S1 refactor): paj7620.* gesture driver, person_sensor.* face
tracking decode, pan_servo.* ServoEasing wrapper, diag.h serial macros.
*/

#include <Wire.h>
#include "paj7620.h"
#include "person_sensor.h"
#include "pan_servo.h"
#include "diag.h"

extern "C" void _reboot_Teensyduino_(void);

// Set to 1 to print a one-time I2C scan 10s after boot (diagnostic only)
#define I2C_SCAN_DIAG 0

unsigned long lastFaceMs = 0;

void setup() {
  Wire.begin();
  setupPersonSensor();
  setupPanServo();

  Serial.begin(115200);
  delay(8000);  // hold boot diagnostics — open serial monitor within 8s of power cycle

#if SERIAL_DIAG
  // ===== CODEX DIAGNOSTIC INSERT BEGIN: boot status and I2C presence probe =====
  Serial.println("DIAG: boot; servo commanded center=90 on PWM pin 2");
  Wire.beginTransmission(PERSON_SENSOR_I2C_ADDRESS);
  Serial.print("DIAG: Person Sensor 0x62 ACK=");
  Serial.println(Wire.endTransmission() == 0 ? "YES" : "NO");
  Wire.beginTransmission(PAJ7620_ADDR);
  Serial.print("DIAG: PAJ7620U2 0x73 ACK=");
  Serial.println(Wire.endTransmission() == 0 ? "YES" : "NO");
  // ===== CODEX DIAGNOSTIC INSERT END: boot status and I2C presence probe =====
#endif

  pajOk = paj7620Init();
#if SERIAL_DIAG
  // ===== CODEX DIAGNOSTIC INSERT BEGIN: PAJ7620U2 init result =====
  Serial.print("DIAG: PAJ7620U2 init=");
  Serial.println(pajOk ? "OK" : "FAIL");
  // ===== CODEX DIAGNOSTIC INSERT END: PAJ7620U2 init result =====
#endif
}

void loop() {
  // Handle incoming serial commands
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd == "REBOOT") {
      _reboot_Teensyduino_();
    } else if (cmd.startsWith("PSLED=")) {
      setPersonSensorLed(cmd.substring(6).toInt() != 0);
      Serial.print("PSLED=");
      Serial.println(personSensorLedEnabled ? 1 : 0);  // ack so the bridge/WebUI can confirm
    } else {
      handleSerialPanCmd(cmd);  // PAN / PAN? (SERIAL_DIAG only)
    }
  }

#if I2C_SCAN_DIAG
  // One-time I2C scan 10s after boot — bridge has time to connect, output appears in journal
  static bool _i2cScanned = false;
  if (!_i2cScanned && millis() > 10000) {
    for (byte addr = 1; addr < 127; addr++) {
      Wire.beginTransmission(addr);
      if (Wire.endTransmission() == 0) {
        Serial.print("I2C_SCAN: 0x");
        Serial.println(addr, HEX);
      }
    }
    Serial.println("I2C_SCAN_DONE");
    _i2cScanned = true;
  }
#endif

  pollGesture();

  PersonResult ps = pollPersonSensor();
  if (!ps.ok) return;  // short read this cycle — hold pan (matches original early return)

  if (ps.faceVisible) {
    lastFaceMs = millis();
    updatePanFromFace(ps.faceCenterX);
  } else {
    updatePanIdle(millis() - lastFaceMs);
  }
}
