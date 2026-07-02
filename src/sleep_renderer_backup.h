// sleep_renderer.h — Deep space starfield displayed on both TFTs when eyesSleeping.
// renderSleepFrame() is called from main loop() instead of the eye engine.
// Target ~15fps via millis() throttle.
// Uses raw GC9A01A_t3n driver (exposed via GC9A01A_Display::getDriver()).
// No eye engine calls. Custom draw only.
#pragma once
#include <Arduino.h>
#include <math.h>
#include <GC9A01A_t3n.h>

// ---------------------------------------------------------------------------
// COLORS (RGB565)
// ---------------------------------------------------------------------------
static constexpr uint16_t SR_BLACK      = 0x0000;
static constexpr uint16_t SR_WARM_WHITE = 0xE71F;
static constexpr uint16_t SR_BLUE_WHITE = 0xAD7F;
static constexpr uint16_t SR_ICE_BLUE   = 0x9CDF;
static constexpr uint16_t SR_MOON       = 0xCE59; // pale blue-white
static constexpr uint16_t SR_NEBULA_PUR = 0x2001;
static constexpr uint16_t SR_NEBULA_BLU = 0x0002;
static constexpr uint16_t SR_NEBULA_TEL = 0x0201;
static constexpr uint16_t SR_RING       = 0x0003;
static constexpr uint16_t SR_ZZZ        = 0x0003;

// ---------------------------------------------------------------------------
// DISPLAY SIZE
// ---------------------------------------------------------------------------
static constexpr int SR_W = 240;
static constexpr int SR_H = 240;
static constexpr int SR_CX = 120;
static constexpr int SR_CY = 120;

// ---------------------------------------------------------------------------
// STARS — 25 per display, 3 layers
// Layer 0: 6 big bright stars  (r=2, period 2.2–3.4s)
// Layer 1: 9 medium stars      (r=1, period 3.4–4.5s)
// Layer 2: 10 small dim stars  (r=1, period 4.6–6.0s)
// Left/right eyes use different x,y — not mirrored.
// ---------------------------------------------------------------------------
struct SrStar {
    uint8_t  x, y;
    uint8_t  r;        // draw radius (pixels)
    uint16_t period;   // twinkle period ms
    uint16_t phase;    // phase offset ms
    uint16_t color;    // RGB565
};

static constexpr SrStar SR_STARS_L[25] = {
    // Layer 0 — 6 stars
    { 32,  18, 3, 2200,    0, SR_WARM_WHITE },
    { 195, 40, 3, 2800,  600, SR_BLUE_WHITE },
    { 70, 200, 3, 3000, 1200, SR_WARM_WHITE },
    {210, 165, 3, 3400, 1800, SR_ICE_BLUE   },
    { 50,  90, 3, 2600,  400, SR_BLUE_WHITE },
    {170,  25, 3, 2900, 1000, SR_ICE_BLUE   },
    // Layer 1 — 9 stars
    {100,  55, 1, 3400,  200, SR_WARM_WHITE },
    {148, 190, 1, 3800,  900, SR_BLUE_WHITE },
    { 20, 145, 1, 4000,  700, SR_ICE_BLUE   },
    {220,  90, 1, 4200, 1500, SR_WARM_WHITE },
    { 60, 230, 1, 3600,  350, SR_BLUE_WHITE },
    {185, 210, 1, 4500, 1100, SR_ICE_BLUE   },
    { 90, 170, 1, 3900,  800, SR_WARM_WHITE },
    {230,  50, 1, 4100, 1300, SR_BLUE_WHITE },
    {130,  80, 1, 3700,  500, SR_ICE_BLUE   },
    // Layer 2 — 10 stars
    { 15,  60, 1, 4600,  150, SR_ICE_BLUE   },
    {200, 130, 1, 5200,  750, SR_BLUE_WHITE },
    { 45, 180, 1, 5800, 1400, SR_ICE_BLUE   },
    {165,  75, 1, 4800,  950, SR_WARM_WHITE },
    {235, 200, 1, 6000,  300, SR_ICE_BLUE   },
    { 80,  30, 1, 5000, 1600, SR_BLUE_WHITE },
    {120, 220, 1, 5400,  550, SR_ICE_BLUE   },
    {225, 160, 1, 4700,  100, SR_WARM_WHITE },
    { 30, 115, 1, 5600,  850, SR_ICE_BLUE   },
    {155,  35, 1, 4900, 1250, SR_BLUE_WHITE },
};

static constexpr SrStar SR_STARS_R[25] = {
    // Layer 0 — 6 stars
    { 45,  30, 3, 2300,  300, SR_WARM_WHITE },
    {180,  55, 3, 2700,  900, SR_BLUE_WHITE },
    { 85, 210, 3, 3100,  600, SR_ICE_BLUE   },
    {220, 140, 3, 3300, 1500, SR_WARM_WHITE },
    { 25,  85, 3, 2500,  200, SR_BLUE_WHITE },
    {160,  18, 3, 2950, 1100, SR_ICE_BLUE   },
    // Layer 1 — 9 stars
    {110,  65, 1, 3500,  400, SR_WARM_WHITE },
    {165, 185, 1, 3700,  800, SR_BLUE_WHITE },
    { 35, 155, 1, 4100,  650, SR_ICE_BLUE   },
    {215,  80, 1, 4300, 1600, SR_WARM_WHITE },
    { 70, 225, 1, 3650,  250, SR_BLUE_WHITE },
    {190, 205, 1, 4450, 1200, SR_ICE_BLUE   },
    {100, 165, 1, 3850,  700, SR_WARM_WHITE },
    {235,  45, 1, 4050, 1350, SR_BLUE_WHITE },
    {140,  90, 1, 3750,  500, SR_ICE_BLUE   },
    // Layer 2 — 10 stars
    { 20,  50, 1, 4650,  100, SR_ICE_BLUE   },
    {205, 120, 1, 5100,  800, SR_BLUE_WHITE },
    { 55, 175, 1, 5750, 1450, SR_ICE_BLUE   },
    {175,  70, 1, 4850, 1000, SR_WARM_WHITE },
    {230, 195, 1, 5950,  350, SR_ICE_BLUE   },
    { 90,  25, 1, 5050, 1550, SR_BLUE_WHITE },
    {125, 215, 1, 5350,  600, SR_ICE_BLUE   },
    {220, 150, 1, 4750,  150, SR_WARM_WHITE },
    { 40, 105, 1, 5550,  900, SR_ICE_BLUE   },
    {150,  40, 1, 4950, 1300, SR_BLUE_WHITE },
};

// ---------------------------------------------------------------------------
// STATE
// ---------------------------------------------------------------------------
static bool     srInitialized   = false;
static uint32_t srLastFrameMs   = 0;
static constexpr uint32_t SR_FRAME_MS = 150; // ~6.7fps — reduced to keep loop() responsive

// Moon drift
static constexpr int SR_MOON_CX   = 120;
static constexpr int SR_MOON_CY_L = 85;  // left eye moon center Y
static constexpr int SR_MOON_CY_R = 88;  // right eye moon center Y
static constexpr int SR_MOON_R    = 18;
static constexpr int SR_MOON_BITE_R = 15;
static constexpr int SR_MOON_BITE_DX = 9;
static constexpr int SR_MOON_BITE_DY = -3;

// Pulse rings — 3 expanding rings
static constexpr float SR_RING_SPEED  = 0.5f;   // px per frame
static constexpr float SR_RING_MIN    = 30.0f;
static constexpr float SR_RING_MAX    = 64.0f;
static float           srRingR[3]     = {30.0f, 41.0f, 51.0f}; // staggered start

// Shooting stars — 2 per display, staggered 1800ms
static uint32_t srShootTimerL[2] = {0, 1800};  // fire time (absolute ms, 0=fire immediately)
static uint32_t srShootTimerR[2] = {0, 1800};
static constexpr uint32_t SR_SHOOT_INTERVAL = 4500;

// Left eye shooting star paths: start→end
static constexpr int SR_SHOOT_L[2][4] = {
    { 40, 55,  90, 35 },   // star 0
    {130,165, 170,148 },   // star 1
};
static constexpr int SR_SHOOT_R[2][4] = {
    {160, 52, 110, 32 },
    { 68,168,  30,150 },
};

// ZZZ — 3 characters, staggered 1270ms apart, 3800ms cycle
static constexpr uint32_t SR_ZZZ_CYCLE = 3800;
static constexpr uint32_t SR_ZZZ_GAP   = 1270;
static const uint8_t      SR_ZZZ_SIZE[3] = {2, 1, 1}; // GFX scale

// Moon position for ZZZ origin
static constexpr int SR_ZZZ_ORIGIN_X = SR_MOON_CX + SR_MOON_R + 4;
static constexpr int SR_ZZZ_ORIGIN_Y = SR_MOON_CY_L - SR_MOON_R - 10;

// ---------------------------------------------------------------------------
// HELPERS
// ---------------------------------------------------------------------------

static inline float srBrightness(uint32_t nowMs, uint16_t period, uint16_t phase) {
    float t = (float)((nowMs + phase) % period) / (float)period;
    float s = 0.5f + 0.5f * sinf(t * 6.2832f);
    return s * s;  // square for sharper on/off pulse
}

static void srDrawStar(GC9A01A_t3n* d, const SrStar& s, uint32_t nowMs) {
    float b = srBrightness(nowMs, s.period, s.phase);
    if (b < 0.05f) return; // star below threshold: off
    // Dim color by extracting components and scaling
    uint16_t c = s.color;
    uint8_t r5 = ((c >> 11) & 0x1F);
    uint8_t g6 = ((c >> 5)  & 0x3F);
    uint8_t b5 = (c & 0x1F);
    r5 = (uint8_t)(min(r5 * b * 1.4f, 31.0f));
    g6 = (uint8_t)(min(g6 * b * 1.4f, 63.0f));
    b5 = (uint8_t)(min(b5 * b * 1.4f, 31.0f));
    if (r5 == 0 && g6 == 0 && b5 == 0) return;
    uint16_t col = ((uint16_t)r5 << 11) | ((uint16_t)g6 << 5) | b5;
    if (s.r <= 1) {
        d->drawPixel(s.x, s.y, col);
    } else {
        d->fillCircle(s.x, s.y, s.r, col);
    }
}

static void srDrawMoon(GC9A01A_t3n* d, int baseCY) {
    uint32_t t = millis();
    int cy = baseCY + (int)roundf(2.0f * sinf((float)t / 5000.0f));
    // White filled circle
    d->fillCircle(SR_MOON_CX, cy, SR_MOON_R, SR_MOON);
    // Carve crescent with black circle
    d->fillCircle(SR_MOON_CX + SR_MOON_BITE_DX,
                  cy + SR_MOON_BITE_DY,
                  SR_MOON_BITE_R, SR_BLACK);
}

static void srDrawRings(GC9A01A_t3n* d, int frameNum) {
    for (int i = 0; i < 3; i++) {
        int ri = (int)srRingR[i];
        // Fade: dim as ring expands. Skip alternate frames for far rings.
        float fade = 1.0f - (srRingR[i] - SR_RING_MIN) / (SR_RING_MAX - SR_RING_MIN);
        if (fade < 0.2f && (frameNum & 1)) continue;  // skip alternate frames when dim
        uint16_t color = (fade > 0.5f) ? SR_RING : 0x0001;
        d->drawCircle(SR_CX, SR_CY, ri, color);
    }
}

static void srAdvanceRings() {
    for (int i = 0; i < 3; i++) {
        srRingR[i] += SR_RING_SPEED;
        if (srRingR[i] > SR_RING_MAX) {
            srRingR[i] = SR_RING_MIN;
        }
    }
}

// Draw a shooting star segment. progress: 0.0→1.0 across the path.
static void srDrawShootStar(GC9A01A_t3n* d, const int path[4], float progress) {
    float x0 = path[0], y0 = path[1];
    float x1 = path[2], y1 = path[3];
    float dx = x1 - x0, dy = y1 - y0;
    float len = sqrtf(dx*dx + dy*dy);
    float ux = dx/len, uy = dy/len;

    // Bright segment 25px long centered at progress point
    float headDist = progress * len;
    float segStart = headDist - 25.0f;
    if (segStart < 0) segStart = 0;

    // Step along segment
    for (float s = segStart; s <= headDist; s += 1.0f) {
        int px = (int)(x0 + ux * s);
        int py = (int)(y0 + uy * s);
        float intensity = (s - segStart) / 25.0f; // brighter at head
        uint16_t col = (intensity > 0.6f) ? 0xFFFF :
                       (intensity > 0.3f) ? 0xBDF7 : 0x7BCF;
        if (px >= 0 && px < SR_W && py >= 0 && py < SR_H) {
            d->drawPixel(px, py, col);
        }
    }
}

static void srHandleShootingStars(GC9A01A_t3n* left, GC9A01A_t3n* right, uint32_t nowMs) {
    // Update timers and draw active shooting stars
    for (int i = 0; i < 2; i++) {
        // Left
        if (nowMs >= srShootTimerL[i]) {
            uint32_t elapsed = nowMs - srShootTimerL[i];
            if (elapsed < SR_SHOOT_INTERVAL) {
                float progress = (float)elapsed / (float)SR_SHOOT_INTERVAL;
                srDrawShootStar(left, SR_SHOOT_L[i], progress);
            } else {
                srShootTimerL[i] = nowMs + SR_SHOOT_INTERVAL + (i * 1800);
            }
        }
        // Right
        if (nowMs >= srShootTimerR[i]) {
            uint32_t elapsed = nowMs - srShootTimerR[i];
            if (elapsed < SR_SHOOT_INTERVAL) {
                float progress = (float)elapsed / (float)SR_SHOOT_INTERVAL;
                srDrawShootStar(right, SR_SHOOT_R[i], progress);
            } else {
                srShootTimerR[i] = nowMs + SR_SHOOT_INTERVAL + (i * 1800);
            }
        }
    }
}

// Draw one ZZZ character. cyclePhase: 0.0→1.0 within SR_ZZZ_CYCLE.
// Returns whether anything was drawn.
static bool srDrawOneZzz(GC9A01A_t3n* d, int startX, int startY,
                          uint8_t sz, float cyclePhase, bool driftRight) {
    if (cyclePhase < 0.0f || cyclePhase >= 1.0f) return false;
    // Fade: 0→peak at 0.1→0 at 1.0
    float fade;
    if (cyclePhase < 0.1f)      fade = cyclePhase / 0.1f;
    else if (cyclePhase < 0.9f) fade = 1.0f;
    else                         fade = (1.0f - cyclePhase) / 0.1f;
    if (fade < 0.05f) return false;

    // Drift: +/- 14px horizontal, -30px vertical over full cycle
    float driftX = cyclePhase * (driftRight ? 14.0f : -14.0f);
    float driftY = cyclePhase * -30.0f;
    int cx = startX + (int)driftX;
    int cy = startY + (int)driftY;

    uint16_t col = (fade > 0.5f) ? SR_ZZZ : 0x0001;
    if (cx >= 0 && cx < SR_W - sz*6 && cy >= 0 && cy < SR_H - sz*8) {
        d->drawChar(cx, cy, 'Z', col, SR_BLACK, sz, sz);
    }
    return true;
}

static void srDrawZzz(GC9A01A_t3n* left, GC9A01A_t3n* right, uint32_t nowMs) {
    uint32_t t = nowMs % (SR_ZZZ_CYCLE + SR_ZZZ_GAP * 3);
    for (int i = 0; i < 3; i++) {
        uint32_t zStart = i * SR_ZZZ_GAP;
        int32_t  zElapsed = (int32_t)t - (int32_t)zStart;
        float    phase = (float)zElapsed / (float)SR_ZZZ_CYCLE;
        int originY = SR_ZZZ_ORIGIN_Y - i * (SR_ZZZ_SIZE[i] * 9);
        srDrawOneZzz(left,  SR_ZZZ_ORIGIN_X, originY, SR_ZZZ_SIZE[i], phase, true);
        srDrawOneZzz(right, SR_W - SR_ZZZ_ORIGIN_X - SR_ZZZ_SIZE[i]*6,
                            originY, SR_ZZZ_SIZE[i], phase, false);
    }
}

// ---------------------------------------------------------------------------
// INIT — called once on sleep entry (after blankDisplays)
// ---------------------------------------------------------------------------
static inline void sleepRendererInit() {
    srInitialized = true;
    srLastFrameMs = 0;
    // Reset ring radii
    srRingR[0] = 30.0f;
    srRingR[1] = 41.0f;
    srRingR[2] = 51.0f;
    // Shooting star timers fire after brief delay
    uint32_t now = millis();
    srShootTimerL[0] = now + 1000;
    srShootTimerL[1] = now + 1000 + 1800;
    srShootTimerR[0] = now + 1500;
    srShootTimerR[1] = now + 1500 + 1800;
}

// ---------------------------------------------------------------------------
// RENDER — called every loop() iteration while sleeping
// ---------------------------------------------------------------------------
static uint32_t srFrameCount = 0;

static inline void renderSleepFrame(GC9A01A_t3n* left, GC9A01A_t3n* right) {
    if (!left || !right) return;

    uint32_t nowMs = millis();
    if (nowMs - srLastFrameMs < SR_FRAME_MS) return;
    srLastFrameMs = nowMs;
    srFrameCount++;
    // Heartbeat every 10 frames (~1.5s) so serial log confirms rendering is live.
    if ((srFrameCount % 10) == 0) {
      Serial.print("[SR] frame=");
      Serial.println(srFrameCount);
    }

    // ── Left eye ──────────────────────────────────────────────────
    left->fillScreen(SR_BLACK);
    // Nebula: dim red-purple bottom-left + dim blue bottom-right
    left->fillCircle(55, 185, 55, SR_NEBULA_PUR);
    left->fillCircle(185, 190, 45, SR_NEBULA_BLU);
    // Stars, moon, rings
    for (int i = 0; i < 25; i++) srDrawStar(left, SR_STARS_L[i], nowMs);
    srDrawMoon(left, SR_MOON_CY_L);
    srDrawRings(left, (int)srFrameCount);

    // ── Right eye ─────────────────────────────────────────────────
    right->fillScreen(SR_BLACK);
    // Nebula: dim purple top-right + dim teal bottom-left
    right->fillCircle(180, 55, 50, SR_NEBULA_PUR);
    right->fillCircle(55, 195, 48, SR_NEBULA_TEL);
    // Stars, moon, rings
    for (int i = 0; i < 25; i++) srDrawStar(right, SR_STARS_R[i], nowMs);
    srDrawMoon(right, SR_MOON_CY_R);
    srDrawRings(right, (int)srFrameCount);

    // ── Elements that draw on both displays ───────────────────────
    srHandleShootingStars(left, right, nowMs);
    srDrawZzz(left, right, nowMs);

    // ── Advance ring state ────────────────────────────────────────
    srAdvanceRings();

    // ── Push both framebuffers to hardware ───────────────────────
    left->updateScreen();
    right->updateScreen();
}
