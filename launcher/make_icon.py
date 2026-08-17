#!/usr/bin/env python3
"""Draw orrery.ico: the launcher's mark, as a Windows icon.

An orrery is a clockwork model of a solar system, so the icon is one: a lit
core with two orbit rings and a body on each, in the launcher's own palette.
Drawn at 8x and downsampled, which is what keeps the 16px ring from turning
into a dotted mess.

Run:  python make_icon.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "orrery.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)

SUPER = 8                      # supersampling factor
BASE = 256
CANVAS = BASE * SUPER

BACKDROP = (14, 15, 18, 255)   # near the app's page ink, not pure black
# Brighter than the in-app hairline on purpose: at 32px a 0.18-alpha ring
# disappears into the backdrop, and 32px is the size a desktop actually uses.
RING = (255, 255, 255, 92)
ACCENT = (57, 135, 229, 255)   # --accent
ACCENT_HALO = (57, 135, 229, 70)
GOOD = (12, 163, 12, 255)      # --good, the "something is running" green
MUTED = (137, 135, 129, 255)   # --muted


def circle(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float,
           fill=None, outline=None, width: int = 0) -> None:
    draw.ellipse((cx - r, cy - r, cx + r, cy + r),
                 fill=fill, outline=outline, width=width)


def build() -> Image.Image:
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = CANVAS / 2

    # Rounded-square backdrop so the mark reads on any desktop wallpaper.
    pad = CANVAS * 0.045
    d.rounded_rectangle((pad, pad, CANVAS - pad, CANVAS - pad),
                        radius=CANVAS * 0.22, fill=BACKDROP)

    # Two orbits. Thin on purpose: the app's own header mark is hairline.
    inner_r, outer_r = CANVAS * 0.215, CANVAS * 0.345
    stroke = max(1, int(CANVAS * 0.016))
    circle(d, c, c, inner_r, outline=RING, width=stroke)
    circle(d, c, c, outer_r, outline=RING, width=stroke)

    # The core, with a soft halo so it reads as lit rather than painted.
    circle(d, c, c, CANVAS * 0.115, fill=ACCENT_HALO)
    circle(d, c, c, CANVAS * 0.072, fill=ACCENT)

    # Two bodies, set at angles that stay distinct after downsampling.
    for radius, angle_deg, colour, size in (
        (inner_r, -52.0, GOOD, 0.050),
        (outer_r, 141.0, MUTED, 0.038),
    ):
        angle = math.radians(angle_deg)
        bx = c + radius * math.cos(angle)
        by = c + radius * math.sin(angle)
        if colour is GOOD:                       # running body glows a little
            circle(d, bx, by, CANVAS * (size + 0.030), fill=(12, 163, 12, 60))
        circle(d, bx, by, CANVAS * size, fill=colour)

    return img


def main() -> None:
    master = build().resize((BASE, BASE), Image.LANCZOS)
    master.save(OUT, format="ICO",
                sizes=[(s, s) for s in SIZES])
    print(f"wrote {OUT}  ({OUT.stat().st_size:,} bytes, sizes {SIZES})")


if __name__ == "__main__":
    main()
