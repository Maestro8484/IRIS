// sleep_cfg.h — SleepCfg struct shared between sleep_renderer.h and mouth_tft.cpp.
// sleep_renderer.h defines sleepCfg; mouth_tft.cpp accesses it via this header
// without pulling in GC9A01A_t3n.h (which conflicts with ILI9341 headers).
#pragma once
#include <stdint.h>

struct SleepCfg {
    float   speed;           // global animation speed scalar (0.1–3.0)
    uint8_t starCount;       // informational (fixed star arrays used)
    uint8_t starBrightMin;   // 0–255 brightness floor
    uint8_t starBrightMax;   // 0–255 brightness ceiling
    uint8_t starTwinkleAmp;  // 0–255 twinkle swing
    uint8_t shootCount;      // shooting stars per eye (0–10, max 4 used)
    uint8_t shootSpeed;      // 0–255 scaled
    uint8_t shootLen;        // trail pixels
    uint8_t shootBright;     // 0–255
    uint8_t warpCount;       // warp particles per eye (0–60, max 48)
    uint8_t warpSpeed;       // 0–255 scaled
    uint8_t warpBright;      // 0–255
    uint8_t moonR;           // moon radius px
    uint8_t moonDrift;       // drift amplitude px
    uint8_t saturnR;         // saturn planet radius px
    uint8_t saturnDrift;     // drift amplitude px
    uint8_t nebulaAlpha;     // 0–255
    uint8_t waveAmp0;        // primary wave amplitude px
    uint8_t waveAmp1;        // secondary wave amplitude px
    uint8_t waveAmp2;        // tertiary wave amplitude px
    uint8_t waveOscAmp;      // whole-waveform vertical oscillation px
    uint8_t mouthPulseAlpha; // reserved (not used in firmware v3)
    uint8_t zzzAlpha0;       // 0–255 mapped to ZZZ color brightness
    uint8_t zzzAlpha1;
    uint8_t zzzAlpha2;
};

extern SleepCfg sleepCfg;
