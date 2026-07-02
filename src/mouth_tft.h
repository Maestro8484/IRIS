// mouth_tft.h — ILI9341 2.8" TFT mouth driver public API
// Implementation lives in mouth_tft.cpp (separate TU) to avoid
// header conflicts between ILI9341_t3.h and GC9A01A_t3n's ILI9341_fonts.h.
//
// Expression index:
//   0=NEUTRAL  1=HAPPY  2=CURIOUS  3=ANGRY
//   4=SLEEPY   5=SURPRISED  6=SAD  7=CONFUSED  8=SLEEP/OFF  9=SILLY

#pragma once
#include <Arduino.h>

void mouthTFTInit();
void mouthTFTShow(uint8_t idx);

// Same names as mouth.h stubs so main.cpp call sites are unchanged
void mouthSetSleepIntensity();
void mouthRestoreIntensity();
void mouthSetIntensity(uint8_t level);
void mouthSleepFrame();
void mouthSleepReset();

// Idle animation engine
void mouthIdleStart();
void mouthIdleStop();
void mouthIdleTick(uint32_t nowMs);
bool mouthIdleIsActive();

// RD-030 #2: redraw the idle resting face tinted toward the last expressed mood.
void mouthApplyIdleTint();
// RD-030 #3: one-shot "noticed you" greet when a person enters frame while idle.
void mouthGreet();
