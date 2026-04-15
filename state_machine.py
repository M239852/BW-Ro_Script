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
    RETRIEVING = "RETRIEVING"
    MINIGAME = "MINIGAME"
    CHEST = "CHEST"
    RECOVER = "RECOVER"


TICK_SECONDS = 0.035
CAST_LOCKOUT_S = 0.8
SINK_TIMEOUT_S = 20.0
MINIGAME_MAX_S = 45.0
LETTER_DEBOUNCE_S = 0.08

# Post-cast settle: how long to wait for the bobber to land and ripples to
# calm before snapshotting the sink baseline. Too short and the baseline is
# noisy; too long and we waste fishing time. 1.8 s is a reasonable middle.
CAST_SETTLE_S = 1.8
CAST_SETTLE_FRAMES = 6
# Heartbeat interval for WAITING_SINK debug log.
HEARTBEAT_S = 3.0
# After retrieve click, how long to wait for the minigame UI to pop.
RETRIEVE_WAIT_S = 0.5


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
        self._last_heartbeat: float = 0.0
        # Persistent across state transitions — counts consecutive "no bobber
        # after cast" failures so we can back off after repeated misses.
        self._cast_attempts: int = 0
        # Rolling buffer of (timestamp, frame, signals) during WAITING_SINK.
        # On strike fire we dump the tail so you can see exactly what the
        # bot was reacting to — this is the only way to debug "wrong timing".
        self._strike_history: list = []
        self._strike_dump_idx: int = 0

        if debug:
            Path("debug").mkdir(exist_ok=True)
            Path("debug/strikes").mkdir(exist_ok=True)

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
        if new_state == State.WAITING_SINK:
            self._strike_history = []

    def _elapsed(self) -> float:
        return time.perf_counter() - self._state_t

    def _step(self) -> None:
        if self.state == State.CASTING:
            self._handle_casting()
        elif self.state == State.WAITING_SINK:
            self._handle_waiting_sink()
        elif self.state == State.RETRIEVING:
            self._handle_retrieving()
        elif self.state == State.MINIGAME:
            self._handle_minigame()
        elif self.state == State.CHEST:
            self._handle_chest()
        elif self.state == State.RECOVER:
            self._handle_recover()

    def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep in small chunks so F6/Esc aborts quickly. Returns False if stopped."""
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            if self._stop.is_set():
                return False
            time.sleep(min(0.05, end - time.perf_counter()))
        return True

    def _dump_strike_history(self, reason: str) -> None:
        """Save the last ~1 second of bobber-region frames leading up to a
        strike fire, plus the annotated mask for each. Lets the user see
        exactly what triggered the strike and whether the timing was right."""
        if not self.debug or not self._strike_history:
            return
        self._strike_dump_idx += 1
        tag = f"strike_{self._strike_dump_idx:03d}_{reason}"
        out = Path("debug/strikes") / tag
        out.mkdir(parents=True, exist_ok=True)
        for i, (t_rel, frame, sig) in enumerate(self._strike_history):
            cv2.imwrite(str(out / f"{i:02d}_t{int(t_rel*1000):04d}ms.png"), frame)
            cv2.imwrite(
                str(out / f"{i:02d}_t{int(t_rel*1000):04d}ms_mask.png"),
                vision.annotate_red_mask(frame),
            )
        with open(out / "signals.txt", "w") as f:
            f.write(f"reason: {reason}\n")
            f.write("i  t_ms  delta  vdrop  edgeD  splash%  mdelta\n")
            for i, (t_rel, _, sig) in enumerate(self._strike_history):
                f.write(
                    f"{i:2d}  {int(t_rel*1000):5d}  "
                    f"{sig['delta']:5.1f}  {sig['vdrop']:5.1f}  "
                    f"{sig['edgeD']:+6.1f}  {sig['splash']*100:5.2f}  "
                    f"{sig['mdelta']:5.1f}\n"
                )
        print(f"[bot] strike dump -> {out}/ ({len(self._strike_history)} frames)")

    def _retrieve_action(self) -> None:
        """Fire the hook/retrieve action. Defaults to the cast action."""
        rp = self.cfg.retrieve_point if self.cfg.retrieve_point is not None else self.cfg.cast_point
        rk = self.cfg.retrieve_key if self.cfg.retrieve_key is not None else self.cfg.cast_key
        self.inp.focus_game(self.cfg.focus_window_title, debug=self.debug)
        if self.debug:
            target = f"click {rp}" if rp is not None else f"key '{rk}'"
            print(f"[bot] retrieve target={target}")
        if rp is not None:
            self.inp.click(int(rp[0]), int(rp[1]))
        else:
            self.inp.press(rk)

    # -- CASTING
    def _handle_casting(self) -> None:
        self._cast_attempts += 1
        if self.debug:
            target = (
                f"click {self.cfg.cast_point}"
                if self.cfg.cast_point is not None
                else f"key '{self.cfg.cast_key}'"
            )
            print(f"[bot] cast attempt #{self._cast_attempts}  target={target}")
        self.inp.focus_game(self.cfg.focus_window_title, debug=self.debug)
        self.inp.cast(self.cfg.cast_point, self.cfg.cast_key)
        self._cast_t = time.perf_counter()

        # Collect settle frames over ~CAST_SETTLE_S so we can (a) average them
        # into a stable baseline and (b) verify a bobber actually appeared.
        frames = []
        dt = CAST_SETTLE_S / CAST_SETTLE_FRAMES
        for _ in range(CAST_SETTLE_FRAMES):
            if not self._interruptible_sleep(dt):
                return
            try:
                frames.append(self.screen.grab(self.cfg.bobber_region))
            except Exception as e:
                if self.debug:
                    print(f"[bot] bobber grab failed during settle: {e}")

        if not frames:
            self._baseline_bobber = None
            self._enter(State.WAITING_SINK)
            return

        last = frames[-1]
        edge_var = vision.bobber_edge_variance(last)
        if self.debug:
            print(f"[bot] bobber edge var={edge_var:.1f} (min {self.cfg.bobber_edge_min:.1f})")

        if edge_var < self.cfg.bobber_edge_min:
            # No bobber landed in the calibrated region.
            if self._cast_attempts >= self.cfg.max_recast_attempts:
                print(f"[bot] {self._cast_attempts} casts with no bobber; pausing to recover")
                self._cast_attempts = 0
                self._enter(State.RECOVER)
            else:
                if self.debug:
                    print("[bot] no bobber in water; recasting")
                self._enter(State.CASTING)
            return

        # Bobber is present. Build a robust averaged baseline from the last few
        # settle frames so natural ripples don't look like a sink.
        tail = frames[-3:] if len(frames) >= 3 else frames
        self._baseline_bobber = np.mean(np.stack(tail).astype(np.float32), axis=0).astype(np.uint8)
        self._cast_attempts = 0
        self._enter(State.WAITING_SINK)

    # -- WAITING_SINK
    def _handle_waiting_sink(self) -> None:
        if self._elapsed() > SINK_TIMEOUT_S:
            if self.debug:
                print("[bot] sink timeout")
            self._enter(State.RECOVER)
            return

        if self._baseline_bobber is None:
            self._enter(State.RECOVER)
            return

        try:
            curr = self.screen.grab(self.cfg.bobber_region)
        except Exception as e:
            if self.debug:
                print(f"[bot] bobber grab failed: {e}")
            return

        delta = vision.frame_delta(self._baseline_bobber, curr)
        vdrop = vision.value_drop(self._baseline_bobber, curr)
        splash_frac = vision.bright_splash_fraction(curr)
        edge_delta = vision.edge_variance_delta(self._baseline_bobber, curr)
        # Frame-to-frame motion (vs previous captured frame, not baseline).
        # Catches the splash onset even if it was still rising vs baseline.
        if self._strike_history:
            prev_frame = self._strike_history[-1][1]
            mdelta = vision.frame_delta(prev_frame, curr)
        else:
            mdelta = 0.0

        # Maintain a rolling ~1.5 s buffer of frames + signals for strike dumps.
        now = time.perf_counter()
        sig = {
            "delta": delta, "vdrop": vdrop, "edgeD": edge_delta,
            "splash": splash_frac, "mdelta": mdelta,
        }
        self._strike_history.append((self._elapsed(), curr.copy(), sig))
        # Cap memory: keep ~40 entries (~1.5s at 0.035s tick).
        if len(self._strike_history) > 40:
            self._strike_history.pop(0)

        # Heartbeat log — lets the user see the bot is alive and what values it
        # is seeing, which is critical for tuning the strike thresholds.
        if self.debug and now - self._last_heartbeat >= HEARTBEAT_S:
            self._last_heartbeat = now
            print(
                f"[bot] waiting sink  t={self._elapsed():4.1f}s  "
                f"delta={delta:5.1f}/{self.cfg.sink_threshold:.1f}  "
                f"vdrop={vdrop:5.1f}  "
                f"splash={splash_frac*100:4.1f}%/{self.cfg.splash_min*100:.1f}%  "
                f"edgeD={edge_delta:6.1f}/{self.cfg.strike_edge_min:.1f}  "
                f"m={mdelta:4.1f}  hits={self._sink_hits}"
            )

        since_cast = now - self._cast_t
        if since_cast < CAST_LOCKOUT_S:
            return

        # Strong single-frame hit: fire immediately, no debounce. The cyan
        # splash is often only bright for 1-3 frames before dissipating, so
        # waiting for 2 consecutive hits on a weak signal misses real bites.
        strong_edge = edge_delta >= self.cfg.strike_edge_min * 2
        strong_splash = splash_frac >= self.cfg.splash_min * 2
        strong_motion = mdelta >= 15
        if strong_edge or strong_splash or strong_motion:
            if strong_splash:
                reason = "SPLASH"
            elif strong_edge:
                reason = "EDGE"
            else:
                reason = "MOTION"
            if self.debug:
                print(
                    f"[bot] STRIKE ({reason}, single-frame) "
                    f"edgeD={edge_delta:.1f} splash={splash_frac*100:.2f}% "
                    f"m={mdelta:.1f}"
                )
                self._dump_strike_history(reason)
            self._enter(State.RETRIEVING)
            return

        # Weaker signals still use a 2-frame debounce to avoid false triggers.
        weak_hit = (
            delta > self.cfg.sink_threshold
            or vdrop > 12
            or splash_frac >= self.cfg.splash_min
            or edge_delta >= self.cfg.strike_edge_min
            or mdelta >= 6
        )
        if weak_hit:
            self._sink_hits += 1
        else:
            self._sink_hits = max(0, self._sink_hits - 1)

        if self._sink_hits >= 2:
            if self.debug:
                print(
                    f"[bot] STRIKE (debounced) delta={delta:.1f} vdrop={vdrop:.1f} "
                    f"splash={splash_frac*100:.2f}% edgeD={edge_delta:.1f} "
                    f"m={mdelta:.1f}"
                )
                self._dump_strike_history("DEBOUNCED")
            self._enter(State.RETRIEVING)

    # -- RETRIEVING
    def _handle_retrieving(self) -> None:
        """Hook the fish. Most Roblox fishing games use the same button as
        cast, so by default this fires the cast action again. Override with
        retrieve_point / retrieve_key in config.json if the game differs.

        A reaction delay (retrieve_delay_ms) is applied *before* the click so
        the fish has time to fully commit to the bobber. Clicking too fast on
        the first dip frame causes the fish to slip the hook."""
        delay_s = max(0.0, self.cfg.retrieve_delay_ms / 1000.0)
        if self.debug:
            print(f"[bot] retrieving: wait {delay_s*1000:.0f} ms then hook")
        if not self._interruptible_sleep(delay_s):
            return
        self._retrieve_action()
        # Give the minigame UI a moment to appear.
        self._interruptible_sleep(RETRIEVE_WAIT_S)
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
        # Longer pause lets any in-progress animation or failed-minigame UI
        # clear before we try again. Interruptible so F6/Esc still works.
        self._interruptible_sleep(1.8)
        self._cast_attempts = 0
        self._enter(State.CASTING)

    # -- debug dump
    def _dump(self, tag: str, img: np.ndarray) -> None:
        if not self.debug:
            return
        ts = int(time.time() * 1000)
        cv2.imwrite(f"debug/{tag}_{ts}.png", img)
