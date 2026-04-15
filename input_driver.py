"""pydirectinput wrapper for Roblox-compatible keyboard/mouse simulation.

Roblox filters input from most pyautogui/pynput/SendInput callers; pydirectinput
uses DirectInput scan codes that Roblox accepts reliably.
"""
from __future__ import annotations

import time
from typing import Optional

try:
    import pydirectinput  # type: ignore
    pydirectinput.PAUSE = 0  # we manage timing ourselves
    pydirectinput.FAILSAFE = False
    _HAS_PDI = True
except ImportError:
    pydirectinput = None  # type: ignore
    _HAS_PDI = False


class Input:
    def __init__(self, dry_run: bool = False, hold_ms: int = 60):
        self.dry_run = dry_run
        self.hold_ms = hold_ms
        self._held: Optional[str] = None
        if not dry_run and not _HAS_PDI:
            raise RuntimeError(
                "pydirectinput is required for live input. "
                "Install with: pip install pydirectinput"
            )

    def press(self, key: str) -> None:
        key = key.lower()
        if self.dry_run:
            print(f"[DRY] press {key}")
            return
        self._held = key
        try:
            pydirectinput.keyDown(key)
            time.sleep(self.hold_ms / 1000.0)
        finally:
            pydirectinput.keyUp(key)
            self._held = None

    def click(self, x: int, y: int) -> None:
        if self.dry_run:
            print(f"[DRY] click ({x},{y})")
            return
        pydirectinput.moveTo(x, y)
        time.sleep(0.02)
        pydirectinput.click()

    def cast(self, cast_point: Optional[tuple], cast_key: str) -> None:
        """Cast the rod: click the configured point if present, else press cast_key."""
        if cast_point is not None:
            self.click(int(cast_point[0]), int(cast_point[1]))
        else:
            self.press(cast_key)

    def release_all(self) -> None:
        """Make sure no key is left held down on exit."""
        if self.dry_run or not _HAS_PDI:
            return
        if self._held is not None:
            try:
                pydirectinput.keyUp(self._held)
            except Exception:
                pass
            self._held = None
