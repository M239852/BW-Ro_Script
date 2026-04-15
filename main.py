"""Bridger Western fishing-minigame solver — entry point.

Usage:
    python main.py                  # run the bot
    python main.py --calibrate      # (re)run region calibration
    python main.py --dry-run        # full state machine, input is only logged
    python main.py --debug          # verbose logging + dumps to debug/
    python main.py --capture-templates
                                    # live-build A-Z templates from prompts
    python main.py --test-input     # fire cast -> wait -> retrieve a few times
                                    # to verify input reaches the game,
                                    # without sink detection or minigame logic
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import config
from config import Config
from input_driver import Input
from state_machine import FishingBot
from vision import Screen, load_templates, recognize_letter, save_template, _preprocess_letter


def _set_dpi_aware() -> None:
    """Prevent Windows from scaling our captured pixels."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception as e:
        print(f"[main] DPI awareness setup failed: {e}")


def _install_hotkeys(stop_fn) -> None:
    try:
        import keyboard  # type: ignore
    except ImportError:
        print("[main] 'keyboard' package not installed; F6/Esc hot-exit disabled")
        return
    keyboard.add_hotkey("f6", stop_fn)
    keyboard.add_hotkey("esc", stop_fn)
    print("[main] hotkeys: F6 / Esc to stop")


def _capture_templates_mode(cfg: Config, screen: Screen) -> None:
    """Interactively build the A-Z template library.

    The user casts and plays the minigame normally; for each prompt the script
    crops the letter region, asks which letter it is, and saves the template.
    """
    print("\n=== Template capture mode ===")
    print("Play fishing normally. Each new prompt will be saved after you label it.")
    print("Type 'q' to quit.\n")

    templates = load_templates()
    seen: set[str] = set()
    last_raw = None
    try:
        while True:
            img = screen.grab(cfg.letter_region)
            processed = _preprocess_letter(img)
            if processed is None:
                time.sleep(0.05)
                continue
            # Skip if visually identical to the last processed glyph
            if last_raw is not None and processed.shape == last_raw.shape:
                diff = (processed != last_raw).mean()
                if diff < 0.02:
                    time.sleep(0.05)
                    continue
            last_raw = processed

            letter, score, _ = recognize_letter(img, templates)
            if letter is not None and letter in seen:
                time.sleep(0.1)
                continue

            label = input(
                f"Detected={letter} score={score:.2f}. Enter the actual letter (A-Z) or 'skip'/'q': "
            ).strip().upper()
            if label == "Q":
                break
            if label == "SKIP" or not label:
                continue
            if len(label) != 1 or not label.isalpha():
                print("  invalid")
                continue
            save_template(label, processed)
            templates[label] = processed
            seen.add(label)
            print(f"  saved templates/{label}.png  ({len(templates)}/26)")
            if len(templates) >= 26:
                print("All 26 letters captured.")
                break
    except KeyboardInterrupt:
        print("\n[main] capture aborted")


def _list_windows_mode() -> None:
    """Diagnostic: print every visible window title + process name so the
    user can verify what focus_window_title should match."""
    if sys.platform != "win32":
        print("[main] --list-windows is Windows-only")
        return
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, wintypes.HWND, wintypes.LPARAM
    )
    rows = []

    def _process_name(hwnd):
        try:
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            h = kernel32.OpenProcess(0x1000, False, pid.value)
            if not h:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(520)
                size = ctypes.c_ulong(520)
                if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
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
        rows.append((buf.value, _process_name(hwnd)))
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    print("\n=== Visible windows ===")
    for title, proc in rows:
        print(f"  [{proc:<24}]  {title}")
    print(
        f"\nTotal: {len(rows)} windows. Set config.json -> focus_window_title "
        "to the exact window title of your Roblox client (usually just 'Roblox')."
    )


def _watch_bobber_mode(cfg: Config, screen: Screen) -> None:
    """Continuously capture the bobber region and print every strike signal
    plus save annotated frames. No input is sent, no state machine runs.

    Workflow: run this, cast manually in-game, wait for a bite, watch the
    numbers scroll on-screen, and inspect debug/watch/*.png afterward to see
    exactly which pixels the red-trail detector picked up. If a real bite
    happened but no number ever crossed its threshold, the one that got
    closest is the one to lower in config.json.
    """
    import numpy as np
    import cv2
    import vision

    out_dir = Path("debug/watch")
    out_dir.mkdir(parents=True, exist_ok=True)
    # Wipe old frames so it's easier to scan the relevant ones.
    for old in out_dir.glob("*.png"):
        try:
            old.unlink()
        except OSError:
            pass

    print("\n=== Watch-bobber mode ===")
    print("Cast manually in-game. Every frame with high signal will be saved")
    print(f"to {out_dir}/ . Press Ctrl+C (or F6/Esc if hotkeys loaded) to stop.\n")
    print("columns: t  delta  vdrop  edgeVar  edgeD  splash%  mdelta")
    print("         (splash% = fraction of bright saturated pixels, any hue)")

    # Build a rolling baseline from the first few frames so the user doesn't
    # have to time a separate calibration cast.
    warmup = []
    for _ in range(8):
        try:
            warmup.append(screen.grab(cfg.bobber_region))
        except Exception:
            pass
        time.sleep(0.1)
    if not warmup:
        print("[watch] failed to grab bobber_region — is it calibrated?")
        return
    baseline = np.mean(np.stack(warmup).astype(np.float32), axis=0).astype(np.uint8)
    base_edge = vision.bobber_edge_variance(baseline)
    print(f"[watch] baseline edge var={base_edge:.1f}  shape={baseline.shape}")
    cv2.imwrite(str(out_dir / "000_baseline.png"), baseline)

    prev = baseline.copy()
    peak = {"delta": 0.0, "vdrop": 0.0, "edgeD": 0.0, "splash": 0.0, "mdelta": 0.0}
    t0 = time.perf_counter()
    dump_idx = 0
    try:
        while True:
            curr = screen.grab(cfg.bobber_region)
            delta = vision.frame_delta(baseline, curr)
            vdrop = vision.value_drop(baseline, curr)
            edge_var = vision.bobber_edge_variance(curr)
            edge_d = edge_var - base_edge
            splash = vision.bright_splash_fraction(curr)
            # Motion: frame-to-frame delta against the previous capture.
            # This catches flickers/splashes even when vs-baseline is small.
            mdelta = vision.frame_delta(prev, curr)
            prev = curr

            peak["delta"] = max(peak["delta"], delta)
            peak["vdrop"] = max(peak["vdrop"], vdrop)
            peak["edgeD"] = max(peak["edgeD"], edge_d)
            peak["splash"] = max(peak["splash"], splash)
            peak["mdelta"] = max(peak["mdelta"], mdelta)

            t = time.perf_counter() - t0
            line = (
                f"{t:5.1f}s  d={delta:5.1f}  vd={vdrop:5.1f}  "
                f"ev={edge_var:6.1f}  ed={edge_d:+6.1f}  "
                f"splash={splash*100:5.2f}%  m={mdelta:5.1f}"
            )
            # Highlight high-signal frames and dump them.
            high = (
                delta > cfg.sink_threshold
                or vdrop > 8
                or splash >= cfg.splash_min
                or edge_d >= cfg.strike_edge_min
                or mdelta > 8
            )
            if high:
                dump_idx += 1
                annotated = vision.annotate_red_mask(curr)
                cv2.imwrite(str(out_dir / f"{dump_idx:04d}_curr.png"), curr)
                cv2.imwrite(str(out_dir / f"{dump_idx:04d}_mask.png"), annotated)
                print(line + "  *DUMPED*")
            else:
                print(line)
            time.sleep(0.08)
    except KeyboardInterrupt:
        print("\n[watch] stopped.")

    print("\n=== Peak values observed ===")
    print(f"  delta   peak = {peak['delta']:6.1f}  (threshold sink_threshold={cfg.sink_threshold:.1f})")
    print(f"  vdrop   peak = {peak['vdrop']:6.1f}  (hard-coded 12)")
    print(f"  edgeD   peak = {peak['edgeD']:+6.1f}  (threshold strike_edge_min={cfg.strike_edge_min:.1f}, "
          f"strong={cfg.strike_edge_min*2:.1f})")
    print(f"  splash% peak = {peak['splash']*100:6.2f}% (threshold splash_min={cfg.splash_min*100:.2f}%, "
          f"strong={cfg.splash_min*200:.2f}%)")
    print(f"  mdelta  peak = {peak['mdelta']:6.1f}")
    print("\nIf you watched a real bite happen, lower whichever threshold the")
    print("peak got closest to (but did not exceed). Inspect the *_mask.png")
    print("frames in debug/watch/ to verify the red-trail detector is highlighting")
    print("the actual approaching trail and not just noise.")


def _test_input_mode(cfg: Config, inp: Input) -> None:
    """Diagnostic: fire cast -> pause -> retrieve three times, no sink
    detection, no minigame. Use this to prove that the click/focus/coords
    path actually delivers input to the game."""
    print("\n=== Input test mode ===")
    inp.capture_game_window(countdown=4, debug=True)

    rp = cfg.retrieve_point if cfg.retrieve_point is not None else cfg.cast_point
    rk = cfg.retrieve_key if cfg.retrieve_key is not None else cfg.cast_key
    print(f"cast target    = {cfg.cast_point if cfg.cast_point else repr(cfg.cast_key)}")
    print(f"retrieve target= {rp if rp else repr(rk)}")
    print(f"focus title    = {cfg.focus_window_title!r}")
    print(f"click_hold_ms  = {cfg.click_hold_ms}")

    for cycle in range(3):
        print(f"\n--- cycle {cycle+1}/3 ---")
        inp.focus_game(cfg.focus_window_title, debug=True)
        print("[test] CAST")
        inp.cast(cfg.cast_point, cfg.cast_key)
        time.sleep(3.0)

        inp.focus_game(cfg.focus_window_title, debug=True)
        print("[test] RETRIEVE")
        if rp is not None:
            inp.click(int(rp[0]), int(rp[1]))
        else:
            inp.press(rk)
        time.sleep(3.0)

    print("\nDone. If neither cast nor retrieve produced a visible action in "
          "Roblox, the click path is failing — check focus title, cast_point "
          "coordinates, and Windows DPI scaling. Run `python main.py "
          "--list-windows` to see what titles are actually present.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bridger Western fishing bot")
    p.add_argument("--calibrate", action="store_true", help="(Re)run region calibration")
    p.add_argument("--dry-run", action="store_true", help="Log inputs instead of sending them")
    p.add_argument("--debug", action="store_true", help="Verbose logging + debug dumps")
    p.add_argument("--capture-templates", action="store_true",
                   help="Interactively build the A-Z template library")
    p.add_argument("--test-input", action="store_true",
                   help="Diagnostic: fire cast/retrieve a few times with no sink detection")
    p.add_argument("--list-windows", action="store_true",
                   help="Diagnostic: list all visible window titles + process names and exit")
    p.add_argument("--watch-bobber", action="store_true",
                   help="Diagnostic: live-print strike signals and dump annotated "
                        "bobber-region frames to debug/watch/. Cast manually in-game, "
                        "wait for a bite, then inspect the numbers and the frames to "
                        "see what the bot actually saw.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    _set_dpi_aware()

    if args.list_windows:
        _list_windows_mode()
        return 0

    cfg = config.load()
    if cfg is None or args.calibrate:
        from calibrate import run_calibration
        cfg = run_calibration()

    screen = Screen()

    if args.capture_templates:
        _capture_templates_mode(cfg, screen)
        screen.close()
        return 0

    if args.watch_bobber:
        _watch_bobber_mode(cfg, screen)
        screen.close()
        return 0

    inp = Input(
        dry_run=args.dry_run,
        hold_ms=cfg.hold_ms,
        click_hold_ms=cfg.click_hold_ms,
    )

    if args.test_input:
        _test_input_mode(cfg, inp)
        screen.close()
        return 0

    bot = FishingBot(cfg=cfg, inp=inp, screen=screen, debug=args.debug)

    # Pin the Roblox window now, before the loop starts. User alt-tabs into
    # Roblox during the countdown; whatever is in the foreground at the end
    # is the exact HWND every cast/retrieve will refocus. This avoids any
    # title-match ambiguity (folder names, browser tabs, etc).
    inp.capture_game_window(countdown=4, debug=args.debug)

    _install_hotkeys(bot.stop)

    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop()
    finally:
        try:
            import keyboard  # type: ignore
            keyboard.unhook_all()
        except Exception:
            pass
        screen.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
