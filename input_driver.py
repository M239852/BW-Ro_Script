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


def _focus_window_by_title(title: str, debug: bool = False) -> bool:
    """Best-effort bring a window to the foreground by **strict** title match.

    The match is exact (case-insensitive), OR the window title starts with
    ``title + " "`` / ``title + "-"`` so things like ``"Roblox - MyGame"``
    still resolve. Substring matching is intentionally avoided because a
    working directory such as ``BW-Ro_Script-...-roblox-...`` can end up in
    the terminal window title and fool a naive substring match, causing the
    bot to focus its own PowerShell window and tab out of the game.

    Windows belonging to common shell / terminal / editor processes are also
    skipped even if their title matches, so this is belt-and-suspenders safe.

    Returns True iff a matching window was found and focused. On non-Windows
    platforms returns False without raising.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        SKIP_PROCESSES = {
            "powershell.exe", "pwsh.exe", "cmd.exe",
            "windowsterminal.exe", "conhost.exe",
            "code.exe", "explorer.exe",
            "python.exe", "py.exe", "pythonw.exe",
        }

        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HWND, wintypes.LPARAM
        )
        found_hwnd = [0]
        found_title = [""]
        title_l = title.lower()

        def _process_name(hwnd: int) -> str:
            try:
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                h = kernel32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
                )
                if not h:
                    return ""
                try:
                    buf = ctypes.create_unicode_buffer(520)
                    size = ctypes.c_ulong(520)
                    if kernel32.QueryFullProcessImageNameW(
                        h, 0, buf, ctypes.byref(size)
                    ):
                        return buf.value.rsplit("\\", 1)[-1]
                finally:
                    kernel32.CloseHandle(h)
            except Exception:
                pass
            return ""

        def _cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            wtitle = buf.value
            wl = wtitle.lower()
            # Strict: exact, or starts with "<title> " / "<title>-" so
            # titles like "Roblox - GameName" resolve while substring
            # matches against random paths are rejected.
            if (
                wl != title_l
                and not wl.startswith(title_l + " ")
                and not wl.startswith(title_l + "-")
            ):
                return True
            proc = _process_name(hwnd).lower()
            if proc in SKIP_PROCESSES:
                if debug:
                    print(f"[input] skipping {wtitle!r} (process {proc})")
                return True
            found_hwnd[0] = hwnd
            found_title[0] = wtitle
            return False  # stop enumeration

        user32.EnumWindows(EnumWindowsProc(_cb), 0)
        if not found_hwnd[0]:
            return False

        if debug:
            print(f"[input] focusing {found_title[0]!r}")
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

    def focus_game(self, title: Optional[str], debug: bool = False) -> bool:
        """Best-effort force-focus the game window before sending input.

        Returns True if focus was changed. Silently returns False if title
        is falsy, if we're in dry-run, or if no matching window was found.
        """
        if self.dry_run or not title:
            return False
        ok = _focus_window_by_title(title, debug=debug)
        if not ok:
            # One retry after a short pause — Windows sometimes rejects the
            # first SetForegroundWindow if another process recently used it.
            time.sleep(0.05)
            ok = _focus_window_by_title(title, debug=debug)
        if not ok:
            print(f"[input] WARN no window matching title '{title}' "
                  "(expected exact match or 'Roblox - ...' prefix)")
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
