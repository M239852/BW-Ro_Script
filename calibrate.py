"""Interactive screen-region calibration via a fullscreen tkinter overlay.

Runs on first launch (or when --calibrate is passed). Asks the user to drag
rectangles over the letter, bobber, and progress-bar regions, then click a
single point for the cast location.
"""
from __future__ import annotations

import tkinter as tk
from typing import Optional, Tuple

from config import Config, Region, save


class _RegionPicker:
    def __init__(self, prompt: str, mode: str):
        """mode: 'region' for drag-select, 'point' for single click."""
        self.prompt = prompt
        self.mode = mode
        self.result: Optional[tuple] = None

        self.root = tk.Tk()
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-alpha", 0.30)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="black")
        self.root.config(cursor="cross")

        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        label_text = f"{prompt}    (Esc to cancel)"
        self.canvas.create_text(
            self.root.winfo_screenwidth() // 2,
            40,
            text=label_text,
            fill="white",
            font=("Segoe UI", 18, "bold"),
        )

        self._start: Optional[Tuple[int, int]] = None
        self._rect_id: Optional[int] = None

        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Escape>", self._on_cancel)

    def _on_press(self, event):
        self._start = (event.x_root, event.y_root)
        if self.mode == "region":
            self._rect_id = self.canvas.create_rectangle(
                event.x, event.y, event.x, event.y,
                outline="lime", width=3,
            )

    def _on_drag(self, event):
        if self.mode != "region" or self._rect_id is None or self._start is None:
            return
        x0 = self._start[0] - self.root.winfo_rootx()
        y0 = self._start[1] - self.root.winfo_rooty()
        self.canvas.coords(self._rect_id, x0, y0, event.x, event.y)

    def _on_release(self, event):
        end = (event.x_root, event.y_root)
        if self.mode == "point":
            self.result = end
        elif self._start is not None:
            x0, y0 = self._start
            x1, y1 = end
            x, y = min(x0, x1), min(y0, y1)
            w, h = abs(x1 - x0), abs(y1 - y0)
            if w < 4 or h < 4:
                # Too small — treat as a cancelled drag; don't close.
                if self._rect_id is not None:
                    self.canvas.delete(self._rect_id)
                self._rect_id = None
                self._start = None
                return
            self.result = (x, y, w, h)
        self.root.destroy()

    def _on_cancel(self, _event=None):
        self.result = None
        self.root.destroy()

    def run(self):
        self.root.mainloop()
        return self.result


def pick_region(prompt: str) -> Region:
    while True:
        res = _RegionPicker(prompt, "region").run()
        if res is None:
            raise SystemExit("[calibrate] cancelled")
        x, y, w, h = res
        return Region(x=int(x), y=int(y), w=int(w), h=int(h))


def pick_point(prompt: str) -> Tuple[int, int]:
    res = _RegionPicker(prompt, "point").run()
    if res is None:
        raise SystemExit("[calibrate] cancelled")
    return int(res[0]), int(res[1])


def _prompt_key(label: str, default: str) -> str:
    raw = input(f"{label} [{default}]: ").strip()
    return raw if raw else default


def _prompt_bool(label: str, default: bool) -> bool:
    d = "Y/n" if default else "y/N"
    raw = input(f"{label} ({d}): ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


def run_calibration() -> Config:
    print("\n=== Bridger Western Fishing Bot — Calibration ===")
    print("A translucent overlay will appear. Drag a rectangle over each region.")
    print("Make the letter region tight around where the letter appears.\n")
    input("Press Enter to begin...")

    letter_region = pick_region("1/4  Drag a box over the LETTER prompt area")
    bobber_region = pick_region("2/4  Drag a box over the BOBBER in the water")
    progress_region = pick_region("3/4  Drag a box over the PROGRESS BAR")

    use_click = _prompt_bool(
        "4/4  Cast by clicking a screen point? (no = use a keyboard key)", True
    )
    cast_point = None
    cast_key = "1"
    if use_click:
        cast_point = pick_point("4/4  Click the CAST point (where to click to cast)")
    else:
        cast_key = _prompt_key("Cast key", "1")

    chest_key = _prompt_key("Chest collect key", "e")

    cfg = Config(
        letter_region=letter_region,
        bobber_region=bobber_region,
        progress_region=progress_region,
        cast_point=cast_point,
        cast_key=cast_key,
        chest_key=chest_key,
    )
    save(cfg)
    print("\n[calibrate] done. You can re-run with --calibrate to redo this at any time.\n")
    return cfg
