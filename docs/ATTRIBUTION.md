# Third-Party Attribution

This file lists every third-party project, model, or dataset IRIS depends on,
built from, or derives code from, along with the license terms of each as
found at the cited source. Nothing here is from memory -- every entry cites
a fetched URL. See `LICENSE` for how these terms interact with IRIS's own
license.

---

## Firmware / eye rendering (`src/eyes/`)

### TeensyEyes
- **Author:** Chris Miller
- **License:** MIT License
- **URL:** https://github.com/chrismiller/TeensyEyes
- **Used for:** `src/eyes/eyes.h`, `src/eyes/EyeController.h`, and the
  `src/eyes/240x240/` display + eye-shape headers are IRIS's adapted copy of
  TeensyEyes's rendering engine (per-eye polar-distortion tables, blink/pupil
  state machine, display drivers). TeensyEyes's own README states it is based
  on Adafruit's Uncanny Eyes and M4_Eyes, and on mjs513/GC9A01A_t3n.

### Adafruit Uncanny Eyes
- **Author:** Phil Burgess / Paint Your Dragon, for Adafruit Industries
- **License:** MIT license (stated in the file header: "MIT license.")
- **URL:** https://github.com/adafruit/Uncanny_Eyes/blob/master/uncannyEyes/uncannyEyes.ino
- **Used for:** Original eye-animation concept and algorithms that TeensyEyes
  (and therefore IRIS) are derived from.

### mjs513/GC9A01A_t3n
- **Author:** mjs513 (Mike S)
- **License:** Modified from PJRC's ILI9341_t3n library; distributed "as is,"
  no warranty (standard permissive/BSD-style disclaimer as shown in the
  library source).
- **URL:** https://github.com/mjs513/GC9A01A_t3n
- **Used for:** GC9A01A round-display driver that TeensyEyes's display layer
  (and IRIS's `disp_240_*` headers) build on.

### Thingiverse thing:4643715 -- "Wall-e Eyes from Adafruit" by Xena
- **Author:** Xena (Thingiverse handle) -- itself a remix of Adafruit's
  animated-eyes hardware design
- **License:** Creative Commons Attribution 4.0 International (CC BY 4.0)
  -- confirmed by operator from the page's license badge (automated fetch
  was blocked, HTTP 403; see note below). Full legal text:
  https://creativecommons.org/licenses/by/4.0/legalcode.en
  Attribution-only: no NonCommercial or ShareAlike restriction, but credit
  to Xena (and to Adafruit as the design this remixes) must be given if
  this reference is redistributed.
- **Used for:** Physical eye-housing/mounting reference for the round-display
  eye assembly; referenced in build docs, not shipped as source code here.

---

## Speech pipeline

### Kokoro (hexgrad/Kokoro-82M)
- **Author:** hexgrad
- **License:** Apache License 2.0 (SPDX `Apache-2.0`)
- **URL:** https://github.com/hexgrad/kokoro , model card
  https://huggingface.co/hexgrad/Kokoro-82M
- **Used for:** Primary live TTS voice (`services/tts.py` `_synthesize_kokoro`,
  GandalfAI port 8004).

### faster-whisper
- **Author:** SYSTRAN
- **License:** MIT License, Copyright (c) 2023 SYSTRAN
- **URL:** https://github.com/SYSTRAN/faster-whisper/blob/master/LICENSE
- **Used for:** Speech-to-text transcription engine.

### wyoming-openwakeword
- **Author:** Rhasspy project (rhasspy)
- **License:** Apache License 2.0
- **URL:** https://github.com/rhasspy/wyoming-openwakeword
- **Used for:** Wyoming-protocol wrapper that serves openWakeWord detections
  to IRIS's wake-word listener.

### openWakeWord
- **Author:** dscripka
- **License:** Apache License 2.0 for all repository code. **Pre-trained
  models included in the repo are separately licensed CC BY-NC-SA 4.0**
  (non-commercial) because they were trained partly on datasets with
  restrictive/unknown licensing.
- **URL:** https://github.com/dscripka/openWakeWord/blob/main/LICENSE ,
  https://github.com/dscripka/openWakeWord
- **Used for:** Wake-word detection framework/models (`wakewords/`).

### atlas-voice-training (briankelley/atlas-voice-training)
- **Author:** briankelley
- **License:** Training scripts/configs: Apache 2.0. The README explicitly
  states: "The CC-BY-NC-SA-4.0 license on ACAV100M means trained models
  inherit a non-commercial restriction."
- **URL:** https://github.com/briankelley/atlas-voice-training
- **Used for:** Custom wake-word model training pipeline for IRIS's
  wakeword models (built on openWakeWord). Any wakeword model IRIS trains
  with this pipeline against ACAV100M-derived data inherits the
  non-commercial restriction -- do not represent such models as
  commercially licensed.

---

## LLM

### Ollama
- **Author:** Ollama
- **License:** MIT License, "Copyright (c) Ollama"
- **URL:** https://github.com/ollama/ollama/blob/main/LICENSE
- **Used for:** Model runtime/server on GandalfAI serving `iris:latest`.

### Mistral Small 3.2 24B Instruct (2506) weights
- **Author:** Mistral AI
- **License:** Apache License 2.0 (model card license tag: `apache-2.0`)
- **URL:** https://huggingface.co/mistralai/Mistral-Small-3.2-24B-Instruct-2506
- **Used for:** Base weights for the `iris`/`iris-kids` Ollama models
  (`ollama/iris_modelfile.txt` persona layered on top via Modelfile SYSTEM
  prompt).

---

## Servo / gesture / sensor firmware (`servo_teensy40/`)

### ServoEasing
- **Author:** Armin Joachimsmeyer (ArminJo)
- **License:** GNU General Public License v3.0 (or later)
- **URL:** https://github.com/ArminJo/ServoEasing
- **Used for:** Smooth pan-servo motion easing
  (`servo_teensy40/teensy40_base_mount/pan_servo.cpp` includes
  `<ServoEasing.hpp>`). GPLv3 code linked into the Teensy 4.0 servo firmware
  binary; GPLv3 and AGPL-3.0 (IRIS's own license, see `LICENSE`) are
  one-way compatible per the FSF compatibility matrix, so the combined
  servo-firmware binary is distributable under AGPL-3.0 terms, and the
  full corresponding source (including this vendored library) must remain
  available.

### PAJ7620U2 gesture sensor driver
- **IRIS's own code:** `servo_teensy40/teensy40_base_mount/paj7620.h` /
  `paj7620.cpp` are original register-level I2C driver code written by the
  IRIS project directly against the public PAJ7620U2 datasheet register map
  (register addresses/bit layout are factual/functional, not copyrightable
  expression). They are **not** a copy of any third-party library
  (Seeed `Grove_Gesture`, DFRobot `DFRobot_PAJ7620U2`, or
  `acrandal/RevEng_PAJ7620` were reviewed for comparison and none of their
  source is present here) and carry no separate license obligation beyond
  IRIS's own AGPL-3.0.
- **For reference (not used in this repo):**
  - Seeed `Grove_Gesture` -- MIT License, https://github.com/Seeed-Studio/Grove_Gesture
  - DFRobot `DFRobot_PAJ7620U2` -- https://github.com/DFRobot/DFRobot_PAJ7620U2

### SparkFun_APDS9960 (removed)
- **Author:** SparkFun Electronics (Shawn Hymel et al.)
- **License:** Hardware: CC BY-SA 3.0. All other (software) content:
  Beerware license.
- **URL:** upstream https://github.com/sparkfun/APDS-9960_RGB_and_Gesture_Sensor
- **Status:** Was vendored at
  `servo_teensy40/teensy40_base_mount/lib/SparkFun_APDS9960/` but never
  `#include`d by any `.cpp`/`.h`/`.ino` in that firmware (the active gesture
  sensor is PAJ7620U2, not APDS9960) -- removed from the repo so it does not
  ship with the public release. Listed here for history only.

---

## Pi4 audio hardware

### ReSpeaker 2-Mic HAT (WM8960 codec) driver -- seeed-voicecard
- **Author:** Seeed Studio / respeaker
- **License:** GPL v2 (kernel driver/module source)
- **URL:** https://github.com/respeaker/seeed-voicecard
- **Used for:** ALSA `wm8960` sound-card kernel driver installed on the Pi4
  (system package/kernel module, not vendored source in this repo --
  `pi4/hardware/audio_io.py` talks to it only via ALSA/`amixer`, no linking).

---

## Excluded

- **Coqui XTTS-v2** was benched (S162c) but never shipped and has been
  removed from the live pipeline. Per its CPML (Coqui Public Model License,
  non-commercial) it is intentionally **excluded from this attribution
  list** and from public release artifacts.
