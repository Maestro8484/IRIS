#include "paj7620.h"
#include <Wire.h>

// Rotation table — compile-time remapping from physical mount to logical direction.
// Reg 0x43 raw bit layout (datasheet v1.5 p.24):
//   Bit[0]=Left Bit[1]=Right Bit[2]=Down Bit[3]=Up  Bit[4]=Fwd Bit[5]=Back Bit[6]=CW Bit[7]=CCW
#if   GESTURE_MOUNT_DEGREES == 0
  #define GEST_UP    0x08
  #define GEST_DOWN  0x04
  #define GEST_LEFT  0x01
  #define GEST_RIGHT 0x02
#elif GESTURE_MOUNT_DEGREES == 90
  #define GEST_UP    0x01
  #define GEST_DOWN  0x02
  #define GEST_LEFT  0x04
  #define GEST_RIGHT 0x08
#elif GESTURE_MOUNT_DEGREES == 180
  #define GEST_UP    0x04
  #define GEST_DOWN  0x08
  #define GEST_LEFT  0x02
  #define GEST_RIGHT 0x01
#elif GESTURE_MOUNT_DEGREES == 270
  #define GEST_UP    0x02
  #define GEST_DOWN  0x01
  #define GEST_LEFT  0x08
  #define GEST_RIGHT 0x04
#else
  #error "GESTURE_MOUNT_DEGREES must be 0, 90, 180, or 270"
#endif

bool pajOk = false;

static void paj_write(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(PAJ7620_ADDR);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}

static uint8_t paj_read(uint8_t reg) {
  Wire.beginTransmission(PAJ7620_ADDR);
  Wire.write(reg);
  Wire.endTransmission();
  Wire.requestFrom((uint8_t)PAJ7620_ADDR, (uint8_t)1);
  return Wire.available() ? Wire.read() : 0;
}

bool paj7620Init() {
  // Wake device from suspend (any I2C access wakes it), then wait for ready
  Wire.beginTransmission(PAJ7620_ADDR);
  Wire.endTransmission();
  delay(700);

  // Confirm device responds at 0x73
  Wire.beginTransmission(PAJ7620_ADDR);
  if (Wire.endTransmission() != 0) return false;

  // Bank 0 config — PixArt official initial_register_array (datasheet v1.5 §8.1).
  // Prior version was a hand-abbreviated ~60-write subset with several drifted
  // values (e.g. 0x82, 0x90, 0xCF) and omissions (0x83/0x84/0x86/0x91/0x93/0x94/
  // 0x9F/0xA0) that left gesture sensitivity/FOV tuning at chip power-on
  // defaults instead of PixArt's validated settings — root cause of missed/
  // inconsistent swipe detection.
  paj_write(0xEF, 0x00);
  paj_write(0x41, 0xFF); paj_write(0x42, 0x01); paj_write(0x46, 0x2D);
  paj_write(0x47, 0x0F); paj_write(0x48, 0x80); paj_write(0x49, 0x00);
  paj_write(0x4A, 0x40); paj_write(0x4B, 0x00); paj_write(0x4C, 0x20);
  paj_write(0x4D, 0x00); paj_write(0x51, 0x10); paj_write(0x5C, 0x02);
  paj_write(0x5E, 0x10); paj_write(0x80, 0x41); paj_write(0x81, 0x44);
  paj_write(0x82, 0x0C); paj_write(0x83, 0x20); paj_write(0x84, 0x20);
  paj_write(0x85, 0x00); paj_write(0x86, 0x10); paj_write(0x87, 0x00);
  paj_write(0x8B, 0x01); paj_write(0x8D, 0x00); paj_write(0x90, 0x0C);
  paj_write(0x91, 0x0C); paj_write(0x93, 0x0D); paj_write(0x94, 0x0A);
  paj_write(0x95, 0x0A); paj_write(0x96, 0x0C); paj_write(0x97, 0x05);
  paj_write(0x9A, 0x14); paj_write(0x9C, 0x3F); paj_write(0x9F, 0xF9);
  paj_write(0xA0, 0x48); paj_write(0xA5, 0x19); paj_write(0xCC, 0x19);
  paj_write(0xCD, 0x0B); paj_write(0xCE, 0x13); paj_write(0xCF, 0x62);
  paj_write(0xD0, 0x21);

  // Bank 1 config — PixArt official initial_register_array (cont'd).
  paj_write(0xEF, 0x01);
  paj_write(0x00, 0x1E); paj_write(0x01, 0x1E); paj_write(0x02, 0x0F);
  paj_write(0x03, 0x0F); paj_write(0x04, 0x02); paj_write(0x25, 0x01);
  paj_write(0x26, 0x00); paj_write(0x27, 0x39); paj_write(0x28, 0x7F);
  paj_write(0x29, 0x08); paj_write(0x30, 0x03); paj_write(0x3E, 0xFF);
  paj_write(0x5E, 0x3D); paj_write(0x65, 0xAC); paj_write(0x66, 0x00);
  paj_write(0x67, 0x97); paj_write(0x68, 0x01); paj_write(0x69, 0xCD);
  paj_write(0x6A, 0x01); paj_write(0x6B, 0xB0); paj_write(0x6C, 0x04);
  paj_write(0x6D, 0x2C); paj_write(0x6E, 0x01); paj_write(0x72, 0x01);
  paj_write(0x73, 0x35); paj_write(0x74, 0x00); paj_write(0x77, 0x01);

  // Back to bank 0 (gesture interrupts already unmasked + gesture mode
  // enabled by the 0x41/0x42 writes at the top of this array).
  paj_write(0xEF, 0x00);

  return true;
}

void pollGesture() {
  // Debounce state — survives across calls, reset only on reboot.
  static bool          _firstPoll    = true;
  static unsigned long _lastGestMs[8] = {0, 0, 0, 0, 0, 0, 0, 0};
  static unsigned long _lastAnyMs    = 0;

  if (!pajOk) return;
  paj_write(0xEF, 0x00);  // ensure bank 0 (defensive — keeps bank state correct after any reset)

  // PAJ7620U2 returns stale data on first read after bank 0 select; discard it.
  if (_firstPoll) {
    paj_read(0x43);
    _firstPoll = false;
    return;
  }

  uint8_t gest = paj_read(0x43);
  if (gest == 0) return;

  // Map the lowest set bit to a 0–7 index for per-gesture cooldown lookup.
  uint8_t bitPos = 0;
  for (; bitPos < 8; bitPos++) {
    if (gest & (1u << bitPos)) break;
  }

  unsigned long now = millis();

  // Global cooldown: any gesture within GESTURE_COOLDOWN_MS of the last one is suppressed.
  if ((now - _lastAnyMs) < (unsigned long)GESTURE_COOLDOWN_MS) {
#if SERIAL_DIAG
    Serial.print("DIAG: PAJ7620 gest=0x");
    Serial.print(gest, HEX);
    Serial.println(" SUPPRESSED cooldown");
#endif
    return;
  }

  // Per-gesture debounce: same gesture within GESTURE_DEBOUNCE_MS is suppressed.
  if ((now - _lastGestMs[bitPos]) < (unsigned long)GESTURE_DEBOUNCE_MS) {
#if SERIAL_DIAG
    Serial.print("DIAG: PAJ7620 gest=0x");
    Serial.print(gest, HEX);
    Serial.println(" SUPPRESSED debounce");
#endif
    return;
  }

  // Gesture passes — record timestamps before emit.
  _lastAnyMs          = now;
  _lastGestMs[bitPos] = now;

#if SERIAL_DIAG
  // ===== CODEX DIAGNOSTIC INSERT BEGIN: PAJ7620U2 raw gesture byte =====
  // Prints raw register value and physical direction (rotation-corrected via GEST_* defines).
  Serial.print("DIAG: PAJ7620 gest=0x");
  Serial.print(gest, HEX);
  if      (gest & GEST_UP)    Serial.print(" UP");
  else if (gest & GEST_DOWN)  Serial.print(" DOWN");
  else if (gest & GEST_LEFT)  Serial.print(" LEFT");
  else if (gest & GEST_RIGHT) Serial.print(" RIGHT");
  else if (gest & 0x10)       Serial.print(" FORWARD");
  else if (gest & 0x20)       Serial.print(" BACKWARD");
  else if (gest & 0x40)       Serial.print(" CW");
  else if (gest & 0x80)       Serial.print(" CCW");
  Serial.println();
  // ===== CODEX DIAGNOSTIC INSERT END: PAJ7620U2 raw gesture byte =====
#endif

  // Emit command — do not change these strings, Pi4 base_mount_bridge.py depends on them
  if      (gest & GEST_UP)    Serial.println("VOL+");
  else if (gest & GEST_DOWN)  Serial.println("VOL-");
  else if (gest & GEST_LEFT)  Serial.println("STOP");
  else if (gest & GEST_RIGHT) Serial.println("RIGHT");
  else if (gest & 0x10)       Serial.println("FORWARD");
  else if (gest & 0x20)       Serial.println("BACKWARD");
  else if (gest & 0x40)       Serial.println("CW");
  else if (gest & 0x80)       Serial.println("CCW");
}
