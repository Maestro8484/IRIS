# IRIS Base Mount Controller — Teensy 4.0 Firmware

**Board:** Teensy 4.0 (iteration 7 — current active board)
**Status:** Active. DS3218MG installed and operational. Firmware current as of TS40-S1/TS40-S2 (modular split, all 8 PAJ7620U2 gestures, debounce/cooldown, PAN_MIN/MAX, PAN? query). REPO-ONLY pending replacement sensor and user flash.

## Hardware

- Pan servo: Miuzei DS3218MG MS24 Digital Servo (25kg metal gear), pin 2 (hardware PWM), shared I2C bus with Person Sensor
- I2C SDA: pin 18 (Wire default, shared bus)
- I2C SCL: pin 19 (Wire default, shared bus)
- Person Sensor: addr 0x62
- PAJ7620U2 gesture sensor (HiLetgo): addr 0x73
- Current sensor status: HW-004 BLOCKED — installed PAJ7620U2 confirmed dead; replacement GY-PAJ7620 on order
- Pi4 comms: USB CDC serial at 115200 baud (/dev/ttyIRIS_SERVO)
- Servo power: external 5V supply (not from Teensy rail)

## Wiring reference

`docs/servo_teensy40_wiring.md`

## Firmware

`teensy40_base_mount/teensy40_base_mount.ino` — orchestration sketch (setup/loop). Subsystems are modular: `paj7620.h/.cpp` (gesture driver), `person_sensor.h/.cpp` (face decode + tracking gate), `pan_servo.h/.cpp` (ServoEasing wrapper), `diag.h` (serial diagnostic macros). Build with PlatformIO env `teensy40`, user performs upload.

See `docs/servo_teensy40_wiring.md` for full pin/wiring/power detail.
