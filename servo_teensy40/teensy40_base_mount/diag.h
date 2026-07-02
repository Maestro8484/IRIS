#pragma once
#include <Arduino.h>

// Serial diagnostic wrappers shared across servo + sensor modules.
// SERIAL_DIAG master switch is owned by paj7620.h (TS40-S2). Define it
// defensively here so this header is self-sufficient if included first.
#ifndef SERIAL_DIAG
#define SERIAL_DIAG 1
#endif

#if SERIAL_DIAG
  #define DIAG_PRINT(...)   Serial.print(__VA_ARGS__)
  #define DIAG_PRINTLN(...) Serial.println(__VA_ARGS__)
  #define DIAG_PRINTF(...)  Serial.printf(__VA_ARGS__)
#else
  #define DIAG_PRINT(...)
  #define DIAG_PRINTLN(...)
  #define DIAG_PRINTF(...)
#endif
