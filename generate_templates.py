"""Generate A-Z templates by rendering a TrueType font through the same
preprocessing pipeline the recognizer uses.

Usage:
    # try common Windows serif fonts in order until one is found
    python generate_templates.py

    # use a specific font
    python generate_templates.py --font "C:/Windows/Fonts/trajanpro-bold.ttf"
    python generate_templates.py --font "C:/Windows/Fonts/times.ttf"

    # only render specific letters (e.g. to fill gaps left by --capture-templates)
    python generate_templates.py --only GT

The output is written to templates/<LETTER>.png and should be immediately
usable by main.py. If a rendered template doesn't match the in-game font
closely enough, the runtime match score will be below 0.85 and the recognizer
will ignore it — in that case use `python main.py --capture-templates` to
replace the bad letters with real in-game crops.
"""
from __future__ import annotations

import argparse
import string
import sys
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Keep in sync with vision.py
TEMPLATE_SIZE = 48
TEMPLATE_DIR = Path(__file__).with_name("templates")

# Best-guess Windows font candidates, in priority order. The first one that
# exists on disk wins. Trajan / Cinzel are closest to the Bridger Western
# look; the others are progressive fallbacks.
FONT_CANDIDATES = [
    r"C:/Windows/Fonts/TrajanPro-Bold.otf",
    r"C:/Windows/Fonts/TrajanPro-Regular.otf",
    r"C:/Windows/Fonts/trajanpro-bold.ttf",
    r"C:/Windows/Fonts/Cinzel-Bold.ttf",
    r"C:/Windows/Fonts/Cinzel-Regular.ttf",
    r"C:/Windows/Fonts/timesbd.ttf",   # Times New Roman Bold
    r"C:/Windows/Fonts/times.ttf",     # Times New Roman
    r"C:/Windows/Fonts/georgiab.ttf",  # Georgia Bold
    # Linux sandbox fallbacks (for testing this script here)
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
]


def find_font(explicit: Optional[str]) -> str:
    if explicit:
        if not Path(explicit).exists():
            sys.exit(f"[generate] font not found: {explicit}")
        return explicit
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return p
    sys.exit(
        "[generate] no candidate font found. Pass --font <path> to a .ttf/.otf, "
        "e.g. C:/Windows/Fonts/times.ttf"
    )


def render_letter(letter: str, font_path: str, render_px: int = 160) -> np.ndarray:
    """Render a single letter as a high-res white-on-black PIL image, return BGR np."""
    font = ImageFont.truetype(font_path, render_px)
    # Measure the glyph so we can pad a canvas big enough for any serifs
    bbox = font.getbbox(letter)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    pad = render_px // 2
    canvas_w = w + 2 * pad
    canvas_h = h + 2 * pad
    img = Image.new("L", (canvas_w, canvas_h), 0)
    draw = ImageDraw.Draw(img)
    draw.text((pad - bbox[0], pad - bbox[1]), letter, fill=255, font=font)
    # Convert to BGR so it goes through the recognizer pipeline identically.
    arr = np.array(img, dtype=np.uint8)
    bgr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    return bgr


def preprocess(img: np.ndarray) -> Optional[np.ndarray]:
    """Mirror of vision._preprocess_letter — must stay in sync with that function."""
    if img.size == 0:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if gray.mean() > 127:
        gray = 255 - gray
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    c = max(contours, key=cv2.contourArea)
    x, y, w, h = cv2.boundingRect(c)
    if w < 3 or h < 3:
        return None
    crop = bw[y : y + h, x : x + w]
    side = max(w, h)
    pad = np.zeros((side, side), dtype=np.uint8)
    dx = (side - w) // 2
    dy = (side - h) // 2
    pad[dy : dy + h, dx : dx + w] = crop
    return cv2.resize(pad, (TEMPLATE_SIZE, TEMPLATE_SIZE), interpolation=cv2.INTER_AREA)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate A-Z letter templates from a font")
    p.add_argument("--font", help="Path to a .ttf/.otf font file")
    p.add_argument("--only", help="Only render these letters, e.g. --only GT", default=None)
    p.add_argument("--out", default=str(TEMPLATE_DIR), help="Output directory (default: templates/)")
    p.add_argument("--overwrite", action="store_true",
                   help="Overwrite existing template files (default: skip existing)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    font_path = find_font(args.font)
    print(f"[generate] using font: {font_path}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    letters = list((args.only or string.ascii_uppercase).upper())
    for ch in letters:
        if ch not in string.ascii_uppercase:
            print(f"[generate] skip invalid letter: {ch}")
            continue
        dst = out_dir / f"{ch}.png"
        if dst.exists() and not args.overwrite:
            print(f"[generate] {dst} exists, skipping (use --overwrite to replace)")
            continue
        rendered = render_letter(ch, font_path)
        processed = preprocess(rendered)
        if processed is None:
            print(f"[generate] FAILED to preprocess {ch}")
            continue
        cv2.imwrite(str(dst), processed)
        print(f"[generate] wrote {dst}")

    print(
        "\nDone. Run `python main.py --debug` and watch the letter scores. "
        "If any are consistently < 0.85, replace that letter with a real "
        "in-game crop via `python main.py --capture-templates`."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
