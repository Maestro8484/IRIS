/*
IRIS project — Teensy 4.0
Pan-only servo controller with Person Sensor + PAJ7620U2 gesture sensor
Last revised: 2026-05-29 IRIS project

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

// Firmware tag — printed unconditionally at boot on /dev/ttyIRIS_SERVO so the running
// build can be confirmed after a flash (grep the serial for "VER"). Bump every flash.
#define FW_VERSION "S243b"

// Serial protocol contract version (RD-048 move 2, S213). Bump when the serial
// command/emit contract changes (not on every flash). Baked into the boot VER
// line; pi4/core/protocol.py EXPECTED_SERVO_PROTOCOL must be bumped in the same
// commit. scripts/version_guard.py fails the flash preflight on silent drift.
#define PROTOCOL_VERSION 1

// Set to 1 to print a one-time I2C scan 10s after boot (diagnostic only)
#define I2C_SCAN_DIAG 0

// ---- Servo-rail power sense + soft-start (default OFF) ---------------------------
// The servo 5V rail is switched by the enclosure toggle while the Teensy runs
// continuously on Pi4 USB. Without sensing the rail, at power-on the DS3218MG slews at
// full speed to whatever PWM setpoint is live. This flag, once the divider in
// docs/servo_power_sense_softstart.md is physically wired, lets the firmware stop
// commanding while the rail is off (so the software angle stays equal to the frozen
// physical angle) and re-attach on power-on with a matching pulse — no jump.
// DO NOT set to 1 until the divider is wired and bench-verified (Teensy 4.0 GPIO is
// 3.3V-only; the raw 5V rail must be divided). See the doc.
#define SERVO_PWR_SENSE      0
#define SERVO_PWR_SENSE_PIN  14   // A0 — free, analog-capable; reads ~3.0V (rail on) / 0V (off)

unsigned long lastFaceMs = 0;

void setup() {
  Wire.begin();
  setupPersonSensor();
  setupPanServo();

  Serial.begin(115200);
  delay(8000);  // hold boot diagnostics — open serial monitor within 8s of power cycle

  Serial.print("VER servo fw ");
  Serial.print(FW_VERSION);
  Serial.print(" proto=");
  Serial.println(PROTOCOL_VERSION);

#if SERIAL_DIAG
  // Boot-time I2C presence probe. One-shot, so it costs nothing at runtime.
  Serial.println("DIAG: boot; servo commanded center=90 on PWM pin 2");
  Wire.beginTransmission(PERSON_SENSOR_I2C_ADDRESS);
  Serial.print("DIAG: Person Sensor 0x62 ACK=");
  Serial.println(Wire.endTransmission() == 0 ? "YES" : "NO");
  Wire.beginTransmission(PAJ7620_ADDR);
  Serial.print("DIAG: PAJ7620U2 0x73 ACK=");
  Serial.println(Wire.endTransmission() == 0 ? "YES" : "NO");
#endif

#if SERVO_PWR_SENSE
  // Rail-sense input. If the rail is off at boot, drop the servo so we never advance the
  // commanded angle while it is dark (keeps software angle == frozen physical angle).
  pinMode(SERVO_PWR_SENSE_PIN, INPUT);
  if (digitalRead(SERVO_PWR_SENSE_PIN) == LOW) {
    detachPanServo();
  }
#endif

  pajOk = paj7620Init();
#if SERIAL_DIAG
  Serial.print("DIAG: PAJ7620U2 init=");
  Serial.println(pajOk ? "OK" : "FAIL");
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

#if SERVO_PWR_SENSE
  // Servo-rail soft-start: freeze the commanded angle while the rail is off so it stays
  // equal to the frozen physical angle; re-attach at that angle on power-on (no jump).
  // Gestures above still run (they are serial-to-Pi4, independent of servo power).
  static bool railWasOn = true;
  bool railOn = (digitalRead(SERVO_PWR_SENSE_PIN) == HIGH);
  if (railOn && !railWasOn) attachPanServo();  // power returned → re-attach at frozen angle
  railWasOn = railOn;
  if (!railOn) {
    detachPanServo();  // rail off → stop PWM, hold software angle == physical angle
    return;            // skip pan tracking this cycle
  }
#endif

  PersonResult ps = pollPersonSensor();
  if (!ps.ok) return;  // short read this cycle — hold pan (matches original early return)

  if (ps.faceVisible) {
    lastFaceMs = millis();
    updatePanFromFace(ps.faceCenterX);
  } else {
    updatePanIdle(millis() - lastFaceMs);
  }
}
