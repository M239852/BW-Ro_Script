"""FishingBot state machine for the Bridger Western fishing minigame."""
from __future__ import annotations

import threading
import time
from enum import Enum
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from config import Config
from input_driver import Input
import vision
from vision import Screen


class State(Enum):
    IDLE = "IDLE"
    CASTING = "CASTING"
    WAITING_SINK = "WAITING_SINK"
    MINIGAME = "MINIGAME"
    CHEST = "CHEST"
    RECOVER = "RECOVER"


TICK_SECONDS = 0.035
CAST_LOCKOUT_S = 0.8
SINK_TIMEOUT_S = 20.0
MINIGAME_MAX_S = 45.0
LETTER_DEBOUNCE_S = 0.08


class FishingBot:
    def __init__(
        self,
        cfg: Config,
        inp: Input,
        screen: Screen,
        debug: bool = False,
    ):
        self.cfg = cfg
        self.inp = inp
        self.screen = screen
        self.debug = debug

        self.templates = vision.load_templates()
        if debug:
            print(f"[bot] loaded {len(self.templates)} letter templates")

        self._stop = threading.Event()
        self.state = State.IDLE

        # per-state scratch
        self._baseline_bobber: Optional[np.ndarray] = None
        self._cast_t: float = 0.0
        self._state_t: float = 0.0
        self._last_letter: Optional[str] = None
        self._last_letter_t: float = 0.0
        self._sink_hits: int = 0
        self._win_hits: int = 0
        self._fail_hits: int = 0
        self._last_green_frac: float = 0.0

        if debug:
            Path("debug").mkdir(exist_ok=True)

    # -------- public --------

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        print("[bot] running. Press F6 or Esc to stop.")
        self._enter(State.CASTING)
        try:
            while not self._stop.is_set():
                self._step()
                time.sleep(TICK_SECONDS)
        finally:
            self.inp.release_all()
            print("[bot] stopped.")

    # -------- internal --------

    def _enter(self, new_state: State) -> None:
        if self.debug and new_state != self.state:
            print(f"[bot] {self.state.value} -> {new_state.value}")
        self.state = new_state
        self._state_t = time.perf_counter()
        # reset per-state counters
        self._sink_hits = 0
        self._win_hits = 0
        self._fail_hits = 0
        self._last_letter = None
        self._last_letter_t = 0.0
        self._last_green_frac = 0.0

    def _elapsed(self) -> float:
        return time.perf_counter() - self._state_t

    def _step(self) -> None:
        if self.state == State.CASTING:
            self._handle_casting()
        elif self.state == State.WAITING_SINK:
            self._handle_waiting_sink()
        elif self.state == State.MINIGAME:
            self._handle_minigame()
        elif self.state == State.CHEST:
            self._handle_chest()
        elif self.state == State.RECOVER:
            self._handle_recover()

    # -- CASTING
    def _handle_casting(self) -> None:
        self.inp.cast(self.cfg.cast_point, self.cfg.cast_key)
        self._cast_t = time.perf_counter()
        # Let the cast animation settle before snapshotting the baseline.
        time.sleep(0.6)
        try:
            self._baseline_bobber = self.screen.grab(self.cfg.bobber_region)
        except Exception as e:
            print(f"[bot] bobber grab failed: {e}")
            self._baseline_bobber = None
        self._enter(State.WAITING_SINK)

    # -- WAITING_SINK
    def _handle_waiting_sink(self) -> None:
        if self._elapsed() > SINK_TIMEOUT_S:
            if self.debug:
                print("[bot] sink timeout")
            self._enter(State.RECOVER)
            return

        if self._baseline_bobber is None:
            return
        curr = self.screen.grab(self.cfg.bobber_region)
        delta = vision.frame_delta(self._baseline_bobber, curr)
        vdrop = vision.value_drop(self._baseline_bobber, curr)

        if self.debug and int(self._elapsed() * 10) % 5 == 0:
            pass  # silence heavy log

        since_cast = time.perf_counter() - self._cast_t
        if since_cast < CAST_LOCKOUT_S:
            return

        if delta > self.cfg.sink_threshold or vdrop > 12:
            self._sink_hits += 1
        else:
            self._sink_hits = max(0, self._sink_hits - 1)

        if self._sink_hits >= 2:
            if self.debug:
                print(f"[bot] SINK delta={delta:.1f} vdrop={vdrop:.1f}")
            self._enter(State.MINIGAME)

    # -- MINIGAME
    def _handle_minigame(self) -> None:
        if self._elapsed() > MINIGAME_MAX_S:
            if self.debug:
                print("[bot] minigame timeout")
            self._enter(State.RECOVER)
            return

        # 1) check progress bar
        bar = self.screen.grab(self.cfg.progress_region)
        green_frac, red_frac = vision.progress_metrics(bar)

        if green_frac >= self.cfg.win_fill:
            self._win_hits += 1
        else:
            self._win_hits = 0
        if red_frac >= self.cfg.fail_red:
            self._fail_hits += 1
        else:
            self._fail_hits = 0

        if self._win_hits >= 2:
            if self.debug:
                print(f"[bot] WIN green={green_frac:.2f}")
            self._enter(State.CHEST)
            return
        if self._fail_hits >= 2:
            if self.debug:
                print(f"[bot] FAIL red={red_frac:.2f}")
            self._enter(State.RECOVER)
            return

        # Clear last_letter when progress advances so repeats can be pressed.
        if green_frac > self._last_green_frac + 0.02:
            self._last_letter = None
        self._last_green_frac = max(self._last_green_frac, green_frac)

        # 2) recognize + press the letter
        letter_img = self.screen.grab(self.cfg.letter_region)
        letter, score, processed = vision.recognize_letter(letter_img, self.templates)

        now = time.perf_counter()
        if letter is None:
            if processed is not None and score < 0.3 and self.debug:
                # Likely no prompt visible; don't spam.
                pass
            return

        if letter == self._last_letter and (now - self._last_letter_t) < 0.4:
            return

        if self.debug:
            print(f"[bot] letter={letter} score={score:.2f} -> press")
        self.inp.press(letter)
        self._last_letter = letter
        self._last_letter_t = now
        time.sleep(LETTER_DEBOUNCE_S)

    # -- CHEST
    def _handle_chest(self) -> None:
        if self.debug:
            print("[bot] collecting chest")
        for _ in range(3):
            self.inp.press(self.cfg.chest_key)
            time.sleep(0.35)
        self._enter(State.CASTING)

    # -- RECOVER
    def _handle_recover(self) -> None:
        time.sleep(1.5)
        self._enter(State.CASTING)

    # -- debug dump
    def _dump(self, tag: str, img: np.ndarray) -> None:
        if not self.debug:
            return
        ts = int(time.time() * 1000)
        cv2.imwrite(f"debug/{tag}_{ts}.png", img)
