"""Bridger Western fishing-minigame solver — entry point.

Usage:
    python main.py                  # run the bot
    python main.py --calibrate      # (re)run region calibration
    python main.py --dry-run        # full state machine, input is only logged
    python main.py --debug          # verbose logging + dumps to debug/
    python main.py --capture-templates
                                    # live-build A-Z templates from prompts
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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bridger Western fishing bot")
    p.add_argument("--calibrate", action="store_true", help="(Re)run region calibration")
    p.add_argument("--dry-run", action="store_true", help="Log inputs instead of sending them")
    p.add_argument("--debug", action="store_true", help="Verbose logging + debug dumps")
    p.add_argument("--capture-templates", action="store_true",
                   help="Interactively build the A-Z template library")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    _set_dpi_aware()

    cfg = config.load()
    if cfg is None or args.calibrate:
        from calibrate import run_calibration
        cfg = run_calibration()

    screen = Screen()

    if args.capture_templates:
        _capture_templates_mode(cfg, screen)
        screen.close()
        return 0

    inp = Input(dry_run=args.dry_run, hold_ms=cfg.hold_ms)
    bot = FishingBot(cfg=cfg, inp=inp, screen=screen, debug=args.debug)

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
