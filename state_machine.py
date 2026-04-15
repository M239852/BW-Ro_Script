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

# Rolling-window strike detector: fraction of the last bobber_lost_ms
# milliseconds of frames that must be "dropped" (score crater + position
# shift) before we fire a strike. 0.80 means 80% of the window has to show
# the bobber obscured/moved. Rain drops are sparse in time — even heavy
# rain rarely pushes this above 0.3 because most frames still see the
# bobber clearly pinned at home. A real underwater bite ramps it to 1.0
# within one window.
DROP_WINDOW_FRAC = 0.80
# Minimum samples we need in the window before we're willing to fire.
# Prevents an immediate strike on the very first post-lockout frame if
# the window happens to start with a transient dip.
DROP_WINDOW_MIN_SAMPLES = 8

# Post-cast settle: we used to wait a fixed 1.8 s for the bobber to land,
# but the initial cast-impact splash on the water is large and can still
# be dissipating after 2+ seconds. A fixed wait either cuts off the splash
# (polluted baseline → instant false strike) or wastes time. Instead we
# poll the region at SETTLE_SAMPLE_S intervals and wait until the frame-
# to-frame delta has been below SETTLE_QUIET_DELTA for SETTLE_QUIET_FRAMES
# consecutive samples — i.e. the water is actually stable. Capped at
# SETTLE_MAX_S so a stubborn splash can't hang the bot forever.
SETTLE_SAMPLE_S = 0.20
SETTLE_QUIET_DELTA = 4.0
SETTLE_QUIET_FRAMES = 4
SETTLE_MIN_S = 0.8
SETTLE_MAX_S = 6.0
# Heartbeat interval for WAITING_SINK debug log.
HEARTBEAT_S = 3.0
# After retrieve click, how long to poll the letter region waiting for
# a minigame prompt to appear. If no letter is recognized within this
# window, we assume the catch was not a minigame (Bridger Western only
# triggers the minigame occasionally) and recast.
MINIGAME_DETECT_S = 3.0
MINIGAME_DETECT_POLL_S = 0.1


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
        # Bobber tracking template (small grayscale crop from the center of
        # the baseline region). We find this each frame during WAITING_SINK
        # and watch its match score + position. Weather-resistant bite
        # signal because rain/ripples don't move the bobber itself.
        self._bobber_template: Optional[np.ndarray] = None
        self._bobber_home: tuple[int, int] = (0, 0)
        self._bobber_score_rest: float = 0.0
        # Rolling window of (timestamp, dropped_this_frame) flags over the
        # last bobber_lost_ms milliseconds. We fire a strike only when the
        # fraction of "dropped" frames inside the window exceeds
        # DROP_WINDOW_FRAC — i.e. the bobber has been obscured/moved for
        # *most* of the window, not just intermittently.
        #
        # Why a rolling fraction and not a consecutive-time timer? Rain
        # creates bursts of score noise and small position jitter that can
        # make a consecutive timer creep toward threshold — each individual
        # rain drop covers the bobber briefly, and if they come close enough
        # together, the timer never gets a clean reset frame between them.
        # A fraction-over-window ignores isolated drops: even in heavy rain,
        # most frames still see the bobber clearly pinned at home, so the
        # drop fraction stays well below DROP_WINDOW_FRAC. A real underwater
        # bite fills the window to ~100% within bobber_lost_ms.
        self._drop_flags: list[tuple[float, bool]] = []
        # kept for fallback (no template) path only
        self._drop_start_t: float = 0.0
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
        self._drop_start_t = 0.0
        self._drop_flags = []
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

        # Adaptive settle: poll the bobber region until the water stabilizes.
        # The cast-impact splash is large and can still be dissipating 2+ s
        # after cast; a fixed wait pollutes the baseline with splash pixels,
        # and that corrupted baseline then fires a false strike the instant
        # the splash clears (because "quiet water" now looks like a drop vs
        # "splash-covered water"). Wait until frame-to-frame delta is low
        # for SETTLE_QUIET_FRAMES consecutive samples.
        frames: list = []
        quiet_count = 0
        settle_start = time.perf_counter()
        if not self._interruptible_sleep(SETTLE_MIN_S):
            return
        while True:
            elapsed_settle = time.perf_counter() - settle_start
            if elapsed_settle >= SETTLE_MAX_S:
                if self.debug:
                    print(f"[bot] settle max {SETTLE_MAX_S:.1f}s reached "
                          f"without stabilizing; proceeding anyway")
                break
            try:
                f = self.screen.grab(self.cfg.bobber_region)
            except Exception as e:
                if self.debug:
                    print(f"[bot] bobber grab failed during settle: {e}")
                if not self._interruptible_sleep(SETTLE_SAMPLE_S):
                    return
                continue
            frames.append(f)
            if len(frames) >= 2:
                d = vision.frame_delta(frames[-2], f)
                if d < SETTLE_QUIET_DELTA:
                    quiet_count += 1
                else:
                    quiet_count = 0
                if self.debug and len(frames) % 3 == 0:
                    print(f"[bot] settling t={elapsed_settle:.1f}s "
                          f"delta={d:.1f} quiet={quiet_count}/{SETTLE_QUIET_FRAMES}")
                if quiet_count >= SETTLE_QUIET_FRAMES:
                    if self.debug:
                        print(f"[bot] water settled after {elapsed_settle:.1f}s")
                    break
            if not self._interruptible_sleep(SETTLE_SAMPLE_S):
                return

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
        # Build the bobber tracking template from the center of the baseline
        # and then measure its REST STATE against a sequence of fresh LIVE
        # frames — not against the baseline it was cut from. Matching the
        # template against its own source is a tautology: it will always
        # return ~1.0 regardless of whether the template actually tracks a
        # real bobber in moving water. The baseline-tautology rest score
        # then pretends "drop = 0" in the rest state when in reality the
        # live match score oscillates much lower than 1.0, and every live
        # frame in WAITING_SINK looks like a "drop" vs that phantom rest.
        #
        # Instead we:
        #   1. Extract the template from the baseline
        #   2. Capture N fresh live frames after a short pause
        #   3. Run matchTemplate on each live frame
        #   4. Use the *median* of those live scores as the real rest score
        #      and the median location as the real home position
        #   5. If the live rest score is too low OR the location is jittering
        #      by more than a pixel or two even without a bite, the template
        #      is unreliable — disable the template path entirely for this
        #      cast and fall through to the delta-based fallback detector.
        self._bobber_template = vision.extract_bobber_template(self._baseline_bobber)
        if self._bobber_template is not None:
            live_scores: list[float] = []
            live_locs: list[tuple[int, int]] = []
            for _ in range(5):
                if not self._interruptible_sleep(0.08):
                    return
                try:
                    live_frame = self.screen.grab(self.cfg.bobber_region)
                except Exception:
                    continue
                s, loc = vision.track_bobber(live_frame, self._bobber_template)
                live_scores.append(s)
                live_locs.append(loc)

            if len(live_scores) >= 3:
                ss = sorted(live_scores)
                live_rest = ss[len(ss) // 2]
                xs = sorted(l[0] for l in live_locs)
                ys = sorted(l[1] for l in live_locs)
                live_home = (xs[len(xs) // 2], ys[len(ys) // 2])
                max_jitter = max(
                    abs(l[0] - live_home[0]) + abs(l[1] - live_home[1])
                    for l in live_locs
                )
                self._bobber_score_rest = live_rest
                self._bobber_home = live_home
                if self.debug:
                    print(
                        f"[bot] bobber template {self._bobber_template.shape} "
                        f"live rest={live_rest:.2f} home={live_home} "
                        f"jitter={max_jitter}px (scores={['%.2f' % s for s in live_scores]})"
                    )
                # Unreliable-template guard: if the live rest score is low or
                # the location jitters more than the pos_shift threshold even
                # in the rest state, any legitimate frame-to-frame motion
                # will look like a strike. Disable the template path so the
                # delta-based fallback handles this cast instead.
                if (
                    live_rest < 0.85
                    or max_jitter >= self.cfg.bobber_pos_shift
                ):
                    if self.debug:
                        print(
                            f"[bot] template UNRELIABLE (rest={live_rest:.2f} "
                            f"jitter={max_jitter}) — disabling template path "
                            "for this cast, using fallback delta detector"
                        )
                    self._bobber_template = None
                    self._bobber_score_rest = 0.0
            else:
                if self.debug:
                    print("[bot] not enough live rest samples; disabling template path")
                self._bobber_template = None
                self._bobber_score_rest = 0.0
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
        # Weather-resistant bobber tracking: find the cast-time bobber
        # template in the current frame. When the fish bites, the bobber
        # dips / is obscured by splash / moves — all of which drop the
        # match score or shift the location. Rain and ripples don't move
        # the bobber, so this signal stays near 1.0 in bad weather.
        bscore, bloc = 0.0, self._bobber_home
        score_drop = 0.0
        pos_shift = 0
        if self._bobber_template is not None:
            bscore, bloc = vision.track_bobber(curr, self._bobber_template)
            score_drop = self._bobber_score_rest - bscore
            pos_shift = abs(bloc[0] - self._bobber_home[0]) + abs(bloc[1] - self._bobber_home[1])

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

        # Drop fraction over rolling window (computed below for strike
        # decision too). We prune + count here so the heartbeat shows the
        # live fraction the strike detector is actually using.
        window_start = now - (self.cfg.bobber_lost_ms / 1000.0)
        self._drop_flags = [(t, d) for (t, d) in self._drop_flags if t >= window_start]
        if self._drop_flags:
            drop_frac_live = sum(1 for (_, d) in self._drop_flags if d) / len(self._drop_flags)
        else:
            drop_frac_live = 0.0

        # Heartbeat log — lets the user see the bot is alive and what values it
        # is seeing, which is critical for tuning the strike thresholds.
        if self.debug and now - self._last_heartbeat >= HEARTBEAT_S:
            self._last_heartbeat = now
            print(
                f"[bot] waiting sink  t={self._elapsed():4.1f}s  "
                f"bobber={bscore:.2f} drop={score_drop:.2f}/{self.cfg.bobber_score_drop:.2f} "
                f"shift={pos_shift}/{self.cfg.bobber_pos_shift}  "
                f"wfrac={drop_frac_live*100:3.0f}%/{DROP_WINDOW_FRAC*100:.0f}% "
                f"(n={len(self._drop_flags)})"
            )

        since_cast = now - self._cast_t
        if since_cast < CAST_LOCKOUT_S:
            return

        # === Bobber tracking is the ONLY strike signal ===
        # The bobber itself only moves or vanishes when a fish hits. Rain,
        # wind ripples, daytime ambient splashes, particle effects, etc. all
        # happen around the bobber without displacing it, so the template
        # match stays pinned to the bobber's home location at high score.
        # Splash/edge/motion detectors were tried previously and produced
        # too many false triggers on ambient daytime splashes — they are
        # intentionally NOT used to fire strikes here.
        if self._bobber_template is None or self._bobber_score_rest <= 0.5:
            # Template couldn't be built at cast-settle; we have no reliable
            # signal. Fall back to the old delta/vdrop detection so the bot
            # isn't stuck forever — this path is a degraded last resort.
            fb_dropped = delta > self.cfg.sink_threshold or vdrop > 12
            if fb_dropped:
                if self._drop_start_t == 0.0:
                    self._drop_start_t = now
                if (now - self._drop_start_t) * 1000.0 >= self.cfg.bobber_lost_ms:
                    if self.debug:
                        print(
                            f"[bot] STRIKE (FALLBACK) delta={delta:.1f} "
                            f"vdrop={vdrop:.1f}  (no bobber template)"
                        )
                        self._dump_strike_history("FALLBACK")
                    self._enter(State.RETRIEVING)
            else:
                self._drop_start_t = 0.0
            return

        # Rolling-window strike detector. A frame is "dropped" only if BOTH
        # the template score has cratered AND the best-match location has
        # shifted away from home. Using AND (not OR) is the critical guard
        # against weather noise:
        #
        #   * Rain drop hits the water near the bobber -> might jitter the
        #     match score a little, might jitter the best-match pixel
        #     location a little, but almost never both at once on the same
        #     frame. AND rejects these.
        #   * Real fish bite pulls the bobber straight underwater -> the
        #     template is no longer findable at all, so cv2.matchTemplate
        #     falls back to the best random-noise match somewhere in the
        #     region. That gives low score AND shifted location at the
        #     same time, and it stays that way for seconds.
        #
        # We then require that a high fraction (DROP_WINDOW_FRAC) of the
        # last bobber_lost_ms milliseconds of frames were dropped. Rain
        # produces isolated dropped frames scattered among clean frames,
        # so the fraction stays well below threshold. A bite produces an
        # unbroken run of dropped frames, so the fraction hits ~1.0 within
        # one window.
        dropped_now = (
            score_drop >= self.cfg.bobber_score_drop
            and pos_shift >= self.cfg.bobber_pos_shift
        )
        self._drop_flags.append((now, dropped_now))

        drop_count = sum(1 for (_, d) in self._drop_flags if d)
        drop_frac = drop_count / len(self._drop_flags) if self._drop_flags else 0.0
        window_span_ms = (
            (self._drop_flags[-1][0] - self._drop_flags[0][0]) * 1000.0
            if len(self._drop_flags) >= 2 else 0.0
        )

        if (
            len(self._drop_flags) >= DROP_WINDOW_MIN_SAMPLES
            and window_span_ms >= self.cfg.bobber_lost_ms * 0.8
            and drop_frac >= DROP_WINDOW_FRAC
        ):
            reason = "BOBBER_LOST"
            if self.debug:
                print(
                    f"[bot] STRIKE ({reason}) score={bscore:.2f} "
                    f"drop={score_drop:.2f} shift={pos_shift} "
                    f"wfrac={drop_frac*100:.0f}% over {window_span_ms:.0f}ms "
                    f"(n={len(self._drop_flags)})"
                )
                self._dump_strike_history(reason)
            self._enter(State.RETRIEVING)

    # -- RETRIEVING
    def _handle_retrieving(self) -> None:
        """Hook the fish. Most Roblox fishing games use the same button as
        cast, so by default this fires the cast action again. Override with
        retrieve_point / retrieve_key in config.json if the game differs.

        A reaction delay (retrieve_delay_ms) is applied *before* the click so
        the fish has time to fully commit to the bobber. Clicking too fast on
        the first dip frame causes the fish to slip the hook.

        After the retrieve click we poll the letter region for up to
        MINIGAME_DETECT_S seconds. If a letter prompt shows up we enter
        MINIGAME; if it never does we assume this catch was not a minigame
        (Bridger Western only triggers the minigame occasionally) and
        recast directly.
        """
        delay_s = max(0.0, self.cfg.retrieve_delay_ms / 1000.0)
        if self.debug:
            print(f"[bot] retrieving: wait {delay_s*1000:.0f} ms then hook")
        if not self._interruptible_sleep(delay_s):
            return
        self._retrieve_action()

        deadline = time.perf_counter() + MINIGAME_DETECT_S
        while time.perf_counter() < deadline:
            if self._stop.is_set():
                return
            try:
                letter_img = self.screen.grab(self.cfg.letter_region)
            except Exception as e:
                if self.debug:
                    print(f"[bot] letter grab failed: {e}")
                time.sleep(MINIGAME_DETECT_POLL_S)
                continue
            letter, score, _ = vision.recognize_letter(letter_img, self.templates)
            if letter is not None:
                if self.debug:
                    print(
                        f"[bot] minigame prompt detected letter={letter} "
                        f"score={score:.2f} -> MINIGAME"
                    )
                self._enter(State.MINIGAME)
                return
            time.sleep(MINIGAME_DETECT_POLL_S)

        if self.debug:
            print(
                f"[bot] no minigame prompt in {MINIGAME_DETECT_S:.1f}s — "
                "plain catch, recasting"
            )
        self._enter(State.CASTING)

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
