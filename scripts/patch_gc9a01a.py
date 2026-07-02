# scripts/patch_gc9a01a.py
# PlatformIO extra_script (pre) — patches the GC9A01A1_t3n drawChar wrapper
# to call the 7-parameter overload instead of recursing infinitely.
# Runs automatically before every build.  Safe to re-run: checks before patching.

Import("env")  # noqa: F821 — PlatformIO SCons env
import os

TARGET = os.path.join(
    env.subst("$PROJECT_LIBDEPS_DIR"),
    env.subst("$PIOENV"),
    "GC9A01A1_t3n",
    "src",
    "GC9A01A_t3n.h",
)

BAD  = "drawChar(x, y, c, color, bg, size);\n"
GOOD = "drawChar(x, y, c, color, bg, size, size); // fix: was recursive, now calls 7-param overload\n"

def patch():
    if not os.path.isfile(TARGET):
        print(f"[patch_gc9a01a] WARN: target not found: {TARGET}")
        return
    with open(TARGET, "r", encoding="utf-8") as f:
        src = f.read()
    if GOOD.strip() in src:
        print("[patch_gc9a01a] already patched — skip")
        return
    if BAD.strip() not in src:
        print("[patch_gc9a01a] WARN: expected pattern not found — library may have changed")
        return
    patched = src.replace(BAD, GOOD, 1)
    with open(TARGET, "w", encoding="utf-8") as f:
        f.write(patched)
    print("[patch_gc9a01a] patched GC9A01A_t3n.h drawChar wrapper (infinite recursion fix)")

patch()
