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


def _get_foreground_hwnd() -> int:
    """Return the HWND of the currently foreground window, or 0."""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return 0


def _window_title(hwnd: int) -> str:
    if sys.platform != "win32" or not hwnd:
        return ""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        return ""


def _focus_hwnd(hwnd: int, debug: bool = False) -> bool:
    """Force-focus a specific HWND. Returns True iff focus was (re)applied.

    Uses the AttachThreadInput trick to bypass Windows' SetForegroundWindow
    restrictions (you can't normally steal focus from another process unless
    your process was recently foreground). Attaching the calling thread to
    the current foreground thread's input queue lets SetForegroundWindow
    succeed regardless.
    """
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not user32.IsWindow(hwnd):
            if debug:
                print(f"[input] stored HWND {hwnd} is no longer a valid window")
            return False
        # Fast-path: already focused. Don't thrash the input queue.
        if user32.GetForegroundWindow() == hwnd:
            return True
        if debug:
            print(f"[input] refocusing HWND {hwnd} ({_window_title(hwnd)!r})")
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        fg = user32.GetForegroundWindow()
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = False
        if fg_tid and fg_tid != cur_tid:
            attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
        try:
            user32.BringWindowToTop(hwnd)
            ok = bool(user32.SetForegroundWindow(hwnd))
            user32.SetFocus(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
        if not ok and debug:
            print("[input] SetForegroundWindow refused even with AttachThreadInput")
        return ok
    except Exception as e:
        if debug:
            print(f"[input] focus hwnd failed: {e}")
        return False


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
        # Pinned game HWND. If set (via capture_game_window), focus_game will
        # always refocus this exact handle instead of searching by title.
        # This is the bulletproof path: the user alt-tabs into Roblox during
        # the startup countdown, we snapshot the foreground window, and then
        # we can't possibly focus the wrong one.
        self._game_hwnd: int = 0
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
        # Post-click focus sanity check. If a click lands outside the game
        # window, Windows switches focus to whatever was clicked — that's
        # how the "tabs out mid-run" bug manifests. Detect it here so the
        # user knows cast_point is the problem (not title matching).
        if sys.platform == "win32" and self._game_hwnd:
            try:
                import ctypes
                fg = ctypes.windll.user32.GetForegroundWindow()
                if fg != self._game_hwnd:
                    print(f"[input] WARN click stole focus from game "
                          f"(fg={_window_title(fg)!r}). cast_point/retrieve_point "
                          "is likely outside the Roblox window — recalibrate.")
            except Exception:
                pass

    def cast(self, cast_point: Optional[tuple], cast_key: str) -> None:
        """Cast the rod: click the configured point if present, else press cast_key."""
        if cast_point is not None:
            self.click(int(cast_point[0]), int(cast_point[1]))
        else:
            if not self.dry_run:
                print(f"[input] cast -> press '{cast_key}'")
            self.press(cast_key)

    def capture_game_window(self, countdown: int = 4, debug: bool = False) -> bool:
        """Prompt the user to alt-tab into Roblox, then pin whatever window is
        in the foreground when the countdown ends. All subsequent focus calls
        refocus that exact HWND, so we can never accidentally pull a
        PowerShell / VS Code / browser window forward instead.

        Returns True if a non-zero HWND was captured.
        """
        if self.dry_run:
            return False
        if sys.platform != "win32":
            return False
        print(f"\n[input] Switch to Roblox NOW. Capturing focused window in {countdown}s...")
        for i in range(countdown, 0, -1):
            print(f"  {i}...")
            time.sleep(1)
        hwnd = _get_foreground_hwnd()
        if not hwnd:
            print("[input] WARN could not read foreground window; falling back to title match")
            return False
        title = _window_title(hwnd)
        self._game_hwnd = hwnd
        print(f"[input] pinned game window: HWND={hwnd} title={title!r}")
        if "roblox" not in title.lower() and title:
            print("[input] WARN captured window title does not contain 'Roblox'. "
                  "If that's wrong, stop (F6/Esc) and re-run.")
        return True

    def focus_game(self, title: Optional[str], debug: bool = False) -> bool:
        """Force-focus the game window before sending input.

        Preferred path: if a game HWND was captured via capture_game_window(),
        refocus that exact handle. Otherwise fall back to strict title match.
        Returns True if focus was (re)applied.
        """
        if self.dry_run:
            return False
        # Preferred: pinned HWND path.
        if self._game_hwnd:
            ok = _focus_hwnd(self._game_hwnd, debug=debug)
            if ok:
                return True
            # HWND went stale (game closed / relaunched). Fall through to
            # title match as a recovery, and forget the dead handle.
            if debug:
                print("[input] pinned HWND stale; retrying by title")
            self._game_hwnd = 0
        if not title:
            return False
        ok = _focus_window_by_title(title, debug=debug)
        if not ok:
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
