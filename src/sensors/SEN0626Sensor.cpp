#include "SEN0626Sensor.h"

// ── Modbus RTU helpers ────────────────────────────────────────────────────────

// Max time to wait for a full Modbus response AFTER the request has been sent
// (flush() already blocked until TX drained). A 4-register FC04 reply is 13
// bytes = ~13.5 ms of wire time at 9600 baud; add the sensor's turnaround and a
// normal round trip lands near ~25 ms. 100 ms is a ~4x safety margin yet caps a
// stalled-sensor eye freeze at 100 ms instead of the old 300 ms (audit 3.6).
static constexpr uint32_t MODBUS_RESP_TIMEOUT_MS = 100;

static uint16_t modbusCRC(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (int j = 0; j < 8; j++) {
      if (crc & 0x0001) crc = (crc >> 1) ^ 0xA001;
      else crc >>= 1;
    }
  }
  return crc;
}

// FC04: Read Input Registers — addr=device address, reg=first register, count=number of registers.
// Returns count*2 payload bytes into buf. Returns true on success.
static bool modbusReadInputRegs(HardwareSerial &ser, uint8_t addr,
                                 uint16_t reg, uint8_t count, uint8_t *buf) {
  uint8_t req[8];
  req[0] = addr;
  req[1] = 0x04;
  req[2] = reg >> 8;
  req[3] = reg & 0xFF;
  req[4] = 0x00;
  req[5] = count;
  uint16_t crc = modbusCRC(req, 6);
  req[6] = crc & 0xFF;
  req[7] = crc >> 8;

  while (ser.available()) ser.read();
  ser.write(req, 8);
  ser.flush();

  // Response: addr(1) + FC(1) + byte_count(1) + data(count*2) + CRC(2)
  const uint8_t respLen = 5 + count * 2;
  uint32_t deadline = millis() + MODBUS_RESP_TIMEOUT_MS;
  while (ser.available() < respLen && millis() < deadline) {}
  if (ser.available() < respLen) return false;

  uint8_t resp[32];
  for (uint8_t i = 0; i < respLen; i++) resp[i] = ser.read();

  // Validate CRC
  uint16_t rcrc = modbusCRC(resp, respLen - 2);
  if (resp[respLen - 2] != (rcrc & 0xFF) || resp[respLen - 1] != (rcrc >> 8)) return false;

  // Validate header
  if (resp[0] != addr || resp[1] != 0x04 || resp[2] != count * 2) return false;

  memcpy(buf, &resp[3], count * 2);
  return true;
}

// ── SEN0626Sensor ─────────────────────────────────────────────────────────────

SEN0626Sensor::SEN0626Sensor(HardwareSerial &serial) : serial(serial) {}

bool SEN0626Sensor::tryBaud(long testBaud) {
  serial.begin(testBaud);
  delay(200);  // let the UART line settle (the AI-model boot is covered by BOOT_SETTLE_MS)
  // Read PID register (input register 0x00), expect 0x0272
  uint8_t buf[2];
  if (modbusReadInputRegs(serial, DEVICE_ADDR, 0x00, 1, buf)) {
    uint16_t pid = (buf[0] << 8) | buf[1];
    if (pid == 0x0272) return true;
  }
  serial.end();
  return false;
}

bool SEN0626Sensor::begin() {
  // The SEN0626 loads an AI model into RAM on power-up and will not answer
  // Modbus until that completes. Hold off the first probe until the sensor has
  // had BOOT_SETTLE_MS since power-on (audit 3.9).
  while (millis() < BOOT_SETTLE_MS) {}

  // Try 115200 first per spec, then 9600 (factory default). Retry the whole
  // sweep a few times so a cold sensor that misses the first probe is still
  // found rather than leaving tracking dead for the session.
  for (int attempt = 0; attempt < BAUD_ATTEMPTS; attempt++) {
    for (long testBaud : {115200L, 9600L}) {
      if (tryBaud(testBaud)) {
        baud = testBaud;
        present = true;
        Serial.printf("[CG] SEN0626 found at %ld (attempt %d)\n", testBaud, attempt + 1);
        return true;
      }
    }
    delay(300);
  }
  Serial.println("[CG] SEN0626 NOT FOUND at 115200 or 9600 -- check wiring");
  present = false;
  return false;
}

// Reads face count, X, Y, score in a single Modbus transaction.
// Registers 0x04..0x07 are contiguous: face_number, face_x, face_y, face_score.
// Returns face count (0 if read fails). Sets faceX, faceY, score on success.
uint16_t SEN0626Sensor::readFaceData(uint16_t *faceX, uint16_t *faceY, uint16_t *score) {
  uint8_t buf[8];
  if (!modbusReadInputRegs(serial, DEVICE_ADDR, 0x04, 4, buf)) return 0;
  uint16_t count = (buf[0] << 8) | buf[1];
  *faceX  = (buf[2] << 8) | buf[3];
  *faceY  = (buf[4] << 8) | buf[5];
  *score  = (buf[6] << 8) | buf[7];
  return count;
}

bool SEN0626Sensor::read() {
  if (!present) return false;
  if (timeSinceSampledMs < (uint32_t)SAMPLE_TIME_MS) return false;
  timeSinceSampledMs = 0;

  uint16_t faceX, faceY, score;
  uint16_t count = readFaceData(&faceX, &faceY, &score);

  if (count == 0) {
    num_faces = 0;
    return true;
  }

  // Preserve the raw register values for bench calibration (rawFace*()).
  lastRawX = faceX;
  lastRawY = faceY;
  lastRawScore = score;

  // Clamp and remap center coords to 0-255. X and Y are normalised over their
  // OWN native span (640 wide, 480 tall) so a face at either frame edge drives
  // the target to full deflection on that axis -- correct edge-to-edge gaze
  // mapping despite the 4:3 vs 1:1 aspect mismatch (audit 3.4).
  faceX = min(faceX, (uint16_t)NATIVE_W);
  faceY = min(faceY, (uint16_t)NATIVE_H);
  score = min(score, (uint16_t)100);

  uint8_t cx = (uint8_t)((uint32_t)faceX * 255 / NATIVE_W);
  uint8_t cy = (uint8_t)((uint32_t)faceY * 255 / NATIVE_H);

  // IRIS S212 -- DELIBERATE DIVERGENCE from CyclopsGaze, which emits
  // score*255/100 (0-255) and gates at PS_CONF_GATE=152 (CG-S5).
  //
  // IRIS gates on `face.box_confidence > psConfGate` (main.cpp) and psConfGate is
  // clamped to 0-100 (main.cpp PS_CFG:CONF handler). Emitting 0-255 would make
  // every real detection (raw 60-90 -> 153-229) clear any reachable gate, silently
  // rendering the operator's CONF knob inert. Emitting the raw score makes CONF
  // mean exactly what DFRobot documents: a score >= 60 is a valid face
  // (wiki.dfrobot.com/sen0626). score is already clamped to 0-100 above.
  //
  // NOTE the 0-100 clamp was ALREADY a latent mismatch against the old Person
  // Sensor, which also reported box_confidence on a 0-255 scale -- the gate could
  // never be set above 100/255 (~39%). This change is the first time CONF means a
  // real percentage.
  //
  // LIVE VALUE IS NOT THE DEFAULT: assistant.py PS_CFG_DEFAULTS says CONF=60, but
  // /home/pi/ps_config.json overrides it and read CONF=25 / FACING=0 / LOST_MS=8500
  // on 2026-07-16. CONF=25 was tuned against the Person Sensor's 0-255 scale (~10%)
  // and is far below DFRobot's 60 floor on this scale -- leaving it at 25 makes the
  // SEN0626 track detections the vendor considers invalid. Raise ps_config.json to
  // CONF=60 with the swap (operator decision -- do not change it silently).
  //
  // If this shim is ever synced back to CyclopsGaze, set its PS_CONF_GATE 152 -> 60
  // and re-run the CG-S6/S7/S8 bench. See 09_IRIS_INTEGRATION_PLAN.md section 5.
  face.box_confidence = (uint8_t)score;
  // SEN0626 reports a face center, not a true bounding box. Store that center
  // in both edges so consumers recover the exact target even at frame edges
  // (box_left==box_right==cx -> IRIS's (left+(right-left)/2) collapses to cx
  // exactly, no edge drift at any value incl 0 and 255 -- audit 3.3).
  face.box_left = cx;
  face.box_right = cx;
  face.box_top = cy;
  face.box_bottom = cy;
  face.id_confidence = 0;
  face.id = 0;
  face.is_facing = 1;

  num_faces = 1;
  lastDetectionTimeMs = 0;
  return true;
}

person_sensor_face_t SEN0626Sensor::faceDetails(int faceNumber) {
  if (faceNumber >= num_faces) return person_sensor_face_t{};
  return face;
}
