"""Screen capture + image analysis for the fishing bot.

All detection is done on numpy BGR frames. No external OCR binary.
"""
from __future__ import annotations

import string
import time
from pathlib import Path
from typing import Dict, Literal, Optional

import cv2
import numpy as np

try:
    import mss  # type: ignore
    _HAS_MSS = True
except ImportError:
    mss = None  # type: ignore
    _HAS_MSS = False

from config import Region

TEMPLATE_DIR = Path(__file__).with_name("templates")
LETTERS = list(string.ascii_uppercase)
TEMPLATE_SIZE = 48
MATCH_THRESHOLD = 0.85


class Screen:
    """Thin wrapper over mss for region capture."""

    def __init__(self):
        if not _HAS_MSS:
            raise RuntimeError("mss is required. pip install mss")
        self._sct = mss.mss()

    def grab(self, region: Region) -> np.ndarray:
        raw = self._sct.grab(region.as_mss())
        # raw is BGRA; drop alpha
        img = np.asarray(raw, dtype=np.uint8)[:, :, :3]
        return img  # BGR

    def close(self):
        try:
            self._sct.close()
        except Exception:
            pass


# --- bobber sink detection -------------------------------------------------

def frame_delta(baseline: np.ndarray, curr: np.ndarray) -> float:
    """Mean absolute pixel delta between two BGR frames (grayscale-based)."""
    if baseline.shape != curr.shape:
        return 0.0
    a = cv2.cvtColor(baseline, cv2.COLOR_BGR2GRAY)
    b = cv2.cvtColor(curr, cv2.COLOR_BGR2GRAY)
    return float(cv2.absdiff(a, b).mean())


def value_drop(baseline: np.ndarray, curr: np.ndarray) -> float:
    """HSV Value-channel mean drop. Positive if curr is darker than baseline."""
    if baseline.shape != curr.shape:
        return 0.0
    a = cv2.cvtColor(baseline, cv2.COLOR_BGR2HSV)[:, :, 2].mean()
    b = cv2.cvtColor(curr, cv2.COLOR_BGR2HSV)[:, :, 2].mean()
    return float(a - b)


def bobber_edge_variance(img: np.ndarray) -> float:
    """Laplacian variance — low for featureless water, high when a bobber is present.

    Empty water (small ripples) typically yields variance < 5.
    A bobber with red/white contrast typically yields variance > 30.
    """
    if img.size == 0:
        return 0.0
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def red_trail_fraction(img: np.ndarray) -> float:
    """Fraction of pixels matching the red/orange trail mask. Kept for
    backwards compatibility; most games use bright_splash_fraction now."""
    if img.size == 0:
        return 0.0
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    warm = (((H <= 18) | (H >= 165)) & (S >= 90) & (V >= 70))
    return float(warm.sum()) / float(warm.size)


def bright_splash_fraction(img: np.ndarray) -> float:
    """Fraction of pixels that look like a bright saturated splash against
    dark water. Hue-agnostic — catches cyan/teal Bridger Western bite splashes,
    red/orange trails, white foam, yellow/green particles, anything.

    The water baseline is nearly black-teal (very low V), so any bright
    saturated pixel is by definition "something new" — fish splash, ripple
    trail, particle effect, etc. This is the most reliable bite signal in
    practice because it doesn't assume the game uses any specific color.
    """
    if img.size == 0:
        return 0.0
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    V = hsv[:, :, 2]
    S = hsv[:, :, 1]
    bright = (V >= 120) & (S >= 60)
    return float(bright.sum()) / float(bright.size)


def edge_variance_delta(baseline: np.ndarray, curr: np.ndarray) -> float:
    """Structural-feature increase in curr vs baseline. Positive values mean
    new edges have appeared in the region — e.g. a splash ring, smoke effect,
    or red ripple trail over quiet water. This is robust to overall brightness
    changes (unlike frame_delta) because it only measures high-frequency
    structure, not mean pixel value."""
    if baseline.size == 0 or curr.size == 0:
        return 0.0
    return bobber_edge_variance(curr) - bobber_edge_variance(baseline)


def extract_bobber_template(baseline: np.ndarray) -> Optional[np.ndarray]:
    """Cut a small window from the center of the bobber_region baseline to
    use as a tracking template. The region is assumed to be calibrated so
    that the bobber sits roughly in the middle — we take a 40%-sized central
    crop, which is almost guaranteed to contain the bobber and little else.

    Returns a grayscale template, or None if the baseline is unusable.
    """
    if baseline is None or baseline.size == 0:
        return None
    gray = cv2.cvtColor(baseline, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    if h < 8 or w < 8:
        return None
    ch = max(6, int(h * 0.4))
    cw = max(6, int(w * 0.4))
    y0 = (h - ch) // 2
    x0 = (w - cw) // 2
    return gray[y0:y0 + ch, x0:x0 + cw].copy()


def track_bobber(
    curr: np.ndarray,
    template: np.ndarray,
) -> tuple[float, tuple[int, int]]:
    """Find the best match of `template` inside `curr`, weather-resistant.

    Returns (score, (x, y)) where score is TM_CCOEFF_NORMED in [-1, 1] and
    (x, y) is the top-left of the best match inside `curr` (grayscale coords).
    Falls back to (0.0, (0, 0)) on any failure.

    The bite signal is derived from this:
      - Bobber at rest: score ~0.95+, position stable
      - Fish bites (bobber dips underwater, ripples hide it, splash covers
        it): score drops sharply — often below 0.6 within one frame
      - Weather noise (rain, wind ripples): barely moves the score because
        the bobber itself is still the best match in the region
    """
    if curr is None or curr.size == 0 or template is None or template.size == 0:
        return 0.0, (0, 0)
    gray = cv2.cvtColor(curr, cv2.COLOR_BGR2GRAY)
    th, tw = template.shape[:2]
    if gray.shape[0] < th or gray.shape[1] < tw:
        return 0.0, (0, 0)
    try:
        res = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        return float(max_val), (int(max_loc[0]), int(max_loc[1]))
    except cv2.error:
        return 0.0, (0, 0)


def annotate_red_mask(img: np.ndarray) -> np.ndarray:
    """Return a BGR image with the bright-splash mask overlaid in bright
    magenta, so the user can visually verify what the splash detector sees."""
    if img.size == 0:
        return img
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    V = hsv[:, :, 2]
    S = hsv[:, :, 1]
    bright = (V >= 120) & (S >= 60)
    out = img.copy()
    out[bright] = (255, 0, 255)  # magenta, very distinct
    return out


# --- progress bar ----------------------------------------------------------

ProgressState = Literal["filling", "win", "fail", "unknown"]


def progress_metrics(img: np.ndarray) -> tuple[float, float]:
    """Return (green_fraction, red_fraction) along the center row of the bar.

    Uses HSV masks robust to brightness variation.
    """
    if img.size == 0:
        return 0.0, 0.0
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h = img.shape[0]
    # Sample a 3-row-tall strip at the center to smooth noise
    y0 = max(0, h // 2 - 1)
    y1 = min(h, h // 2 + 2)
    strip = hsv[y0:y1, :, :]

    H = strip[:, :, 0]
    S = strip[:, :, 1]
    V = strip[:, :, 2]

    green = ((H >= 40) & (H <= 85) & (S > 80) & (V > 80))
    red = (((H < 10) | (H > 170)) & (S > 100) & (V > 80))

    total = strip.shape[0] * strip.shape[1]
    if total == 0:
        return 0.0, 0.0
    return float(green.sum() / total), float(red.sum() / total)


def progress_state(
    img: np.ndarray,
    win_fill: float = 0.90,
    fail_red: float = 0.50,
) -> ProgressState:
    green_frac, red_frac = progress_metrics(img)
    if green_frac >= win_fill:
        return "win"
    if red_frac >= fail_red:
        return "fail"
    if green_frac > 0.02 or red_frac > 0.02:
        return "filling"
    return "unknown"


# --- letter recognition ----------------------------------------------------

def _preprocess_letter(img: np.ndarray) -> Optional[np.ndarray]:
    """Grayscale → binarize → crop to largest contour bbox → resize 48×48.

    Returns a uint8 binary image, or None if no glyph found.
    """
    if img.size == 0:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # Invert if the background is brighter than the glyph (common for UI overlays).
    if gray.mean() > 127:
        gray = 255 - gray
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    # Pick the largest contour by area — that's the glyph
    c = max(contours, key=cv2.contourArea)
    x, y, w, h = cv2.boundingRect(c)
    if w < 3 or h < 3:
        return None
    crop = bw[y : y + h, x : x + w]
    # Pad to square to preserve aspect before resize
    side = max(w, h)
    pad = np.zeros((side, side), dtype=np.uint8)
    dx = (side - w) // 2
    dy = (side - h) // 2
    pad[dy : dy + h, dx : dx + w] = crop
    return cv2.resize(pad, (TEMPLATE_SIZE, TEMPLATE_SIZE), interpolation=cv2.INTER_AREA)


def load_templates(template_dir: Path = TEMPLATE_DIR) -> Dict[str, np.ndarray]:
    templates: Dict[str, np.ndarray] = {}
    if not template_dir.exists():
        return templates
    for p in template_dir.glob("*.png"):
        name = p.stem.upper()
        if len(name) == 1 and name in LETTERS:
            img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            if img.shape != (TEMPLATE_SIZE, TEMPLATE_SIZE):
                img = cv2.resize(img, (TEMPLATE_SIZE, TEMPLATE_SIZE), interpolation=cv2.INTER_AREA)
            templates[name] = img
    return templates


def save_template(letter: str, processed: np.ndarray, template_dir: Path = TEMPLATE_DIR) -> None:
    template_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(template_dir / f"{letter.upper()}.png"), processed)


def save_unknown(processed: np.ndarray, template_dir: Path = TEMPLATE_DIR) -> Path:
    """Save an unrecognized glyph for later manual labeling."""
    unk_dir = template_dir / "unknown"
    unk_dir.mkdir(parents=True, exist_ok=True)
    path = unk_dir / f"unk_{int(time.time()*1000)}.png"
    cv2.imwrite(str(path), processed)
    return path


def recognize_letter(
    img: np.ndarray,
    templates: Dict[str, np.ndarray],
    threshold: float = MATCH_THRESHOLD,
) -> tuple[Optional[str], float, Optional[np.ndarray]]:
    """Return (letter, score, processed_glyph). letter is None if no confident match."""
    processed = _preprocess_letter(img)
    if processed is None or not templates:
        return None, 0.0, processed

    best_letter: Optional[str] = None
    best_score = -1.0
    for letter, tpl in templates.items():
        res = cv2.matchTemplate(processed, tpl, cv2.TM_CCOEFF_NORMED)
        score = float(res.max())
        if score > best_score:
            best_score = score
            best_letter = letter

    if best_score >= threshold:
        return best_letter, best_score, processed
    return None, best_score, processed


# --- self-test entry point -------------------------------------------------

def _selftest() -> None:
    """Minimal offline sanity check. Run: python vision.py"""
    # Green bar
    green = np.zeros((20, 100, 3), dtype=np.uint8)
    green[:, :, 1] = 200  # green channel
    # Convert to HSV-friendly pure green
    green_bgr = np.full((20, 100, 3), (0, 200, 0), dtype=np.uint8)
    assert progress_state(green_bgr, win_fill=0.9) == "win", "green bar should be win"

    red_bgr = np.full((20, 100, 3), (0, 0, 200), dtype=np.uint8)
    assert progress_state(red_bgr, fail_red=0.5) == "fail", "red bar should be fail"

    blank = np.zeros((20, 100, 3), dtype=np.uint8)
    assert progress_state(blank) == "unknown", "blank bar should be unknown"

    # Bobber delta
    a = np.full((40, 40, 3), 100, dtype=np.uint8)
    b = np.full((40, 40, 3), 40, dtype=np.uint8)
    assert frame_delta(a, a) < 1.0
    assert frame_delta(a, b) > 50.0
    assert value_drop(a, b) > 40.0

    # Letter preprocessing round-trip
    canvas = np.full((80, 80, 3), 255, dtype=np.uint8)
    cv2.putText(canvas, "A", (10, 65), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 0), 4)
    processed = _preprocess_letter(canvas)
    assert processed is not None and processed.shape == (TEMPLATE_SIZE, TEMPLATE_SIZE)

    print("vision self-test: OK")


if __name__ == "__main__":
    _selftest()
