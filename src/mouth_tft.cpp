// mouth_tft.cpp — ILI9341 2.8" TFT mouth driver (Arduino_GFX SWSPI, Teensy 4.1)
// Bit-bang SPI on pins 35/37/36 — no hardware SPI, no DMA, no collision with GC9A01A_t3n.
// Pins: MOSI=35, SCK=37, CS=36, DC=8, RST=4, BL=5

#include "mouth_tft.h"
#include "sleep_cfg.h"
#include <Arduino_GFX_Library.h>

static constexpr uint8_t MOUTH_TFT_CS   = 36;
static constexpr uint8_t MOUTH_TFT_DC   =  8;
static constexpr uint8_t MOUTH_TFT_RST  =  4;
static constexpr uint8_t MOUTH_TFT_MOSI = 35;
static constexpr uint8_t MOUTH_TFT_SCK  = 37;
static constexpr uint8_t MOUTH_TFT_BL   =  5;  // GPIO5 — PWM dimming, wired to ILI9341 BL

// ── Colors (RGB565) matching assistant.py emotion palette ────────────────────
static constexpr uint16_t MTFT_BLACK   = 0x0000;
static constexpr uint16_t MTFT_CYAN    = 0x07FF;  // NEUTRAL, CURIOUS
static constexpr uint16_t MTFT_YELLOW  = 0xFFE0;  // HAPPY
static constexpr uint16_t MTFT_RED     = 0xF800;  // ANGRY
static constexpr uint16_t MTFT_PURPLE  = 0x780F;  // SLEEPY
static constexpr uint16_t MTFT_WHITE   = 0xFFFF;  // SURPRISED
static constexpr uint16_t MTFT_BLUE    = 0x001F;  // SAD
static constexpr uint16_t MTFT_MAGENTA = 0xF81F;  // CONFUSED
static constexpr uint16_t MTFT_BLUSH   = 0xFBCC;  // HAPPY cheek blush (light pink)
static constexpr uint16_t MTFT_AMBER   = 0xFD20;  // AMUSED — reserved, needs dedicated expression index
static constexpr uint16_t MTFT_TONGUE  = 0xFB36;  // hot pink — SILLY tongue

// ── Backlight lookup table (logarithmic, level 0-15 → PWM 0-255) ─────────────
// intensity 0=off, 1=barely visible, 8=comfortable idle, 15=full
static const uint8_t BL_MAP[16] = {
    0, 2, 4, 7, 11, 16, 22, 30, 40, 55, 75, 100, 135, 175, 210, 255
};

// ── Backlight + expression state (tracked for idle engine) ───────────────────
static uint8_t  _currentBLLevel  = 14; // matches init (BL_MAP[14]=210)
static uint8_t  _currentMouthIdx = 0;
static uint32_t _sillyShownMs    = 0;  // millis() when idx 9 shown; arms TONGUE_WAG

// ── Emotion-tinted idle (RD-030 #2) ──────────────────────────────────────────
// The last clearly-coloured mood IRIS expressed, captured in mouthTFTShow().
// The idle resting face is redrawn in this hue, decaying back to cyan over
// IDLE_TINT_DECAY_MS, so IRIS idles "warm" after a happy chat, "cool" after a sad one.
static uint16_t _lastEmotionColor = 0x07FF;       // start neutral cyan
static uint32_t _lastEmotionMs    = 0;            // 0 = never → no tint
static constexpr uint32_t IDLE_TINT_DECAY_MS = 240000UL; // 4 min to fade to cyan

static Arduino_DataBus *_bus = nullptr;
static Arduino_GFX     *_tft = nullptr;

// ── Draw primitives ───────────────────────────────────────────────────────────

// Thick arc: center (cx,cy), radius r, angles a0..a1 radians.
// Screen-coord angles: 0=right, PI/2=down, PI=left, 3PI/2=up.
// Stroke via fillRect at each parametric step (~1px apart).
static void _arc(int16_t cx, int16_t cy, int16_t r,
                 float a0, float a1, uint16_t color, int16_t t) {
    float step = (r > 0) ? (1.0f / r) : 0.02f;
    for (float a = a0; a <= a1; a += step) {
        int16_t x = cx + (int16_t)(r * cosf(a));
        int16_t y = cy + (int16_t)(r * sinf(a));
        _tft->fillRect(x - t / 2, y - t / 2, t, t, color);
    }
}

// Thick quadratic bezier from P0→control→P2
static void _bezier(int16_t x0, int16_t y0, int16_t xc, int16_t yc,
                    int16_t x2, int16_t y2, uint16_t color, int16_t t) {
    for (float f = 0.0f; f <= 1.0f; f += 0.004f) {
        float m = 1.0f - f;
        int16_t x = (int16_t)(m * m * x0 + 2.0f * m * f * xc + f * f * x2);
        int16_t y = (int16_t)(m * m * y0 + 2.0f * m * f * yc + f * f * y2);
        _tft->fillRect(x - t / 2, y - t / 2, t, t, color);
    }
}

// Thick sine wave with optional linear downward drift
static void _sine(int16_t x0, int16_t x1, int16_t cy,
                  float amp, float freq, float drift_per_px,
                  uint16_t color, int16_t t) {
    for (int16_t x = x0; x <= x1; x++) {
        float dx  = (float)(x - x0);
        int16_t y = (int16_t)(cy + dx * drift_per_px + amp * sinf(freq * dx));
        _tft->fillRect(x - t / 2, y - t / 2, t, t, color);
    }
}

// Draw a single 'Z' character as three line segments (for SLEEPY ZZZ)
// top-left corner (tx, ty), width w, height h
static void _draw_Z(int16_t tx, int16_t ty, int16_t w, int16_t h, uint16_t color) {
    int16_t t = 4;  // stroke width
    // top horizontal
    _tft->fillRect(tx, ty, w, t, color);
    // diagonal (approximate with a bezier/line scan)
    int16_t steps = w + h;
    for (int16_t i = 0; i <= steps; i++) {
        int16_t x = tx + w - (int16_t)((float)i / steps * w);
        int16_t y = ty + (int16_t)((float)i / steps * h);
        _tft->fillRect(x - t / 2, y - t / 2, t, t, color);
    }
    // bottom horizontal
    _tft->fillRect(tx, ty + h - t, w, t, color);
}

// ── Expression draw functions ─────────────────────────────────────────────────

// 0: NEUTRAL — very shallow upward curve (near-flat), cyan
static void _draw_neutral() {
    _arc(160, -600, 730, 1.35f, 1.79f, MTFT_CYAN, 10);
}

// 1: HAPPY — large smile arc + cheek blush circles, yellow/pink
static void _draw_happy() {
    _arc(160, 20, 130, 0.50f, 2.64f, MTFT_YELLOW, 12);
    _tft->fillCircle(60,  155, 22, MTFT_BLUSH);
    _tft->fillCircle(260, 155, 22, MTFT_BLUSH);
}

// 2: CURIOUS — asymmetric smirk (flat left, rises right), cyan
static void _draw_curious() {
    _bezier(40, 135, 160, 145, 285, 95, MTFT_CYAN, 10);
}

// 3: ANGRY — downward frown arc, red
static void _draw_angry() {
    _arc(160, 220, 120, 3.77f, 5.65f, MTFT_RED, 12);
}

// 4: SLEEPY — drooping sine wave with downward drift + ZZZ glyphs, purple
static void _draw_sleepy() {
    _sine(30, 290, 105, 18.0f, 0.030f, 0.10f, MTFT_PURPLE, 10);
    _draw_Z(200, 20, 24, 20, MTFT_PURPLE);  // large Z
    _draw_Z(228, 30, 18, 15, MTFT_PURPLE);  // medium Z
    _draw_Z(250, 38, 14, 11, MTFT_PURPLE);  // small Z
}

// 5: SURPRISED — thick hollow oval, white
static void _draw_surprised() {
    const int16_t cx = 160, cy = 120, rx = 58, ry = 48, t = 12;
    for (float a = 0.0f; a < 6.2832f; a += 0.020f) {
        int16_t x = cx + (int16_t)(rx * cosf(a));
        int16_t y = cy + (int16_t)(ry * sinf(a));
        _tft->fillRect(x - t / 2, y - t / 2, t, t, MTFT_WHITE);
    }
}

// 6: SAD — deep frown arc + vertical quiver lines, blue
static void _draw_sad() {
    _arc(160, 200, 145, 3.87f, 5.55f, MTFT_BLUE, 12);
    for (int8_t i = 0; i < 5; i++) {
        int16_t x = 120 + i * 20;
        _tft->drawFastVLine(x,     148, 18, MTFT_BLUE);
        _tft->drawFastVLine(x + 2, 151, 13, MTFT_BLUE);
    }
}

// 7: CONFUSED — squiggly multi-period sine wave (~2.7 cycles), magenta
static void _draw_confused() {
    _sine(30, 290, 120, 28.0f, 0.065f, 0.0f, MTFT_MAGENTA, 10);
}

// 8: SLEEP/OFF — black screen only (cleared by mouthTFTShow before switch)
static void _draw_sleep() {}

// TONGUE_WAG helper: same layout as _draw_silly() but tongue body raised 15px
static void _draw_silly_retracted() {
    _arc(160, -10, 170, 0.52f, 2.62f, MTFT_YELLOW, 12);  // upper lip
    _tft->fillRect(50, 100, 220, 75, 0x3000);               // dark interior
    _tft->fillRect(105, 163, 110, 53, MTFT_TONGUE);          // tongue body (raised 15px)
    _tft->fillCircle(160, 216, 42, MTFT_TONGUE);             // tongue tip
    _arc(160, 115, 55, 0.45f, 2.69f, MTFT_YELLOW, 12);     // lower lip over tongue
    _tft->fillRect(157, 167, 6, 47, 0xC000);                 // groove
}

// 9: SILLY — open mouth + protruding flat tongue, yellow/hot-pink
// Draw order: upper lip → interior → tongue → lower lip on top → groove
static void _draw_silly() {
    _arc(160, -10, 170, 0.52f, 2.62f, MTFT_YELLOW, 12);  // upper lip
    _tft->fillRect(50, 100, 220, 75, 0x3000);               // dark interior
    _tft->fillRect(105, 148, 110, 68, MTFT_TONGUE);          // tongue body
    _tft->fillCircle(160, 216, 42, MTFT_TONGUE);             // tongue tip
    _arc(160, 115, 55, 0.45f, 2.69f, MTFT_YELLOW, 12);     // lower lip over tongue
    _tft->fillRect(157, 152, 6, 62, 0xC000);                 // groove
}

// ── Public API ────────────────────────────────────────────────────────────────

void mouthTFTInit() {
    _bus = new Arduino_SWSPI(MOUTH_TFT_DC, MOUTH_TFT_CS,
                              MOUTH_TFT_SCK, MOUTH_TFT_MOSI, -1);
    _tft = new Arduino_ILI9341(_bus, MOUTH_TFT_RST, 3);
    _tft->begin();
    _tft->fillScreen(0x0000);
    pinMode(MOUTH_TFT_BL, OUTPUT);
    analogWrite(MOUTH_TFT_BL, BL_MAP[14]); // level 14 = 210/255
}

void mouthTFTShow(uint8_t idx) {
    if (!_tft) return;
    if (idx > 9) idx = 0;
    _currentMouthIdx = idx;
    if (idx == 9) {
        _sillyShownMs = millis();
        mouthIdleStart();   // arm TONGUE_WAG — idle was stopped by MOUTH: handler; restart it
    }
    _tft->fillScreen(MTFT_BLACK);
    switch (idx) {
        case 0: _draw_neutral();   break;
        case 1: _draw_happy();     break;
        case 2: _draw_curious();   break;
        case 3: _draw_angry();     break;
        case 4: _draw_sleepy();    break;
        case 5: _draw_surprised(); break;
        case 6: _draw_sad();       break;
        case 7: _draw_confused();  break;
        case 8: _draw_sleep();     break;
        case 9: _draw_silly();     break;
    }
    // RD-030 #2: capture the last clearly-coloured mood for emotion-tinted idle.
    // Neutral/curious (cyan), surprised (white), sleep are intentionally NOT captured
    // so they don't wipe the prevailing mood hue at end-of-turn (Pi4 sends MOUTH:0).
    switch (idx) {
        case 1: case 9: _lastEmotionColor = MTFT_YELLOW;  _lastEmotionMs = millis(); break;
        case 3:         _lastEmotionColor = MTFT_RED;     _lastEmotionMs = millis(); break;
        case 4:         _lastEmotionColor = MTFT_PURPLE;  _lastEmotionMs = millis(); break;
        case 6:         _lastEmotionColor = MTFT_BLUE;    _lastEmotionMs = millis(); break;
        case 7:         _lastEmotionColor = MTFT_MAGENTA; _lastEmotionMs = millis(); break;
        default: break;
    }
}

// Intensity / backlight control — BL on pin 5, PWM via BL_MAP lookup
void mouthSetSleepIntensity() {
    if (!_tft) return;
    _tft->fillScreen(MTFT_BLACK);
    analogWrite(MOUTH_TFT_BL, BL_MAP[5]); // level 5 = 16/255 — dim but visible during sleep
}

void mouthRestoreIntensity() {
    if (!_tft) return;
    analogWrite(MOUTH_TFT_BL, BL_MAP[_currentBLLevel]);
}

void mouthSetIntensity(uint8_t level) {
    if (!_tft) return;
    if (level > 15) level = 15;
    _currentBLLevel = level;
    analogWrite(MOUTH_TFT_BL, BL_MAP[level]);
}

// ── Emotion-tinted idle resting face (RD-030 #2) ─────────────────────────────
// Linear blend of two RGB565 colours; t=0 → a, t=255 → b.
static uint16_t _blend565(uint16_t a, uint16_t b, uint8_t t) {
    int ar = (a >> 11) & 0x1F, ag = (a >> 5) & 0x3F, ab = a & 0x1F;
    int br = (b >> 11) & 0x1F, bg = (b >> 5) & 0x3F, bb = b & 0x1F;
    int rr = ar + ((br - ar) * t) / 255;
    int rg = ag + ((bg - ag) * t) / 255;
    int rb = ab + ((bb - ab) * t) / 255;
    return (uint16_t)((rr << 11) | (rg << 5) | rb);
}

// Redraw the neutral resting curve tinted toward the last mood, fading to cyan over
// IDLE_TINT_DECAY_MS. One arc — as cheap as a normal MOUTH: render — so it is safe to
// call on idle entry and between idle animations. Sets the resting expression to NEUTRAL.
void mouthApplyIdleTint() {
    if (!_tft) return;
    uint8_t t;  // 0 = full mood tint, 255 = full cyan
    if (_lastEmotionMs == 0) {
        t = 255;
    } else {
        uint32_t since = millis() - _lastEmotionMs;
        t = (since >= IDLE_TINT_DECAY_MS) ? 255
                                          : (uint8_t)((uint32_t)since * 255UL / IDLE_TINT_DECAY_MS);
    }
    uint16_t col = _blend565(_lastEmotionColor, MTFT_CYAN, t);
    _tft->fillScreen(MTFT_BLACK);
    _arc(160, -600, 730, 1.35f, 1.79f, col, 10); // same geometry as _draw_neutral()
    _currentMouthIdx = 0;
}

// Sleep animation state — file-scope so mouthSleepReset() can zero it
static uint32_t _sleepFrameCount = 0;
// ZZZ: previous integer Y per pair (-999 = not yet drawn, forces first draw)
static int16_t _prevZY[3] = {-999, -999, -999};
// Wave: previous stroke center Y per wave per column (-999 = not yet drawn)
// 3 waves × 320 columns × 2 bytes = 1920 bytes — fine for Teensy 4.1 512KB RAM
static int16_t _prevSY[3][320];
static bool    _waveInit = false;  // false → full clear on next sleep entry

void mouthSleepReset() {
    _sleepFrameCount = 0;
    _prevZY[0] = _prevZY[1] = _prevZY[2] = -999;
    _waveInit = false;  // trigger band clear + cache init on next frame
}

void mouthSleepFrame() {
    if (!_tft) return;

    float phase = (float)_sleepFrameCount * 0.018f;

    // ── ZZZ region: erase+redraw each Z only when its integer Y position changes.
    // Eliminates the full-band black flash from the old clear-all-then-draw approach.
    struct ZPair { int16_t xR, xL, baseY, w, h; float da, ph; };
    static const ZPair ZP[3] = {
        { 246, 38, 44, 34, 28, 4.0f, 0.00f },  // large — drift amp reduced (was 11)
        { 222, 58, 35, 26, 22, 3.0f, 0.70f },  // medium — (was 8.5)
        { 202, 78, 27, 20, 17, 2.0f, 1.40f },  // small  — (was 6)
    };

    auto zCol = [](uint8_t alpha) -> uint16_t {
        if (alpha > 170) return 0x07FF;  // bright cyan
        if (alpha > 120) return 0x0466;  // mid cyan
        return 0x0244;                    // dim cyan
    };
    static const uint8_t* alphaPtr[3] = {
        &sleepCfg.zzzAlpha0, &sleepCfg.zzzAlpha1, &sleepCfg.zzzAlpha2
    };

    for (int i = 0; i < 3; i++) {
        const ZPair& z = ZP[i];
        int16_t drift = (int16_t)(z.da * sinf(phase * 0.38f + z.ph));
        int16_t ty = z.baseY + drift;
        if (ty < 5) ty = 5;
        if (ty + z.h >= 71) ty = 71 - z.h;

        if (ty != _prevZY[i]) {
            // Erase previous Z bounding box (small targeted clear, not full band)
            if (_prevZY[i] != -999) {
                _tft->fillRect(z.xR, _prevZY[i], z.w, z.h, MTFT_BLACK);
                _tft->fillRect(z.xL, _prevZY[i], z.w, z.h, MTFT_BLACK);
            }
            uint16_t col = zCol(*alphaPtr[i]);
            _draw_Z(z.xR, ty, z.w, z.h, col);
            _draw_Z(z.xL, ty, z.w, z.h, col);
            _prevZY[i] = ty;
        }
    }

    // ── Waveform band: incremental update — no full-band clear, no flash ─────
    // Each column is only redrawn when its wave position changes by ≥1 pixel.
    // Eliminates the "black flash" from clearing 320×87 every frame.
    static constexpr int16_t BAND_Y0 = 76;
    static constexpr int16_t BAND_Y1 = 162;
    static constexpr int16_t BH      = BAND_Y1 - BAND_Y0 + 1;
    static constexpr int16_t S0 = 6;
    static constexpr int16_t S1 = 4;
    static constexpr int16_t S2 = 3;
    static constexpr uint16_t W_BLUE   = 0x001F;
    static constexpr uint16_t W_PURPLE = 0x780F;
    static constexpr uint16_t W_TEAL   = 0x0318;

    float oscAmp = (float)sleepCfg.waveOscAmp;
    if (oscAmp > 14.0f) oscAmp = 14.0f;
    float oscY = sinf(phase * 0.22f) * oscAmp;

    int16_t cy0 = 116 + (int16_t)oscY;
    int16_t cy1 = cy0 - 22;
    int16_t cy2 = cy0 + 20;

    // Dynamic amplitude cap: clamp to fit within band regardless of config values.
    // Accounts for current wave center position (which shifts with oscY).
    float amp0 = (float)sleepCfg.waveAmp0;
    float amp1 = (float)sleepCfg.waveAmp1;
    float amp2 = (float)sleepCfg.waveAmp2;
    {
        int16_t c = cy0 - S0 - 2 - BAND_Y0; int16_t d = BAND_Y1 - cy0 - S0 - 2;
        int16_t cap = (c < d) ? c : d; if (cap < 4) cap = 4;
        if (amp0 > (float)cap) amp0 = (float)cap;
    }
    {
        int16_t c = cy1 - S1 - 2 - BAND_Y0; int16_t d = BAND_Y1 - cy1 - S1 - 2;
        int16_t cap = (c < d) ? c : d; if (cap < 4) cap = 4;
        if (amp1 > (float)cap) amp1 = (float)cap;
    }
    {
        int16_t c = cy2 - S2 - 2 - BAND_Y0; int16_t d = BAND_Y1 - cy2 - S2 - 2;
        int16_t cap = (c < d) ? c : d; if (cap < 4) cap = 4;
        if (amp2 > (float)cap) amp2 = (float)cap;
    }

    // First frame after sleep entry: clear band once and initialise position cache
    if (!_waveInit) {
        _tft->fillRect(0, BAND_Y0, 320, BH, MTFT_BLACK);
        for (int x = 0; x < 320; x++) {
            _prevSY[0][x] = -999;
            _prevSY[1][x] = -999;
            _prevSY[2][x] = -999;
        }
        _waveInit = true;
    }

    float ph0 = phase * 0.85f;
    float ph1 = phase * 1.22f;
    float ph2 = phase * 1.70f;

    for (int16_t x = 0; x < 320; x++) {
        float dx = (float)x;
        int16_t sy0 = cy0 + (int16_t)(amp0 * sinf(0.028f * dx + ph0));
        int16_t sy1 = cy1 + (int16_t)(amp1 * sinf(0.031f * dx + ph1 + 0.9f));
        int16_t sy2 = cy2 + (int16_t)(amp2 * sinf(0.052f * dx + ph2 + 1.8f));

        // Helper macro: erase old stroke and draw new one for a single wave
        #define _WAVE_UPDATE(wi, sy, prv, S, col) \
            if ((sy) != (prv)) { \
                if ((prv) != -999) { \
                    int16_t oa = (prv)-(S); if(oa<BAND_Y0) oa=BAND_Y0; \
                    int16_t ob = (prv)+(S); if(ob>BAND_Y1) ob=BAND_Y1; \
                    if(ob>=oa) _tft->fillRect(x, oa, 1, ob-oa+1, MTFT_BLACK); \
                } \
                int16_t na = (sy)-(S); if(na<BAND_Y0) na=BAND_Y0; \
                int16_t nb = (sy)+(S); if(nb>BAND_Y1) nb=BAND_Y1; \
                if(nb>=na) _tft->fillRect(x, na, 1, nb-na+1, col); \
                (prv) = (sy); \
            }

        _WAVE_UPDATE(0, sy0, _prevSY[0][x], S0, W_BLUE)
        _WAVE_UPDATE(1, sy1, _prevSY[1][x], S1, W_PURPLE)
        _WAVE_UPDATE(2, sy2, _prevSY[2][x], S2, W_TEAL)

        #undef _WAVE_UPDATE
    }

    _sleepFrameCount++;
}

// ── Idle animation engine ─────────────────────────────────────────────────────
//
// Anim IDs:
//   0 = BREATHE    — BL sine pulse, 45s block
//   1 = DRIFT      — subtle BL pulse, 20s block
//   2 = TWITCH     — smirk (idx 2) for 400ms
//   3 = BLINK      — BL off for 150ms
//   4 = YAWN       — surprised oval (idx 5) + BL bump for 600ms
//   5 = SIDESMIRK  — smirk (idx 2) + BL bump for 800ms
//   6 = TONGUE_WAG — silly (idx 9): 6×300ms half-cycles (3 wags); auto-triggered 2s after idx 9
//   7 = BOING      — surprised oval (idx 5) → neutral bounce, 600ms total
//
// Interruption: mouthIdleStop() restores saved mouth/BL immediately.
// Does not use delay() — all timing via millis() in mouthIdleTick().

static bool     _idleActive    = false;
static uint8_t  _idleAnim      = 0xFF; // 0xFF = none running
static uint8_t  _idlePhase     = 0;
static uint32_t _idlePhaseMs   = 0;
static uint32_t _idleNextMs    = 0;
static uint8_t  _idleSavedMouth = 0;

static void _idleRestore(uint32_t nowMs) {
    bool restoreMouth = (_idleAnim == 2 || _idleAnim == 4 || _idleAnim == 5 ||
                         _idleAnim == 6 || _idleAnim == 7);
    _idleAnim  = 0xFF;
    _idlePhase = 0;
    analogWrite(MOUTH_TFT_BL, BL_MAP[_currentBLLevel]);
    if (restoreMouth && _tft) {
        // RD-030 #2: return to the emotion-tinted resting face, not flat cyan, when the
        // saved expression was NEUTRAL (the usual end-of-turn state).
        if (_idleSavedMouth == 0) mouthApplyIdleTint();
        else                      mouthTFTShow(_idleSavedMouth);
    }
    // Gap between animations: 20-60s
    _idleNextMs = nowMs + 20000UL + (uint32_t)random(40000);
}

void mouthIdleTick(uint32_t nowMs) {
    if (!_idleActive || !_tft) return;

    // Pick next animation when slot is empty
    if (_idleAnim == 0xFF) {
        // Auto-trigger TONGUE_WAG 2s after SILLY face is shown (bypasses normal timer)
        if (_currentMouthIdx == 9 && _sillyShownMs != 0 &&
            (nowMs - _sillyShownMs) >= 2000UL) {
            _idleAnim       = 6;
            _idlePhase      = 0;
            _idlePhaseMs    = nowMs;
            _idleSavedMouth = 9;
            _sillyShownMs   = 0;
            return;
        }
        if (nowMs < _idleNextMs) return;
        // Weighted pick: blink/twitch common, yawn/boing rare
        uint8_t r = (uint8_t)random(11);
        if      (r < 2)  _idleAnim = 0; // BREATHE
        else if (r < 4)  _idleAnim = 3; // BLINK
        else if (r < 6)  _idleAnim = 2; // TWITCH
        else if (r < 8)  _idleAnim = 5; // SIDESMIRK
        else if (r < 9)  _idleAnim = 1; // DRIFT
        else if (r < 10) _idleAnim = 4; // YAWN (rare)
        else             _idleAnim = 7; // BOING (rare)
        _idlePhase    = 0;
        _idlePhaseMs  = nowMs;
        _idleSavedMouth = _currentMouthIdx;
        // Start expression-changing anims immediately
        switch (_idleAnim) {
            case 2: // TWITCH
                mouthTFTShow(2);
                break;
            case 4: // YAWN
                mouthTFTShow(5);
                analogWrite(MOUTH_TFT_BL,
                    BL_MAP[(uint8_t)constrain((int)_currentBLLevel + 3, 0, 15)]);
                break;
            case 5: // SIDESMIRK
                mouthTFTShow(2);
                analogWrite(MOUTH_TFT_BL,
                    BL_MAP[(uint8_t)constrain((int)_currentBLLevel + 2, 0, 15)]);
                break;
            case 7: // BOING
                mouthTFTShow(5);
                analogWrite(MOUTH_TFT_BL,
                    BL_MAP[(uint8_t)constrain((int)_currentBLLevel + 3, 0, 15)]);
                break;
            default: break;
        }
        return;
    }

    uint32_t el = nowMs - _idlePhaseMs;

    switch (_idleAnim) {
        case 0: { // BREATHE — 4s sine, 45s total
            float frac = (sinf(2.0f * M_PI * (float)el / 4000.0f) + 1.0f) * 0.5f;
            uint8_t lo = BL_MAP[2];
            uint8_t hi = BL_MAP[_currentBLLevel];
            analogWrite(MOUTH_TFT_BL, lo + (uint8_t)(frac * (hi - lo)));
            if (el >= 45000UL) _idleRestore(nowMs);
            break;
        }
        case 1: { // DRIFT — gentler pulse, 6s sine, 20s total
            float frac = (sinf(2.0f * M_PI * (float)el / 6000.0f) + 1.0f) * 0.5f;
            uint8_t lo = BL_MAP[(uint8_t)constrain((int)_currentBLLevel - 2, 0, 15)];
            uint8_t hi = BL_MAP[_currentBLLevel];
            analogWrite(MOUTH_TFT_BL, lo + (uint8_t)(frac * (hi - lo)));
            if (el >= 20000UL) _idleRestore(nowMs);
            break;
        }
        case 2: // TWITCH — smirk held 400ms
            if (el >= 400) _idleRestore(nowMs);
            break;
        case 3: // BLINK — BL off 150ms, no fillScreen
            if (_idlePhase == 0) {
                analogWrite(MOUTH_TFT_BL, 0);
                _idlePhase = 1;
            }
            if (el >= 150) _idleRestore(nowMs);
            break;
        case 4: // YAWN — surprised oval held 600ms
            if (el >= 600) _idleRestore(nowMs);
            break;
        case 5: // SIDESMIRK — smirk + BL bump held 800ms
            if (el >= 800) _idleRestore(nowMs);
            break;
        case 6: { // TONGUE_WAG — 300ms per half-cycle, 6 half-cycles (3 full wags)
            uint8_t halfCycle = (uint8_t)(el / 300UL);
            if (halfCycle != _idlePhase) {
                _idlePhase = halfCycle;
                if (halfCycle >= 6) {
                    _idleRestore(nowMs);
                } else {
                    _tft->fillScreen(MTFT_BLACK);
                    if (halfCycle & 1) _draw_silly_retracted();
                    else               _draw_silly();
                }
            }
            break;
        }
        case 7: { // BOING — surprised oval → neutral bounce, 600ms total
            if (_idlePhase == 0 && el >= 300) {
                mouthTFTShow(0);
                analogWrite(MOUTH_TFT_BL, BL_MAP[_currentBLLevel]);
                _idlePhase = 1;
            }
            if (_idlePhase >= 1 && el >= 600) _idleRestore(nowMs);
            break;
        }
    }
}

void mouthIdleStart() {
    if (_idleActive) return;
    _idleActive  = true;
    _idleAnim    = 0xFF;
    _idleNextMs  = millis() + 5000UL; // first anim fires 5s after start
}

void mouthIdleStop() {
    if (!_idleActive) return;
    _idleActive = false;
    if (_idleAnim != 0xFF) {
        bool restoreMouth = (_idleAnim == 2 || _idleAnim == 4 || _idleAnim == 5 ||
                             _idleAnim == 6 || _idleAnim == 7);
        _idleAnim  = 0xFF;
        _idlePhase = 0;
        analogWrite(MOUTH_TFT_BL, BL_MAP[_currentBLLevel]);
        if (restoreMouth && _tft) mouthTFTShow(_idleSavedMouth);
    }
}

bool mouthIdleIsActive() { return _idleActive; }

// ── "Noticed you" greet on face-acquire (RD-030 #3) ──────────────────────────
// One-shot recognition pop (surprised oval → settle) when a person enters frame.
// Fires only while the idle engine owns the mouth (post-inactivity) and no idle
// animation is mid-flight, so it never interrupts a conversation or another anim.
// Reuses the tested BOING (anim 7) timing/restore path; the FACE:1 caller is already
// debounced by FACE_COOLDOWN_MS in main.cpp.
void mouthGreet() {
    if (!_idleActive || !_tft) return;  // only when at-rest idle owns the mouth
    if (_idleAnim != 0xFF) return;      // don't interrupt a running idle animation
    _idleSavedMouth = _currentMouthIdx;
    _idleAnim    = 7;                    // BOING: surprised pop → neutral bounce
    _idlePhase   = 0;
    _idlePhaseMs = millis();
    mouthTFTShow(5);                     // surprised oval
    analogWrite(MOUTH_TFT_BL,
        BL_MAP[(uint8_t)constrain((int)_currentBLLevel + 3, 0, 15)]);
}
