// sleep_renderer.h — Cosmic sleep scene on both GC9A01A eye TFTs.
// v3 (S65): Saturn + repositioned Moon, warp particles, SleepCfg, reduced speed.
// ZZZ moved to mouth only (mouth_tft.cpp mouthSleepFrame). Call sites unchanged.
#pragma once
#include <Arduino.h>
#include <math.h>
#include <GC9A01A_t3n.h>
#include "sleep_cfg.h"

// ---------------------------------------------------------------------------
// SleepCfg instance — defaults match HTML v8; overridden by SLEEP_CFG: serial
// ---------------------------------------------------------------------------
SleepCfg sleepCfg = {
    0.85f, // speed (reduced from 1.0 — user requested slower pace)
    110,   // starCount (informational, fixed arrays used)
    115,   // starBrightMin
    205,   // starBrightMax
    140,   // starTwinkleAmp
    4,     // shootCount (per eye, max SR_SHOOT_COUNT)
    38,    // shootSpeed
    55,    // shootLen
    210,   // shootBright
    32,    // warpCount (per eye, max SR_WARP_MAX)
    28,    // warpSpeed
    175,   // warpBright
    28,    // moonR (px)
    3,     // moonDrift
    18,    // saturnR
    4,     // saturnDrift
    44,    // nebulaAlpha
    28,    // waveAmp0
    18,    // waveAmp1
    10,    // waveAmp2
    34,    // waveOscAmp
    140,   // mouthPulseAlpha (used by mouth_tft.cpp)
    191,   // zzzAlpha0
    158,   // zzzAlpha1
    128,   // zzzAlpha2
};

// ---------------------------------------------------------------------------
// COLORS (RGB565)
// ---------------------------------------------------------------------------
static constexpr uint16_t SR_BLACK      = 0x0000;
static constexpr uint16_t SR_WARM_WHITE = 0xE71F;
static constexpr uint16_t SR_BLUE_WHITE = 0xAD7F;
static constexpr uint16_t SR_ICE_BLUE   = 0x9CDF;
static constexpr uint16_t SR_GOLD       = 0xFD40;
// Moon
static constexpr uint16_t SR_MOON_BASE  = 0xE79F;  // warm white
static constexpr uint16_t SR_MOON_GLOW  = 0x2104;  // very faint glow
static constexpr uint16_t SR_MOON_GLOW2 = 0x4228;  // brighter inner glow
// Nebula
static constexpr uint16_t SR_NEBULA_PUR = 0x3802;
static constexpr uint16_t SR_NEBULA_BLU = 0x0005;
static constexpr uint16_t SR_NEBULA_MAG = 0x2802;
// Saturn palette (RGB565 approx of HTML v8 colors)
static constexpr uint16_t SR_SAT_BODY   = 0xCCCA;  // mid golden  #c8a050
static constexpr uint16_t SR_SAT_SPEC   = 0xF6F4;  // bright highlight #f5dfa0
static constexpr uint16_t SR_SAT_BAND   = 0x8B46;  // dark band stripe #8a6830
static constexpr uint16_t SR_RING_BACK  = 0x4184;  // dim back ring
static constexpr uint16_t SR_RING_FRONT = 0xACEC;  // bright front ring
// Warp particle colors
static constexpr uint16_t SR_WARP_WHITE = 0xFFFF;
static constexpr uint16_t SR_WARP_BLUE  = 0x9EFF;  // ice blue-white
static constexpr uint16_t SR_WARP_AMBER = 0xFD20;  // amber

// ---------------------------------------------------------------------------
// DISPLAY SIZE
// ---------------------------------------------------------------------------
static constexpr int SR_W  = 240;
static constexpr int SR_H  = 240;
static constexpr int SR_CX = 120;
static constexpr int SR_CY = 120;

// ---------------------------------------------------------------------------
// STARS — 40 per display, 4 layers (unchanged from v2)
// ---------------------------------------------------------------------------
struct SrStar {
    uint8_t  x, y;
    uint8_t  r;
    uint16_t period;
    uint16_t phase;
    uint16_t color;
};

static constexpr int SR_STAR_COUNT = 40;

static constexpr SrStar SR_STARS_L[40] = {
    // Layer 0 — 4 giant stars
    {  32,  18, 4, 1800,    0, SR_WARM_WHITE },
    { 210, 155, 4, 2200,  700, SR_ICE_BLUE   },
    {  60, 195, 4, 2000,  350, SR_BLUE_WHITE },
    { 175,  30, 4, 2400, 1100, SR_GOLD       },
    // Layer 1 — 8 big stars
    { 195,  40, 3, 2300,  200, SR_BLUE_WHITE },
    {  70, 200, 3, 2500, 1500, SR_WARM_WHITE },
    {  50,  90, 3, 2700,  400, SR_BLUE_WHITE },
    { 170,  25, 3, 2600, 1000, SR_ICE_BLUE   },
    { 115,  50, 3, 2400,  600, SR_GOLD       },
    { 230, 100, 3, 2800,  900, SR_WARM_WHITE },
    {  18, 155, 3, 2200,  150, SR_ICE_BLUE   },
    { 145, 205, 3, 2900, 1300, SR_BLUE_WHITE },
    // Layer 2 — 12 medium stars
    { 100,  55, 2, 3000,  250, SR_WARM_WHITE },
    { 148, 190, 2, 3400, 1000, SR_BLUE_WHITE },
    {  20, 145, 2, 3600,  700, SR_ICE_BLUE   },
    { 220,  90, 2, 3800, 1500, SR_WARM_WHITE },
    {  60, 230, 2, 3200,  350, SR_BLUE_WHITE },
    { 185, 210, 2, 4000, 1100, SR_ICE_BLUE   },
    {  90, 170, 2, 3500,  850, SR_WARM_WHITE },
    { 230,  50, 2, 3700, 1300, SR_BLUE_WHITE },
    { 130,  80, 2, 3300,  500, SR_ICE_BLUE   },
    {  80, 115, 2, 4200,  200, SR_GOLD       },
    { 200, 175, 2, 3900,  800, SR_WARM_WHITE },
    {  45,  30, 2, 3100, 1200, SR_BLUE_WHITE },
    // Layer 3 — 16 small stars
    {  15,  60, 1, 4000,  150, SR_ICE_BLUE   },
    { 200, 130, 1, 4800,  750, SR_BLUE_WHITE },
    {  45, 180, 1, 5400, 1400, SR_ICE_BLUE   },
    { 165,  75, 1, 4400,  950, SR_WARM_WHITE },
    { 235, 200, 1, 5600,  300, SR_ICE_BLUE   },
    {  80,  30, 1, 4600, 1600, SR_BLUE_WHITE },
    { 120, 220, 1, 5000,  550, SR_ICE_BLUE   },
    { 225, 160, 1, 4300,  100, SR_WARM_WHITE },
    {  30, 115, 1, 5200,  850, SR_ICE_BLUE   },
    { 155,  35, 1, 4500, 1250, SR_BLUE_WHITE },
    { 190,  80, 1, 5800,  450, SR_GOLD       },
    {  10, 200, 1, 4700,  700, SR_ICE_BLUE   },
    { 110, 140, 1, 5100, 1100, SR_WARM_WHITE },
    {  65,  12, 1, 4900,  300, SR_ICE_BLUE   },
    { 140,   8, 1, 5300, 1500, SR_BLUE_WHITE },
    { 215,  22, 1, 6000,  600, SR_ICE_BLUE   },
};

static constexpr SrStar SR_STARS_R[40] = {
    // Layer 0 — 4 giant stars
    {  45,  30, 4, 1900,  300, SR_WARM_WHITE },
    { 185,  55, 4, 2100,  900, SR_GOLD       },
    {  25, 160, 4, 2300,  600, SR_ICE_BLUE   },
    { 215, 140, 4, 2000, 1500, SR_BLUE_WHITE },
    // Layer 1 — 8 big stars
    { 160,  18, 3, 2500, 1100, SR_ICE_BLUE   },
    { 110,  65, 3, 2600,  400, SR_WARM_WHITE },
    {  35, 100, 3, 2800,  650, SR_ICE_BLUE   },
    { 215,  80, 3, 2400, 1600, SR_WARM_WHITE },
    {  70, 225, 3, 2200,  250, SR_BLUE_WHITE },
    { 190, 205, 3, 2900, 1200, SR_ICE_BLUE   },
    { 100, 165, 3, 2700,  700, SR_WARM_WHITE },
    { 235,  45, 3, 2300, 1350, SR_BLUE_WHITE },
    // Layer 2 — 12 medium stars
    { 140,  90, 2, 3200,  500, SR_ICE_BLUE   },
    {  20,  50, 2, 4000,  100, SR_ICE_BLUE   },
    { 205, 120, 2, 4600,  800, SR_BLUE_WHITE },
    { 120, 175, 2, 5200, 1450, SR_ICE_BLUE   },
    { 175,  70, 2, 4400, 1000, SR_WARM_WHITE },
    { 230, 195, 2, 5400,  350, SR_ICE_BLUE   },
    {  90,  25, 2, 4600, 1550, SR_BLUE_WHITE },
    { 125, 215, 2, 4900,  600, SR_ICE_BLUE   },
    { 220, 150, 2, 4300,  150, SR_WARM_WHITE },
    {  40, 105, 2, 5000,  900, SR_ICE_BLUE   },
    { 150,  40, 2, 4500, 1300, SR_BLUE_WHITE },
    {  75, 135, 2, 3800,  450, SR_GOLD       },
    // Layer 3 — 16 small stars
    {  10,  75, 1, 4100,  200, SR_ICE_BLUE   },
    { 210,  40, 1, 4800,  700, SR_BLUE_WHITE },
    {  50, 150, 1, 5500, 1200, SR_ICE_BLUE   },
    { 170, 100, 1, 4300,  950, SR_WARM_WHITE },
    { 235, 175, 1, 5700,  400, SR_ICE_BLUE   },
    {  95,  45, 1, 4600, 1600, SR_BLUE_WHITE },
    { 130, 225, 1, 5100,  550, SR_ICE_BLUE   },
    { 215, 155, 1, 4200,  100, SR_WARM_WHITE },
    {  30, 120, 1, 5300,  850, SR_ICE_BLUE   },
    { 155,  35, 1, 4400, 1250, SR_BLUE_WHITE },
    { 185,  90, 1, 5900,  450, SR_GOLD       },
    {  15,  30, 1, 4700,  700, SR_ICE_BLUE   },
    { 110, 140, 1, 5100, 1100, SR_WARM_WHITE },
    {  65,  20, 1, 4900,  300, SR_ICE_BLUE   },
    { 145,  10, 1, 5300, 1500, SR_BLUE_WHITE },
    { 220,  25, 1, 6000,  600, SR_ICE_BLUE   },
};

// ---------------------------------------------------------------------------
// WARP PARTICLES — radiating dots from center outward
// ---------------------------------------------------------------------------
static constexpr int SR_WARP_MAX = 48;

struct SrWarpPt {
    float   angle;
    float   r;
    float   speed;
    float   maxR;
    uint8_t hue;   // 0=white 1=blue 2=amber
};

static SrWarpPt srWarpsL[SR_WARP_MAX];
static SrWarpPt srWarpsR[SR_WARP_MAX];

// ---------------------------------------------------------------------------
// STATE
// ---------------------------------------------------------------------------
static bool     srInitialized = false;
static uint32_t srLastFrameMs = 0;
static constexpr uint32_t SR_FRAME_MS = 155;  // ~6.4fps — relaxed sleep pace

// Moon positions — top-right (left eye) and top-left (right eye)
static constexpr int SR_MOON_L_CX = 175;
static constexpr int SR_MOON_L_CY =  48;
static constexpr int SR_MOON_R_CX =  65;
static constexpr int SR_MOON_R_CY =  48;

// Saturn positions — bottom-left (left eye) and bottom-right (right eye)
static constexpr int SR_SAT_L_CX =  55;
static constexpr int SR_SAT_L_CY = 185;
static constexpr int SR_SAT_R_CX = 185;
static constexpr int SR_SAT_R_CY = 185;

// Pulse rings — 3 expanding rings from center
static constexpr float SR_RING_SPEED = 0.5f;  // slightly slower than v2
static constexpr float SR_RING_MIN   = 28.0f;
static constexpr float SR_RING_MAX   = 66.0f;
static float srRingR[3] = { 28.0f, 40.0f, 52.0f };

// Shooting stars — 4 per display
static constexpr int      SR_SHOOT_COUNT    = 4;
static constexpr uint32_t SR_SHOOT_INTERVAL = 4500;  // ms per traverse (slower)
static constexpr int      SR_SHOOT_TRAIL    = 40;

static uint32_t srShootTimerL[4] = {0, 0, 0, 0};
static uint32_t srShootTimerR[4] = {0, 0, 0, 0};

static constexpr int SR_SHOOT_L[4][4] = {
    {  18,  42,  90,  18 },
    { 128, 162, 178, 142 },
    { 210,  68, 155,  38 },
    { 185, 215, 230, 195 },
};
static constexpr int SR_SHOOT_R[4][4] = {
    { 222,  35, 148,  12 },
    {  52, 168,  15, 148 },
    {  28,  88,  72,  55 },
    { 198, 218, 152, 200 },
};

// ---------------------------------------------------------------------------
// HELPERS
// ---------------------------------------------------------------------------

static inline float srBrightness(uint32_t nowMs, uint16_t period, uint16_t phase) {
    float t = (float)((nowMs + phase) % period) / (float)period;
    float s = 0.5f + 0.5f * sinf(t * 6.2832f);
    return s * s;
}

static void srDrawStar(GC9A01A_t3n* d, const SrStar& s, uint32_t nowMs) {
    float b = srBrightness(nowMs, s.period, s.phase);
    if (b < 0.04f) return;
    uint16_t c = s.color;
    uint8_t r5 = ((c >> 11) & 0x1F);
    uint8_t g6 = ((c >>  5) & 0x3F);
    uint8_t b5 = (c & 0x1F);
    r5 = (uint8_t)(min(r5 * b * 1.5f, 31.0f));
    g6 = (uint8_t)(min(g6 * b * 1.5f, 63.0f));
    b5 = (uint8_t)(min(b5 * b * 1.5f, 31.0f));
    if (r5 == 0 && g6 == 0 && b5 == 0) return;
    uint16_t col = ((uint16_t)r5 << 11) | ((uint16_t)g6 << 5) | b5;
    if (s.r <= 1) {
        d->drawPixel(s.x, s.y, col);
    } else if (s.r == 2) {
        d->drawPixel(s.x,   s.y,   col);
        d->drawPixel(s.x+1, s.y,   col);
        d->drawPixel(s.x,   s.y+1, col);
        d->drawPixel(s.x+1, s.y+1, col);
    } else {
        d->fillCircle(s.x, s.y, s.r, col);
        if (s.r >= 4 && b > 0.55f) {
            d->drawPixel(s.x - s.r - 1, s.y, col);
            d->drawPixel(s.x + s.r + 1, s.y, col);
            d->drawPixel(s.x, s.y - s.r - 1, col);
            d->drawPixel(s.x, s.y + s.r + 1, col);
        }
    }
}

static void srDrawMoon(GC9A01A_t3n* d, int mcx, int mcy) {
    uint32_t t = millis();
    int drift = (int)sleepCfg.moonDrift;
    int cx = mcx + (int)roundf((float)drift * sinf((float)t / 5200.0f * sleepCfg.speed));
    int cy = mcy + (int)roundf((float)drift * 0.6f * cosf((float)t / 6800.0f * sleepCfg.speed));
    int mr = (int)sleepCfg.moonR;
    // Glow rings
    d->drawCircle(cx, cy, mr + 5, SR_MOON_GLOW);
    d->drawCircle(cx, cy, mr + 3, SR_MOON_GLOW);
    d->drawCircle(cx, cy, mr + 1, SR_MOON_GLOW2);
    // Moon body
    d->fillCircle(cx, cy, mr, SR_MOON_BASE);
    // 4 craters
    d->fillCircle(cx - 4, cy - 6,  4, 0x7AEF);  // crater (shadow)
    d->fillCircle(cx + 5, cy + 4,  3, 0x738E);
    d->fillCircle(cx - 6, cy + 5,  2, 0x6ACD);
    d->fillCircle(cx + 2, cy - 9,  2, 0x6ACD);
    // Crescent shadow cutout: offset circle at +62% of moonR on x, radius 0.82*moonR
    d->fillCircle(cx + (int)(mr * 0.62f), cy - (int)(mr * 0.06f),
                  (int)(mr * 0.82f), SR_BLACK);
    // Outer glow halo
    d->drawCircle(cx, cy, mr + 10, SR_MOON_GLOW);
    d->drawCircle(cx, cy, mr +  7, SR_MOON_GLOW);
}

static void srDrawSaturn(GC9A01A_t3n* d, int baseCx, int baseCy, bool mirrorDrift) {
    uint32_t t = millis();
    int drift = (int)sleepCfg.saturnDrift;
    int sdx = (int)roundf(sinf((float)t / 9000.0f * sleepCfg.speed) * drift);
    int sdy = (int)roundf(cosf((float)t / 13000.0f * sleepCfg.speed) * drift * 0.5f);
    if (mirrorDrift) sdx = -sdx;
    int cx = baseCx + sdx;
    int cy = baseCy + sdy;

    // tilt = sin(view elevation). 0.62 ≈ 38° — ring clearly oval, 12px visible
    // above/below planet vs only 2px with old 0.42.
    const float tilt  = 0.62f;
    const int   r     = (int)sleepCfg.saturnR;
    const float rO    = r * 2.6f;    // outer ring (A ring outer edge)
    const float rCas  = r * 2.32f;   // Cassini division outer (A ring inner)
    const float rM    = r * 2.05f;   // B ring outer edge (Cassini inner)
    const int   ringH = (int)(rO * tilt) + 1;

    // Helper: planet half-width at SCREEN offset ry (circular planet on screen,
    // not the scaled ellipse coordinate — fixes inner ring cutout shape).
    auto planetHW = [&](int ry) -> float {
        return (ry < r) ? sqrtf((float)r*r - (float)ry*ry) : 0.0f;
    };

    // 1. Back rings — above planet center (drawn first; planet body covers overlap)
    for (int ry = 1; ry <= ringH; ry++) {
        float rySc = (float)ry / tilt;   // ellipse y-scale for ring projection
        float xO    = sqrtf(max(0.0f, rO*rO   - rySc*rySc));  // A ring outer
        float xCas  = sqrtf(max(0.0f, rCas*rCas - rySc*rySc)); // Cassini outer
        float xM    = sqrtf(max(0.0f, rM*rM   - rySc*rySc));  // B ring outer
        float xP    = planetHW(ry);                              // planet surface
        int ixO = (int)xO, ixCas = (int)xCas;
        int ixM = (int)xM, ixP  = (int)xP;
        int y = cy - ry;
        if (y < 0 || y >= SR_H || ixO <= 0) continue;
        // A ring (outer, brighter): Cassini outer → ring outer
        if (ixO > ixCas) {
            d->drawFastHLine(cx - ixO,   y, ixO - ixCas, SR_RING_FRONT);
            d->drawFastHLine(cx + ixCas, y, ixO - ixCas, SR_RING_FRONT);
        }
        // B ring (inner, dimmer): planet surface → B ring outer
        int ixBinner = ixP; if (ixBinner < 0) ixBinner = 0;
        if (ixM > ixBinner) {
            d->drawFastHLine(cx - ixM,     y, ixM - ixBinner, SR_RING_BACK);
            d->drawFastHLine(cx + ixBinner, y, ixM - ixBinner, SR_RING_BACK);
        }
    }

    // 2. Planet body
    d->fillCircle(cx, cy, r, SR_SAT_BODY);
    d->drawFastHLine(cx - r + 2, cy - r / 4, 2 * r - 4, SR_SAT_BAND);
    d->drawFastHLine(cx - r + 2, cy + r / 4, 2 * r - 4, SR_SAT_BAND);
    d->fillCircle(cx - r / 3, cy - r / 3, r / 3 + 1, SR_SAT_SPEC);

    // 3. Front rings — below planet center, drawn over planet body
    for (int ry = 0; ry <= ringH; ry++) {
        float rySc = (float)ry / tilt;
        float xO    = sqrtf(max(0.0f, rO*rO   - rySc*rySc));
        float xCas  = sqrtf(max(0.0f, rCas*rCas - rySc*rySc));
        float xM    = sqrtf(max(0.0f, rM*rM   - rySc*rySc));
        float xP    = planetHW(ry);
        int ixO = (int)xO, ixCas = (int)xCas;
        int ixM = (int)xM, ixP  = (int)xP;
        int y = cy + ry;
        if (y < 0 || y >= SR_H || ixO <= 0) continue;
        // A ring
        if (ixO > ixCas) {
            d->drawFastHLine(cx - ixO,   y, ixO - ixCas, SR_RING_FRONT);
            d->drawFastHLine(cx + ixCas, y, ixO - ixCas, SR_RING_FRONT);
        }
        // B ring (planet surface to B outer)
        int ixBinner = ixP; if (ixBinner < 0) ixBinner = 0;
        if (ixM > ixBinner) {
            d->drawFastHLine(cx - ixM,     y, ixM - ixBinner, SR_RING_BACK);
            d->drawFastHLine(cx + ixBinner, y, ixM - ixBinner, SR_RING_BACK);
        }
    }
}

static void srDrawPulseRings(GC9A01A_t3n* d, int frameNum) {
    for (int i = 0; i < 3; i++) {
        int ri = (int)srRingR[i];
        float fade = 1.0f - (srRingR[i] - SR_RING_MIN) / (SR_RING_MAX - SR_RING_MIN);
        if (fade < 0.18f && (frameNum & 1)) continue;
        uint16_t color = (fade > 0.5f) ? 0x0005 : 0x0002;
        d->drawCircle(SR_CX, SR_CY, ri, color);
    }
}

static void srAdvanceRings() {
    float step = SR_RING_SPEED * sleepCfg.speed;
    for (int i = 0; i < 3; i++) {
        srRingR[i] += step;
        if (srRingR[i] > SR_RING_MAX) srRingR[i] = SR_RING_MIN;
    }
}

// Warp particles: init with a simple LFSR-based deterministic RNG
static void srInitWarps(SrWarpPt* warps, uint32_t seed) {
    uint32_t s = seed;
    auto nextf = [&]() -> float {
        s = s * 1664525u + 1013904223u;
        return (float)(s >> 8) / 16777215.0f;
    };
    for (int i = 0; i < SR_WARP_MAX; i++) {
        warps[i].angle = nextf() * 6.2832f;
        warps[i].r     = nextf() * 12.0f + 1.0f;  // stagger initial radii
        warps[i].speed = (nextf() * 0.22f + 0.07f) * 0.8f;
        warps[i].maxR  = nextf() * 85.0f + 35.0f;
        float h = nextf();
        warps[i].hue   = (h < 0.25f) ? 1 : (h < 0.45f) ? 2 : 0;
    }
}

static void srTickWarps(SrWarpPt* warps) {
    int count = min((int)sleepCfg.warpCount, SR_WARP_MAX);
    float spd = sleepCfg.speed * ((float)sleepCfg.warpSpeed / 255.0f) * 1.8f + 0.12f;
    for (int i = 0; i < count; i++) {
        warps[i].r += warps[i].speed * spd;
        if (warps[i].r > warps[i].maxR) {
            warps[i].r     = (float)(random(8)) + 1.0f;
            warps[i].angle = ((float)random(628)) / 100.0f;
            warps[i].speed = ((float)random(22) + 7.0f) / 100.0f;
            warps[i].maxR  = ((float)random(56)) + 35.0f;
            int h = random(100);
            warps[i].hue   = (h < 25) ? 1 : (h < 45) ? 2 : 0;
        }
    }
}

static void srDrawWarps(GC9A01A_t3n* d, SrWarpPt* warps, int cx, int cy) {
    int count = min((int)sleepCfg.warpCount, SR_WARP_MAX);
    float bright = (float)sleepCfg.warpBright / 255.0f;
    for (int i = 0; i < count; i++) {
        const SrWarpPt& w = warps[i];
        float frac = w.r / w.maxR;
        float a    = frac * bright * (1.0f - frac * 0.3f);
        if (a < 0.08f) continue;
        int px = cx + (int)(cosf(w.angle) * w.r);
        int py = cy + (int)(sinf(w.angle) * w.r);
        if (px < 0 || px >= SR_W || py < 0 || py >= SR_H) continue;
        // Dim color by alpha (approximate)
        uint16_t col = (w.hue == 1) ? SR_WARP_BLUE : (w.hue == 2) ? SR_WARP_AMBER : SR_WARP_WHITE;
        uint8_t r5 = ((col >> 11) & 0x1F);
        uint8_t g6 = ((col >>  5) & 0x3F);
        uint8_t b5 = (col & 0x1F);
        r5 = (uint8_t)(r5 * a); g6 = (uint8_t)(g6 * a); b5 = (uint8_t)(b5 * a);
        col = ((uint16_t)r5 << 11) | ((uint16_t)g6 << 5) | b5;
        d->drawPixel(px, py, col);
        // Short tail 1px behind
        if (frac > 0.12f) {
            float tl = frac * 7.0f + 1.0f;
            int tx = cx + (int)(cosf(w.angle) * (w.r - tl));
            int ty = cy + (int)(sinf(w.angle) * (w.r - tl));
            if (tx >= 0 && tx < SR_W && ty >= 0 && ty < SR_H) {
                uint8_t dr5 = r5 >> 1, dg6 = g6 >> 1, db5 = b5 >> 1;
                d->drawPixel(tx, ty, ((uint16_t)dr5 << 11) | ((uint16_t)dg6 << 5) | db5);
            }
        }
    }
}

static void srDrawShootStar(GC9A01A_t3n* d, const int path[4], float progress) {
    float x0 = path[0], y0 = path[1];
    float x1 = path[2], y1 = path[3];
    float dx = x1 - x0, dy = y1 - y0;
    float len = sqrtf(dx*dx + dy*dy);
    if (len < 1.0f) return;
    float ux = dx/len, uy = dy/len;
    float headDist = progress * len;
    float segStart = headDist - (float)SR_SHOOT_TRAIL;
    if (segStart < 0.0f) segStart = 0.0f;
    for (float s = segStart; s <= headDist; s += 1.0f) {
        int px = (int)(x0 + ux * s);
        int py = (int)(y0 + uy * s);
        float intensity = (s - segStart) / (float)SR_SHOOT_TRAIL;
        uint16_t col;
        if      (intensity > 0.88f) col = 0xFFFF;
        else if (intensity > 0.70f) col = 0xD6FF;
        else if (intensity > 0.50f) col = 0xAD7F;
        else if (intensity > 0.28f) col = 0x647F;
        else                        col = 0x2316;
        if (px >= 0 && px < SR_W && py >= 0 && py < SR_H) {
            d->drawPixel(px, py, col);
            if (intensity > 0.80f) {
                if (px+1 < SR_W) d->drawPixel(px+1, py, 0xBDF7);
                if (py+1 < SR_H) d->drawPixel(px, py+1, 0xBDF7);
            }
        }
    }
}

static void srHandleShootingStars(GC9A01A_t3n* left, GC9A01A_t3n* right, uint32_t nowMs) {
    int count = min((int)sleepCfg.shootCount, SR_SHOOT_COUNT);
    for (int i = 0; i < count; i++) {
        if (nowMs >= srShootTimerL[i]) {
            uint32_t elapsed = nowMs - srShootTimerL[i];
            if (elapsed < SR_SHOOT_INTERVAL) {
                srDrawShootStar(left, SR_SHOOT_L[i],
                    (float)elapsed / (float)SR_SHOOT_INTERVAL);
            } else {
                srShootTimerL[i] = nowMs + SR_SHOOT_INTERVAL + (uint32_t)(i * 700);
            }
        }
        if (nowMs >= srShootTimerR[i]) {
            uint32_t elapsed = nowMs - srShootTimerR[i];
            if (elapsed < SR_SHOOT_INTERVAL) {
                srDrawShootStar(right, SR_SHOOT_R[i],
                    (float)elapsed / (float)SR_SHOOT_INTERVAL);
            } else {
                srShootTimerR[i] = nowMs + SR_SHOOT_INTERVAL + (uint32_t)(i * 700);
            }
        }
    }
}

// ---------------------------------------------------------------------------
// INIT
// ---------------------------------------------------------------------------
static uint32_t srFrameCount = 0;

static inline void sleepRendererInit() {
    srInitialized = true;
    srLastFrameMs = 0;
    srFrameCount  = 0;
    srRingR[0] = SR_RING_MIN;
    srRingR[1] = SR_RING_MIN + (SR_RING_MAX - SR_RING_MIN) / 3.0f;
    srRingR[2] = SR_RING_MIN + (SR_RING_MAX - SR_RING_MIN) * 2.0f / 3.0f;
    uint32_t now = millis();
    for (int i = 0; i < SR_SHOOT_COUNT; i++) {
        srShootTimerL[i] = now + 900  + (uint32_t)(i * 950);
        srShootTimerR[i] = now + 1400 + (uint32_t)(i * 950);
    }
    srInitWarps(srWarpsL, 0x1A2B3C4D);
    srInitWarps(srWarpsR, 0x5E6F7A8B);
}

// ---------------------------------------------------------------------------
// RENDER — called every loop() iteration while sleeping
// ---------------------------------------------------------------------------
static inline void renderSleepFrame(GC9A01A_t3n* left, GC9A01A_t3n* right) {
    if (!left || !right) return;
    uint32_t nowMs = millis();
    if (nowMs - srLastFrameMs < SR_FRAME_MS) return;
    srLastFrameMs = nowMs;
    srFrameCount++;
#ifdef DEBUG_SR
    // RD-031: default-off. This print fired every 10 sleep frames all night
    // (~52% of journal volume). Build with -DDEBUG_SR to re-enable.
    if ((srFrameCount % 10) == 0) {
        Serial.print("[SR] frame=");
        Serial.println(srFrameCount);
    }
#endif

    // ── Left eye ──────────────────────────────────────────────────────────
    left->fillScreen(SR_BLACK);
    // Nebula patches (4, soft dim clouds)
    left->fillCircle( 48, 168, 55, SR_NEBULA_PUR);  // bottom-left (purple)
    left->fillCircle( 19, 130, 40, SR_NEBULA_BLU);  // mid-left (blue wisp)
    left->fillCircle( 91, 192, 34, SR_NEBULA_PUR);  // overlap
    left->fillCircle(154,  43, 30, SR_NEBULA_MAG);  // top-center (magenta)
    // Pulse rings from center
    srDrawPulseRings(left, (int)srFrameCount);
    // Warp particles
    srTickWarps(srWarpsL);
    srDrawWarps(left, srWarpsL, SR_CX, SR_CY);
    // Stars
    for (int i = 0; i < SR_STAR_COUNT; i++) srDrawStar(left, SR_STARS_L[i], nowMs);
    // Objects: Saturn (bottom-left) and Moon (top-right)
    srDrawSaturn(left, SR_SAT_L_CX, SR_SAT_L_CY, false);
    srDrawMoon(left, SR_MOON_L_CX, SR_MOON_L_CY);

    // ── Right eye ─────────────────────────────────────────────────────────
    right->fillScreen(SR_BLACK);
    right->fillCircle(192, 168, 55, SR_NEBULA_PUR);  // bottom-right
    right->fillCircle(221, 130, 40, SR_NEBULA_BLU);  // mid-right
    right->fillCircle(149, 192, 34, SR_NEBULA_PUR);
    right->fillCircle( 86,  43, 30, SR_NEBULA_MAG);  // top-center (mirrored)
    srDrawPulseRings(right, (int)srFrameCount);
    srTickWarps(srWarpsR);
    srDrawWarps(right, srWarpsR, SR_CX, SR_CY);
    for (int i = 0; i < SR_STAR_COUNT; i++) srDrawStar(right, SR_STARS_R[i], nowMs);
    srDrawSaturn(right, SR_SAT_R_CX, SR_SAT_R_CY, true);
    srDrawMoon(right, SR_MOON_R_CX, SR_MOON_R_CY);

    // ── Shared: shooting stars across both eyes ────────────────────────────
    srHandleShootingStars(left, right, nowMs);

    srAdvanceRings();

    left->updateScreen();
    right->updateScreen();
}
