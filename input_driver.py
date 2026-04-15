"""pydirectinput wrapper for Roblox-compatible keyboard/mouse simulation.

Roblox filters input from most pyautogui/pynput/SendInput callers; pydirectinput
uses DirectInput scan codes that Roblox accepts reliably.

Click reliability notes:
- pydirectinput.click() fires a very short down->up sequence (~1 ms) that
  Roblox sometimes drops. This wrapper uses explicit mouseDown + sleep +
  mouseUp with a configurable hold time (click_hold_ms, default 50 ms).
- Games only accept input on the foreground window. focus_game() force-
  focuses a window by title (default "Roblox") before firing cast/retrieve
  so mis-focused clicks don't silently vanish into other windows.
"""
from __future__ import annotations

import sys
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


def _focus_window_by_title(title: str) -> bool:
    """Best-effort bring a window to the foreground by substring title match.

    Returns True if a matching window was found and focused. On non-Windows
    platforms returns False without raising.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        EnumWindows = user32.EnumWindows
        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_int, ctypes.c_int
        )
        GetWindowTextLength = user32.GetWindowTextLengthW
        GetWindowText = user32.GetWindowTextW
        IsWindowVisible = user32.IsWindowVisible

        found_hwnd = [0]

        def _cb(hwnd, _lparam):
            if not IsWindowVisible(hwnd):
                return True
            length = GetWindowTextLength(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            GetWindowText(hwnd, buf, length + 1)
            if title.lower() in buf.value.lower():
                found_hwnd[0] = hwnd
                return False  # stop enumeration
            return True

        EnumWindows(EnumWindowsProc(_cb), 0)
        if not found_hwnd[0]:
            return False

        hwnd = found_hwnd[0]
        # Restore if minimized (SW_RESTORE = 9)
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


class Input:
    def __init__(
        self,
        dry_run: bool = False,
        hold_ms: int = 60,
        click_hold_ms: int = 50,
    ):
        self.dry_run = dry_run
        self.hold_ms = hold_ms
        self.click_hold_ms = click_hold_ms
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
        """Move to (x,y) and perform a single left click with an explicit
        mouseDown -> hold -> mouseUp sequence. The hold time matters: a ~1 ms
        click (what pydirectinput.click() does by default) is often dropped
        by Roblox."""
        if self.dry_run:
            print(f"[DRY] click ({x},{y})")
            return
        print(f"[input] click -> ({x},{y}) hold={self.click_hold_ms}ms")
        pydirectinput.moveTo(x, y)
        time.sleep(0.03)
        # Verify the OS actually moved the cursor (useful when DPI scaling
        # or a locked cursor silently rejects the move).
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                pt = wintypes.POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                if abs(pt.x - x) > 3 or abs(pt.y - y) > 3:
                    print(f"[input] WARN cursor at ({pt.x},{pt.y}) not ({x},{y}) — "
                          f"DPI or focus issue?")
            except Exception as e:
                print(f"[input] cursor check failed: {e}")
        pydirectinput.mouseDown(button="left")
        time.sleep(self.click_hold_ms / 1000.0)
        pydirectinput.mouseUp(button="left")

    def cast(self, cast_point: Optional[tuple], cast_key: str) -> None:
        """Cast the rod: click the configured point if present, else press cast_key."""
        if cast_point is not None:
            self.click(int(cast_point[0]), int(cast_point[1]))
        else:
            if not self.dry_run:
                print(f"[input] cast -> press '{cast_key}'")
            self.press(cast_key)

    def focus_game(self, title: Optional[str]) -> bool:
        """Best-effort force-focus the game window before sending input.

        Returns True if focus was changed. Silently returns False if title
        is falsy, if we're in dry-run, or if no matching window was found.
        """
        if self.dry_run or not title:
            return False
        ok = _focus_window_by_title(title)
        if not ok:
            # One retry after a short pause — Windows sometimes rejects the
            # first SetForegroundWindow if another process recently used it.
            time.sleep(0.05)
            ok = _focus_window_by_title(title)
        if not ok:
            print(f"[input] WARN could not focus window containing '{title}'")
        return ok

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
