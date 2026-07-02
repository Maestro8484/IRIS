#pragma once

#include <Arduino.h>
#include <vector>
#include <memory>
#include <cmath>
#include "eyes.h"

/// Manages the overall behaviour (movement, blinking, pupil size) of one or more eyes.
template<std::size_t numEyes, typename Disp>
class EyeController {
private:
  std::array<Eye<Disp>, numEyes> eyes{};

  uint32_t eyeIndex{};

  /// Holds the current overall state of the eye(s)
  OverallState state{};

  /// Whether the eyes are moving autonomously
  bool autoMove{};

  /// Whether the eyes are blinking autonomously
  bool autoBlink{};

  /// Whether the pupils are resizing autonomously
  bool autoPupils{};

  /// When autoMove is enabled, this represents the maximum amount of time the eyes
  /// gaze at a particular place before moving again.
  uint32_t maxGazeMs{3000};

  /// The amount of fixation to apply
  int32_t nufix{7};

  // For autonomous iris scaling
  static constexpr size_t irisLevels{7};
  std::array<float, irisLevels> irisPrev{};
  std::array<float, irisLevels> irisNext{};

  // ── Eyelid override ────────────────────────────────────────────────────────
  // When lidOverrideActive is true, these values replace computeEyelids() output.
  // Values are set instantly -- no per-frame smoothing loop.
  // The existing renderEye() per-pixel damping (0.7 prev + 0.3 new) already
  // provides a natural-looking transition without the full-redraw cost.
  // 0.0 = fully closed, 1.0 = fully open.
  bool  lidOverrideActive{false};
  float lidUpper{1.0f};
  float lidLower{1.0f};

  /// If autoMove is enabled, periodically initiates motion to a new random point
  /// with a random speed, and holds there for random period until next motion.
  void applyAutoMove(Eye<Disp> &eye) {
    if (!autoMove && !state.inMotion) {
      eye.x = state.eyeOldX;
      eye.y = state.eyeOldY;
      return;
    }

    uint32_t t = millis();
    uint32_t dt = t - state.moveStartTimeMs;

    if (state.inMotion) {
      if (dt >= state.moveDurationMs) {
        state.inMotion = false;
        uint32_t limit = std::min(static_cast<uint32_t>(1000), maxGazeMs);
        state.moveDurationMs = random(35u, limit);
        if (!state.saccadeIntervalMs) {
          state.lastSaccadeStopMs = t;
          state.saccadeIntervalMs = random(state.moveDurationMs, maxGazeMs);
        }
        state.moveStartTimeMs = t;
        eye.x = state.eyeOldX = state.eyeNewX;
        eye.y = state.eyeOldY = state.eyeNewY;
      } else {
        float e = static_cast<float>(dt) / static_cast<float>(state.moveDurationMs);
        const float e2 = e * e;
        e = e2 * (3 - 2 * e);
        eye.x = state.eyeOldX + (state.eyeNewX - state.eyeOldX) * e;
        eye.y = state.eyeOldY + (state.eyeNewY - state.eyeOldY) * e;
      }
    } else {
      eye.x = state.eyeOldX;
      eye.y = state.eyeOldY;
      if (dt > state.moveDurationMs) {
        if ((t - state.lastSaccadeStopMs) > state.saccadeIntervalMs) {
          float r = (static_cast<float>(eye.definition->polar.mapRadius * 2) - static_cast<float>(screenWidth) * M_PI_2) * 0.75f;
          state.eyeNewX = random(-r, r);
          const float moveDist = sqrtf(r * r - state.eyeNewX * state.eyeNewX);
          state.eyeNewY = random(-moveDist, moveDist);
          state.moveDurationMs = random(83, 166);
          state.saccadeIntervalMs = 0;
        } else {
          float r = static_cast<float>(eye.definition->polar.mapRadius * 2) - static_cast<float>(screenWidth) * M_PI_2;
          r *= 0.07f;
          const float dx = random(-r, r);
          state.eyeNewX = eye.x - eye.definition->polar.mapRadius + dx;
          const float h = sqrtf(r * r - dx * dx);
          state.eyeNewY = eye.y - eye.definition->polar.mapRadius + random(-h, h);
          state.moveDurationMs = random(7, 25);
        }
        state.eyeNewX += eye.definition->polar.mapRadius;
        state.eyeNewY += eye.definition->polar.mapRadius;
        state.moveStartTimeMs = t;
        state.inMotion = true;
      }
    }
  }

  void applyAutoPupils() {
    if (!autoPupils && !state.resizing) {
      return;
    }

    if (state.resizing) {
      uint32_t t = millis();
      uint32_t dt = t - state.resizeStartTimeMs;
      if (dt < state.resizeDurationMs) {
        float fraction = static_cast<float>(dt) / static_cast<float>(state.resizeDurationMs);
        state.pupilAmount = state.resizeStart + fraction * (state.resizeTarget - state.resizeStart);
      } else {
        state.resizing = false;
        state.pupilAmount = state.resizeTarget;
      }
      return;
    }

    float n, sum{0.5f};
    for (uint32_t i = 0; i < irisLevels; i++) {
      uint32_t iexp = 1 << (i + 1);
      uint32_t imask = (iexp - 1);
      uint32_t ibits = state.irisFrame & imask;
      if (ibits) {
        const float weight = static_cast<float>(ibits) / static_cast<float>(iexp);
        n = irisPrev[i] * (1.0f - weight) + irisNext[i] * weight;
      } else {
        n = irisNext[i];
        irisPrev[i] = irisNext[i];
        irisNext[i] = -0.5f + (static_cast<float>(random(1000)) / 999.0f);
      }
      iexp = 1 << (irisLevels - i);
      sum += n / static_cast<float>(iexp);
    }
    setPupil(sum);

    if ((++state.irisFrame) >= (1 << irisLevels)) {
      state.irisFrame = 0;
    }
  }

  void wink(Eye<Disp> &eye, uint32_t duration) {
    if (eye.blink.state == BlinkState::NotBlinking) {
      eye.blink.state = BlinkState::BlinkClosing;
      eye.blink.startTimeMs = millis();
      eye.blink.durationMs = duration;
    }
  }

  uint32_t doBlink() {
    const uint32_t blinkDuration = random(50, 100);
    for (auto &e: eyes) {
      wink(e, blinkDuration);
    }
    return blinkDuration;
  }

  void updateDefinition(Eye<Disp> &eye, const EyeDefinition &def) {
    eye.definition = &def;
    eye.currentIrisAngle = def.iris.startAngle;
    eye.currentScleraAngle = def.sclera.startAngle;
    eye.drawAll = true;
  }

  void applyAutoBlink() {
    if (!autoBlink) {
      return;
    }
    const uint32_t t = millis();
    if (t - state.timeOfLastBlinkMs >= state.timeToNextBlinkMs) {
      state.timeOfLastBlinkMs = t;
      const uint32_t blinkDuration = doBlink();
      state.timeToNextBlinkMs = blinkDuration * 3 + random(4000);
    }
  }

  float updateBlinkState(Eye<Disp> &eye) {
    auto &blink = eye.blink;
    const uint32_t t = millis();
    float blinkFactor{};
    if (blink.state != BlinkState::NotBlinking) {
      if (t - blink.startTimeMs >= blink.durationMs) {
        switch (blink.state) {
          case BlinkState::BlinkClosing:
            blink.state = BlinkState::BlinkOpening;
            blink.durationMs *= 2;
            blink.startTimeMs = t;
            blinkFactor = 1.0f;
            break;
          case BlinkState::BlinkOpening:
            blink.state = BlinkState::NotBlinking;
            blinkFactor = 0.0f;
            break;
          default:
            break;
        }
      } else {
        blinkFactor = (float)(t - blink.startTimeMs) / (float)blink.durationMs;
        if (blink.state == BlinkState::BlinkOpening) {
          blinkFactor = 1.0f - blinkFactor;
        }
      }
    }
    return blinkFactor;
  }

  float mapToScreen(int32_t value, int32_t mapRadius, int32_t eyeRadius) const {
    return sinf(static_cast<float>(value) / static_cast<float>(mapRadius)) * static_cast<float>(M_PI_2) * static_cast<float>(eyeRadius);
  }

  void constrainEyeCoord(float &x, float &y) {
    auto d2 = x * x + y * y;
    if (d2 > 1.0f) {
      auto d = sqrtf(d2);
      x /= d;
      y /= d;
    }
  }

  std::pair<float, float> computeEyelids(Eye<Disp> &eye) const {
    float upperQ, lowerQ;
    if (eye.definition->tracking) {
      uint32_t mapRadius = eye.definition->polar.mapRadius;
      int32_t ix = static_cast<int32_t>(mapToScreen(mapRadius - eye.x, mapRadius, eye.definition->radius)) + screenWidth / 2;
      int32_t iy = static_cast<int32_t>(mapToScreen(mapRadius - eye.y, mapRadius, eye.definition->radius)) + screenWidth / 2;
      iy -= eye.definition->iris.radius * eye.definition->squint;
      if (eyeIndex & 1) {
        ix = screenWidth - 1 - ix;
      }
      int32_t upperOpen = eye.definition->eyelids.upperOpen(ix);
      if (iy <= upperOpen) {
        upperQ = 1.0f;
      } else {
        int32_t upperClosed = eye.definition->eyelids.upperClosed(ix);
        if (iy >= upperClosed) {
          upperQ = 0.0f;
        } else {
          upperQ = static_cast<float>(upperClosed - iy) / static_cast<float>(upperClosed - upperOpen);
        }
      }
      lowerQ = 1.0f - upperQ;
    } else {
      upperQ = 1.0f;
      lowerQ = 1.0f;
    }
    return std::pair<float, float>{upperQ, lowerQ};
  }

  void applyFixation(Eye<Disp> &eye) {
    if (eyes.size() == 1) {
      return;
    }
    state.fixate = ((state.fixate * 15) + nufix) / 16;
    eye.x = (eyeIndex & 1) ? eye.x - static_cast<float>(state.fixate) : eye.x + static_cast<float>(state.fixate);
  }

  void applySpin(Eye<Disp> &eye) {
    const float minutes = static_cast<float>(millis()) / 60000.0f;
    if (eye.definition->iris.iSpin) {
      eye.currentIrisAngle += eye.definition->iris.iSpin;
    } else {
      eye.currentIrisAngle = lroundf(
          static_cast<float>(eye.definition->iris.startAngle) + eye.definition->iris.spin * minutes * -1024.0f);
    }
    if (eye.definition->sclera.iSpin) {
      eye.currentScleraAngle += eye.definition->sclera.iSpin;
    } else {
      eye.currentScleraAngle = lroundf(
          static_cast<float>(eye.definition->sclera.startAngle) + eye.definition->sclera.spin * minutes * -1024.0f);
    }
  }

  void renderEye(Eye<Disp> &eye, float upperFactor, float lowerFactor, float blinkFactor) {

    const int32_t displacementMapSize = screenWidth / 2;
    const int32_t mapRadius = eye.definition->polar.mapRadius;
    const int32_t mapDiameter = mapRadius * 2;

    Disp &display = *eye.display;
    EyeBlink &blink = eye.blink;

    const int32_t xPositionOverMap = eye.x - screenWidth / 2;
    const int32_t yPositionOverMap = eye.y - screenHeight / 2;

    const ScleraParams &sclera = eye.definition->sclera;
    const IrisParams &iris = eye.definition->iris;
    bool hasScleraTexture = sclera.hasTexture();
    bool hasIrisTexture = iris.hasTexture();

    const float pupilRange = eye.definition->pupil.max - eye.definition->pupil.min;
    const float irisValue = 1.0f - (eye.definition->pupil.min + pupilRange * state.pupilAmount);
    const int32_t irisTextureHeight = hasIrisTexture ? iris.texture.height : 1;
    int32_t iPupilFactor = static_cast<int32_t>(32768.0f / 126.0f * (irisTextureHeight - 1) / irisValue);
    const int32_t irisSize = static_cast<int32_t>(126.0f * irisValue) + 128;

    // Dampen the eyelid movement -- this provides the transition smoothing
    upperFactor = eye.upperLidFactor * 0.7f + upperFactor * 0.3f;
    lowerFactor = eye.lowerLidFactor * 0.7f + lowerFactor * 0.3f;

    const float upperF = upperFactor * (1.0f - blinkFactor);
    const float lowerF = lowerFactor * (1.0f - blinkFactor);
    const float prevUpperF = eye.upperLidFactor * (1.0f - blink.blinkFactor);
    const float prevLowerF = eye.lowerLidFactor * (1.0f - blink.blinkFactor);

    eye.upperLidFactor = upperFactor;
    eye.lowerLidFactor = lowerFactor;
    blink.blinkFactor = blinkFactor;

    const uint8_t *angleLookup = eye.definition->polar.angle;
    const uint8_t *distanceLookup = eye.definition->polar.distance;
    const uint16_t pupilColor = eye.definition->pupil.color;
    const uint16_t backColor = eye.definition->backColor;
    const EyelidParams &eyelids = eye.definition->eyelids;
    const uint16_t eyelidColor = eyelids.color;
    const uint8_t *displacement = eye.definition->displacement;

    for (uint32_t screenX = 0; screenX < screenWidth; screenX++) {
      auto currentUpper = eyelids.upperLid(screenX, upperF);
      auto currentLower = eyelids.lowerLid(screenX, lowerF);

      uint32_t minY, maxY;
      if (eye.drawAll) {
        minY = 0;
        maxY = screenHeight;
      } else {
        auto previousUpper = eyelids.upperLid(screenX, prevUpperF);
        minY = std::min(currentUpper, previousUpper);
        auto previousLower = eyelids.lowerLid(screenX, prevLowerF);
        maxY = std::max(currentLower, previousLower);
      }

      const uint8_t *displaceX, *displaceY;
      int32_t xmul;
      int32_t doff;
      if (screenX < (screenWidth / 2)) {
        displaceX = &displacement[displacementMapSize - 1 - screenX];
        displaceY = &displacement[(displacementMapSize - 1 - screenX) * displacementMapSize];
        xmul = -1;
      } else {
        displaceX = &displacement[screenX - displacementMapSize];
        displaceY = &displacement[(screenX - displacementMapSize) * displacementMapSize];
        xmul = 1;
      }

      if (currentUpper > minY) {
        display.drawFastVLine(screenX, minY, currentUpper - minY, eyelidColor);
        minY = currentUpper;
      }
      if (currentLower < maxY) {
        display.drawFastVLine(screenX, currentLower, maxY - currentLower, eyelidColor);
        maxY = currentLower;
      }

      const int32_t xx = xPositionOverMap + screenX;
      for (uint32_t screenY = minY; screenY < maxY; screenY++) {
        uint32_t p;

        const int32_t yy = yPositionOverMap + screenY;
        int32_t dx, dy;
        if (screenY < displacementMapSize) {
          doff = displacementMapSize - screenY - 1;
          dy = -displaceY[doff];
        } else {
          doff = screenY - displacementMapSize;
          dy = displaceY[doff];
        }
        dx = displaceX[doff * displacementMapSize];
        if (dx < 255) {
          dx *= xmul;
          int32_t mx = xx + dx;
          int32_t my = yy + dy;

          if (mx >= 0 && mx < mapDiameter && my >= 0 && my < mapDiameter) {
            uint32_t angle;
            int32_t distance, moff;
            if (my >= mapRadius) {
              my -= mapRadius;
              if (mx >= mapRadius) {
                mx -= mapRadius;
                moff = my * mapRadius + mx;
                angle = angleLookup[moff];
                distance = distanceLookup[moff];
              } else {
                mx = mapRadius - mx - 1;
                angle = angleLookup[mx * mapRadius + my] + 768;
                distance = distanceLookup[my * mapRadius + mx];
              }
            } else {
              if (mx < mapRadius) {
                mx = mapRadius - mx - 1;
                my = mapRadius - my - 1;
                moff = my * mapRadius + mx;
                angle = angleLookup[moff] + 512;
                distance = distanceLookup[moff];
              } else {
                mx -= mapRadius;
                my = mapRadius - my - 1;
                angle = angleLookup[mx * mapRadius + my] + 256;
                distance = distanceLookup[my * mapRadius + mx];
              }
            }

            if (distance < 128) {
              if (hasScleraTexture) {
                angle = ((angle + eye.currentScleraAngle) & 1023) ^ sclera.mirror;
                const int32_t tx = (angle & 1023) * sclera.texture.width / 1024;
                const int32_t ty = distance * sclera.texture.height / 128;
                p = sclera.texture.get(tx, ty);
              } else {
                p = sclera.color;
              }
            } else if (distance < 255) {
              if (distance >= irisSize) {
                p = pupilColor;
              } else {
                if (hasIrisTexture) {
                  angle = ((angle + eye.currentIrisAngle) & 1023) ^ iris.mirror;
                  const int32_t tx = (angle & 1023) * iris.texture.width / 1024;
                  const int32_t ty = (distance - 128) * iPupilFactor / 32768;
                  p = iris.texture.get(tx, ty);
                } else {
                  p = iris.color;
                }
              }
            } else {
              p = backColor;
            }
          } else {
            p = backColor;
          }
        } else {
          p = eyelidColor;
        }
        display.drawPixel(screenX, screenY, p);
      }
    }

    eye.drawAll = false;
  }

  Eye<Disp> &currentEye() {
    return eyes[eyeIndex];
  }

public:
  EyeController(std::array<DisplayDefinition<Disp>, numEyes> displayDefs, bool autoMove, bool autoBlink,
                bool autoPupils) :
      autoMove(autoMove), autoBlink(autoBlink), autoPupils(autoPupils) {
    size_t i{};
    for (const auto &dispDef: displayDefs) {
      Eye<Disp> &eye = eyes[i++];
      eye.display = dispDef.display;
      updateDefinition(eye, dispDef.definition);
      eye.blink.state = BlinkState::NotBlinking;
      eye.x = eye.y = state.eyeOldX = state.eyeNewX = state.eyeOldY = state.eyeNewY = eye.definition->polar.mapRadius;
      eye.drawAll = true;
    }
  }

  void setAutoMove(bool enabled) { autoMove = enabled; }
  bool autoMoveEnabled() { return autoMove; }
  void setAutoBlink(bool enabled) { autoBlink = enabled; }
  bool autoBlinkEnabled() { return autoBlink; }
  void setAutoPupils(bool enabled) { autoPupils = enabled; }
  bool autoPupilsEnabled() { return autoPupils; }
  void setMaxGazeMs(uint32_t maxGazeMillis) { maxGazeMs = maxGazeMillis; }

  void blink() {
    doBlink();
    state.timeToNextBlinkMs = 0;
  }

  void wink(size_t index) {
    if (index < eyes.size()) {
      wink(eyes[index], random(50, 100));
    }
  }

  /// Override eyelid openness for emotional expression.
  /// Values are applied instantly; renderEye()'s built-in damping (0.7/0.3)
  /// provides a natural-looking transition over ~10 frames at no extra cost.
  /// \param upper  0.0 = fully closed, 1.0 = fully open
  /// \param lower  0.0 = fully closed, 1.0 = fully open
  void setLidOpenness(float upper, float lower) {
    lidUpper = constrain(upper, 0.0f, 1.0f);
    lidLower = constrain(lower, 0.0f, 1.0f);
    lidOverrideActive = true;
  }

  /// Restore normal eyelid behavior (fully open / tracking).
  void clearLidOverride() {
    lidOverrideActive = false;
    lidUpper = 1.0f;
    lidLower = 1.0f;
    // Force each eye to snap its stored factors back to open
    for (auto &e : eyes) {
      e.upperLidFactor = 1.0f;
      e.lowerLidFactor = 1.0f;
    }
  }

  void setTargetPosition(float xTarget, float yTarget, int32_t durationMs = 120) {
    constrainEyeCoord(xTarget, yTarget);
    Eye<Disp> &eye = currentEye();
    auto middle = static_cast<float>(eye.definition->polar.mapRadius);
    auto r = (middle * 2.0f - static_cast<float>(screenWidth) * static_cast<float>(M_PI_2)) * 0.75f;
    // If a motion is already in progress, advance eyeOld to the current
    // interpolated position so the restart doesn't snap back to the origin.
    if (state.inMotion) {
      uint32_t dt = millis() - state.moveStartTimeMs;
      if (dt < state.moveDurationMs) {
        float e = static_cast<float>(dt) / static_cast<float>(state.moveDurationMs);
        e = e * e * (3.0f - 2.0f * e); // smoothstep
        state.eyeOldX = state.eyeOldX + (state.eyeNewX - state.eyeOldX) * e;
        state.eyeOldY = state.eyeOldY + (state.eyeNewY - state.eyeOldY) * e;
      } else {
        state.eyeOldX = state.eyeNewX;
        state.eyeOldY = state.eyeNewY;
      }
    }
    state.eyeNewX = middle - xTarget * r;
    state.eyeNewY = middle - yTarget * r;
    state.inMotion = true;
    state.moveDurationMs = durationMs;
    state.moveStartTimeMs = millis();
  }

  void setPosition(float x, float y) {
    constrainEyeCoord(x, y);
    Eye<Disp> &eye = currentEye();
    auto middle = static_cast<float>(eye.definition->polar.mapRadius);
    auto r = (middle * 2.0f - static_cast<float>(screenWidth) * static_cast<float>(M_PI_2)) * 0.75f;
    state.eyeOldX = middle - x * r;
    state.eyeOldY = middle - y * r;
  }

  void setTargetPupil(float ratio, int32_t durationMs = 100) {
    state.resizing = true;
    state.resizeStart = state.pupilAmount;
    state.resizeTarget = ratio;
    state.resizeDurationMs = durationMs;
    state.resizeStartTimeMs = millis();
  }

  void setPupil(float ratio) {
    state.pupilAmount = std::max(0.0f, std::min(1.0f, ratio));
  }

  void updateDefinitions(const std::array<EyeDefinition, numEyes> &definitions) {
    size_t i = 0;
    for (const EyeDefinition &def: definitions) {
      updateDefinition(eyes[i++], def);
    }
  }

  bool renderFrame() {
    auto &eye = currentEye();

    if (!eye.display->isAvailable()) {
      return false;
    }

    applyAutoMove(eye);
    applyAutoBlink();
    applyAutoPupils();

    auto blinkFactor = updateBlinkState(eye);
    applyFixation(eye);
    applySpin(eye);

    float upperQ, lowerQ;
    if (lidOverrideActive) {
      upperQ = lidUpper;
      lowerQ = lidLower;
    } else {
      const std::pair<float, float> &pair = computeEyelids(eye);
      upperQ = pair.first;
      lowerQ = pair.second;
    }

    if (eyeIndex == 0) eye.x = eye.definition->polar.mapRadius * 2 - eye.x;
    renderEye(eye, upperQ, lowerQ, blinkFactor);
    if (eyeIndex == 0) eye.x = eye.definition->polar.mapRadius * 2 - eye.x;

    eye.display->update();

    eyeIndex = (eyeIndex + 1) % eyes.size();

    return true;
  }
};
