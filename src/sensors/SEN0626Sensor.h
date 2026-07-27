#pragma once

#include <Arduino.h>

#define PERSON_SENSOR_MAX_FACES_COUNT (4)
#define PERSON_SENSOR_MAX_IDS_COUNT (7)

typedef struct __attribute__ ((__packed__)) {
  uint8_t reserved[2];
  uint16_t data_size;
} person_sensor_results_header_t;

typedef struct __attribute__ ((__packed__)) {
  uint8_t box_confidence;
  uint8_t box_left;
  uint8_t box_top;
  uint8_t box_right;
  uint8_t box_bottom;
  int8_t id_confidence;
  int8_t id;
  uint8_t is_facing;
} person_sensor_face_t;

typedef struct __attribute__ ((__packed__)) {
  person_sensor_results_header_t header;
  int8_t num_faces;
  person_sensor_face_t faces[PERSON_SENSOR_MAX_FACES_COUNT];
  uint16_t checksum;
} person_sensor_results_t;

// Drop-in replacement for the Useful Sensors Person Sensor (SEN-21231) used by
// IRIS. The person_sensor_face_t layout and the public method surface
// (begin/isPresent/read/enableID/setMode/enableLED/numFacesFound/faceDetails/
// timeSinceFaceDetectedMs) are byte-for-byte / signature-for-signature identical
// so IRIS's main.cpp consumes this unchanged. The raw* accessors below are
// CyclopsGaze-only calibration extras — IRIS never calls them, so they do not
// break the drop-in contract.
class SEN0626Sensor {
public:
  static constexpr uint8_t DEVICE_ADDR = 0x72;

  enum class Mode { Standby = 0x00, Continuous = 0x01 };

  explicit SEN0626Sensor(HardwareSerial &serial);

  bool begin();
  // Drop-in note (IRIS integration): the Useful Sensors PersonSensor this shim
  // replaces has NO begin() -- IRIS's detect loop probes the bus by calling
  // isPresent() directly (main.cpp ~498/539). So isPresent() lazily runs begin()
  // (UART bring-up + baud auto-detect) until the sensor answers, making the
  // existing IRIS probe loop work UNCHANGED with no added begin() call. Once
  // present it short-circuits. CyclopsGaze's own main.cpp still calls begin()
  // explicitly in setup(), so this is a no-op for the standalone path. begin()'s
  // internal BAUD_ATTEMPTS sweep replaces the loop's external ret/recover retry.
  bool isPresent() { if (!present) begin(); return present; }
  bool read();
  void enableID(bool) {}
  void setMode(Mode) {}
  void enableLED(bool) {}
  int numFacesFound() const { return num_faces; }
  person_sensor_face_t faceDetails(int faceNumber);
  unsigned long timeSinceFaceDetectedMs() { return (unsigned long)lastDetectionTimeMs; }

  // ── Calibration extras (CyclopsGaze bench only) ────────────────────────────
  // Raw sensor register values from the most recent successful read, BEFORE the
  // 0-255 remap. Used on the bench to confirm the native Y resolution (480 vs
  // 640 — NATIVE_H is assumed, unverified) and to compare raw score against the
  // shim's rescaled box_confidence. Not part of the Person Sensor contract.
  uint16_t rawFaceX() const { return lastRawX; }
  uint16_t rawFaceY() const { return lastRawY; }
  uint16_t rawScore() const { return lastRawScore; }
  long detectedBaud() const { return baud; }

private:
  static constexpr long SAMPLE_TIME_MS = 150;
  static constexpr uint16_t NATIVE_W = 640;
  // Assumed VGA height. DFRobot documents only the X range (0-640); Y is
  // unconfirmed. Verify on the bench via rawFaceY() (see NOTES.md bench step 6).
  static constexpr uint16_t NATIVE_H = 480;

  // SEN0626 boots an on-board AI model into RAM before it answers Modbus. Give
  // it a floor of settle time from power-on before the first probe, mirroring
  // IRIS's ~2.5 s wait for the Person Sensor's ML boot (IRIS main.cpp:493).
  static constexpr uint32_t BOOT_SETTLE_MS = 2000;
  // Number of full (all-baud) detection passes before begin() gives up. Guards
  // against a cold sensor missing the very first probe.
  static constexpr int      BAUD_ATTEMPTS  = 3;

  HardwareSerial &serial;
  bool present{false};
  long baud{0};
  int8_t num_faces{0};
  person_sensor_face_t face{};

  uint16_t lastRawX{0};
  uint16_t lastRawY{0};
  uint16_t lastRawScore{0};

  elapsedMillis timeSinceSampledMs{(uint32_t)SAMPLE_TIME_MS};
  elapsedMillis lastDetectionTimeMs{};

  bool tryBaud(long baud);
  uint16_t readFaceData(uint16_t *faceX, uint16_t *faceY, uint16_t *score);
};
