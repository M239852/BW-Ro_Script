# Bridger Western — Fishing Minigame Solver

Auto-solves the Roblox **Bridger Western** fishing minigame: casts the rod, waits for the bobber to sink, presses the A–Z letter that appears each round, collects the chest with **E**, and recasts. Full loop — you just start it and walk away.

**Windows only.** Uses `pydirectinput` (DirectInput scan codes) because Roblox filters most other input libraries.

---

## Install

1. Install Python 3.10+.
2. From this folder:
   ```
   pip install -r requirements.txt
   ```

## First run — calibrate

```
python main.py --calibrate
```

A translucent overlay appears. You'll be asked, in order, to drag a box over:

1. **Letter region** — tight rectangle where the minigame letter (A–Z) shows up.
2. **Bobber region** — box around where your bobber floats. **Make this generously wide**: the strike detector looks for the red/orange ripple trail the approaching fish paints across the water, so the region should cover a few bobber-widths of water *around* the bobber (not just the bobber itself). If the region is too tight the trail never enters it and the bite is only detected once the bobber goes fully under — which is usually too late.
3. **Progress region** — the minigame progress / meter bar.
4. **Cast point** — click where to click-cast (or type `n` to use a cast key instead).

Then you're prompted for the cast key (default `1`) and chest collect key (default `e`). Settings are saved to `config.json`. Re-run with `--calibrate` any time to redo this.

## Build the letter templates (one-time)

The recognizer uses OpenCV template matching, so you need a `templates/A.png` … `templates/Z.png` file set. There are two ways to build them. **Do option A first — it's one command — and only fall back to option B for letters that don't match at runtime.**

### A. Generate from a font (fast — one command)

Bridger Western's minigame letters render in a Roman serif capital (looks like **Trajan Pro** or similar). If you already have Trajan Pro or Cinzel on your system the generator will find them automatically; otherwise it falls back to Times New Roman Bold, which is close enough in most cases:

```
python generate_templates.py
```

Or point it at a specific font:

```
python generate_templates.py --font "C:/Windows/Fonts/trajanpro-bold.otf" --overwrite
python generate_templates.py --font "C:/Windows/Fonts/timesbd.ttf" --overwrite
```

The generator renders each letter at high resolution, runs it through the **exact** preprocessing pipeline the recognizer uses, and writes 48×48 binary PNGs to `templates/`. You can re-render specific letters later:

```
python generate_templates.py --only GT --overwrite
```

### B. Capture from the live game (fallback for mismatched letters)

If any generated letter scores consistently below 0.85 at runtime (visible in `--debug`), replace just that letter with a real in-game crop:

```
python main.py --capture-templates
```

Cast your rod, play normally; when a new prompt appears the script crops it, you type the real letter, and it saves `templates/<LETTER>.png`. You can stop any time with `q` — partial coverage is fine because generated templates fill the rest.

> **Tight calibration is critical.** The in-game letter sits on a dark circular button, surrounded by bright scenery (ground, chest). When you calibrate `letter_region`, **drag the box tightly around the dark circle only** — don't include the bright surroundings. If the captured region has more bright pixels than dark ones, Otsu thresholding will invert and pick the wrong contour.

## Run the bot

Dry-run first (nothing is actually pressed, actions are logged):

```
python main.py --dry-run --debug
```

You should see output like:
```
[bot] CASTING -> WAITING_SINK
[bot] SINK delta=22.4 vdrop=15.8
[bot] MINIGAME
[bot] letter=R score=0.93 -> press
[DRY] press r
[bot] WIN green=0.94
[bot] collecting chest
[DRY] press e
```

If that looks right, run for real:

```
python main.py
```

**Stop at any time with F6 or Esc.**

Add `--debug` for verbose logs.

## Tuning

Everything is in `config.json`:

| key | meaning | default |
|---|---|---|
| `bobber_score_drop` | Template match score drop (vs cast-time rest score) that counts as "bobber lost" this frame. Raise if ambient effects partially obscure the bobber, lower if real bites don't drop the score enough. | `0.35` |
| `bobber_pos_shift` | Manhattan pixel distance from the bobber's cast-time home at which this frame counts as "bobber shifted". | `6` |
| `bobber_lost_ms` | **The most important knob.** The score drop or position shift must be *sustained* for this many milliseconds before a strike fires. Ambient daytime splashes briefly cover the bobber for ~150-300 ms and then clear — they never reach this threshold. A real bite pulls the bobber underwater for multiple seconds so it blows through it easily. Raise if ambient splashes still trigger; lower if real bites are being rejected as transient. | `400` |
| `sink_threshold` | mean pixel delta on bobber region for the fallback detector used only when the bobber template cannot be built at cast time. | `18.0` |
| `splash_min` | retained in config for back-compat but **no longer used** as a strike signal — ambient daytime splashes produced too many false triggers. The bobber tracker catches real bites in all weather. | `0.02` |
| `strike_edge_min` | retained in config for back-compat but **no longer used** as a strike signal. | `25.0` |
| `red_trail_min` | (legacy) fraction of red/orange-only pixels. Unused in detection unless your game actually uses a red trail — kept for backwards compatibility. | `0.005` |
| `bobber_edge_min` | Laplacian edge variance below which "no bobber visible" (empty water) | `8.0` |
| `max_recast_attempts` | consecutive cast misses before a longer RECOVER pause | `4` |
| `win_fill` | green fraction of progress bar meaning "win" | `0.90` |
| `fail_red` | red fraction meaning "fail" | `0.50` |
| `hold_ms` | keydown hold time in ms | `60` |
| `cast_key` / `chest_key` | key names passed to pydirectinput | `1` / `e` |
| `retrieve_point` / `retrieve_key` | optional — action to "hook" the fish when bobber sinks. Defaults to the cast action (same button casts and hooks in most Roblox fishing games). | `null` |
| `retrieve_delay_ms` | wait after detecting a strike before firing the retrieve click. The bobber-lost detector already waits `bobber_lost_ms` for a sustained drop so by the time a strike fires we're already well into the bite window. Default `0`; raise toward 100–200 only if the game needs an extra beat before the click registers. | `0` |
| `click_hold_ms` | mouse-button hold time for cast/retrieve clicks. Roblox drops very short clicks — 40–80 ms is reliable. | `50` |
| `focus_window_title` | substring of the game window title to force-focus before each cast/retrieve so mis-focused clicks don't vanish. Set to `""` to disable. | `"Roblox"` |

## Troubleshooting input

If the bot transitions through `CASTING -> WAITING_SINK` but you don't actually see the rod cast in-game (or the retrieve click never fires when the bobber sinks), run the diagnostic mode:

```
python main.py --test-input
```

It fires cast → wait 3 s → retrieve three times with no sink detection. Watch Roblox and console output. Common failure modes:

- **Bot tabs *out* of Roblox after the first cast.** When you run `python main.py` or `--test-input` the bot now prints `Switch to Roblox NOW. Capturing focused window in 4s...` and snapshots whatever window is in the foreground when the countdown ends. That specific HWND is pinned for the whole session, so title matching is bypassed entirely. If you see `WARN captured window title does not contain 'Roblox'` you alt-tabbed to the wrong window — stop (F6/Esc) and re-run.
- **`WARN click stole focus from game (fg='...')`** — the click itself landed outside the Roblox window, so Windows switched focus to whatever was there. This is a **calibration** problem, not a focus problem. Re-run `python main.py --calibrate` and make sure `cast_point` / `retrieve_point` are clearly inside the Roblox viewport.
- **`WARN no window matching title 'Roblox'`** — your Roblox client isn't titled "Roblox" (uncommon, but some web-player builds are different). Use `--list-windows` to find the right title and set it in `config.json`.
- **`WARN cursor at (X,Y) not (A,B)`** — the OS isn't moving the cursor to where you asked. Usually means Windows DPI scaling isn't matching what `mss` captured during calibration. Re-run `python main.py --calibrate` after making sure Roblox and Windows are both at 100% display scaling.
- **Cursor moves to the right place but nothing happens in-game** — the click is being dropped. Raise `click_hold_ms` to 80 or 100.
- **Cursor doesn't move at all and no warnings** — focus_window_title is disabled/unmatched and another window is intercepting. Make sure Roblox is actually visible and un-minimized.

**Tuning with `--debug`.** In debug mode the bot prints:
- `bobber edge var=XX.X` right after each cast — if this is consistently below `bobber_edge_min` even when a bobber is visible, lower `bobber_edge_min` to match.
- A heartbeat every ~3s during `WAITING_SINK` showing the live strike signals:
  ```
  [bot] waiting sink  t= 5.2s  delta= 12.4/18.0  vdrop= 4.1  red= 0.8%/0.5%  edgeD=  41.2/25.0  hits=1
  ```
  - `delta` — mean pixel change vs baseline (old sink signal).
  - `vdrop` — HSV Value drop (old sink signal).
  - `red` — fraction of red/orange trail pixels in the region vs `red_trail_min`. **This is the primary bite signal** — the approaching fish's ripple trail.
  - `edgeD` — Laplacian-variance increase over baseline vs `strike_edge_min`. Catches the splash/smoke ring when the fish strikes.
- A `STRIKE` line tells you which signal fired. If you watched a real bite happen but none of the four values crossed its threshold, lower the one that got closest. If the bot false-triggers in calm water, raise the one that crossed.

If strikes are being missed: first check that `bobber_region` is wide enough to contain the approaching trail (see the calibration note above), then lower `red_trail_min` to e.g. `0.003` or `strike_edge_min` to `15.0`.

### Strike replay dumps (live bot, `--debug`)

When running `python main.py --debug`, **every** strike fire dumps the last ~1.5 s of bobber-region frames to `debug/strikes/strike_NNN_<reason>/`. Each dump contains:

- `NN_tXXXXms.png` — the raw captured frame at time X ms before fire
- `NN_tXXXXms_mask.png` — same frame with the bright-splash mask painted magenta
- `signals.txt` — tabular log of every signal at every frame in the window

This is the definitive ground-truth for "is the bot firing at the right time?". Flip through the frames in timestamp order and find which one has the splash. If the fire happened *before* that frame → lower `retrieve_delay_ms` or the strong thresholds (firing on noise, not splash). If *after* → your splash is too transient and we need to widen the detection window or pre-fire on motion.

### When strikes are still being missed — use `--watch-bobber`

If the heartbeat numbers don't tell you enough, run:

```
python main.py --watch-bobber
```

This takes no action — no clicks, no state machine — and instead continuously captures `bobber_region`, prints every signal (`delta`, `vdrop`, `edgeVar`, `edgeD`, `red%`, frame-to-frame `mdelta`), and saves any **high-signal frame** to `debug/watch/` as two PNGs per dump:

- `NNNN_curr.png` — the raw captured bobber region
- `NNNN_mask.png` — same frame with every red/orange trail pixel painted **bright magenta**, so you can verify the detector is highlighting the actual ripple trail and not just noise

Workflow:

1. Run `python main.py --watch-bobber`, leave it running
2. Cast manually in-game
3. Wait for a real bite, watch it happen
4. Ctrl+C to stop. The final "Peak values observed" block tells you exactly which signal got how close to threshold during your session
5. Lower the threshold of whichever signal's peak *got closest to* but *did not exceed* its target
6. Open `debug/watch/*_mask.png` — if your real ripple trail is **not** turning magenta in any frame, the HSV mask bounds are off for your game's trail color (open the underlying `_curr.png`, pick a trail-dot pixel in any image editor, check its HSV, and widen the `warm` mask in `vision.red_trail_fraction`)

This is the fastest way to close the loop when nothing else is working — you see *exactly* what the bot sees.

## File layout

```
main.py               # entry point, CLI, hotkeys, DPI aware
config.py             # Config dataclass + config.json IO
calibrate.py          # tkinter overlay for drag-select calibration
vision.py             # screen capture + letter/sink/progress detection
input_driver.py       # pydirectinput wrapper, dry-run aware
state_machine.py      # FishingBot: CASTING -> WAITING_SINK -> RETRIEVING -> MINIGAME -> CHEST/RECOVER
generate_templates.py # render A-Z from a TTF/OTF font through the recognizer pipeline
templates/            # A.png..Z.png (generated locally — not in git)
config.json           # calibration output (created on first run)
```

## Offline self-test

```
python vision.py
```

Runs a small set of `assert`s on green/red/blank bars, bobber frame delta, and letter preprocessing. No game needed.

## Caveats

- **Single primary monitor** assumed.
- `keyboard` hotkeys on Windows may need admin for some setups; F6 generally works without it.
- This is automation for a *minigame solver*; use at your own risk and respect the game's rules.
