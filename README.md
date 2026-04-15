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
2. **Bobber region** — small box around where your bobber floats in the water.
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
| `sink_threshold` | mean pixel delta on bobber region to call "sink" | `18.0` |
| `bobber_edge_min` | Laplacian edge variance below which "no bobber visible" (empty water) | `8.0` |
| `max_recast_attempts` | consecutive cast misses before a longer RECOVER pause | `4` |
| `win_fill` | green fraction of progress bar meaning "win" | `0.90` |
| `fail_red` | red fraction meaning "fail" | `0.50` |
| `hold_ms` | keydown hold time in ms | `60` |
| `cast_key` / `chest_key` | key names passed to pydirectinput | `1` / `e` |
| `retrieve_point` / `retrieve_key` | optional — action to "hook" the fish when bobber sinks. Defaults to the cast action (same button casts and hooks in most Roblox fishing games). | `null` |

**Tuning with `--debug`.** In debug mode the bot prints:
- `bobber edge var=XX.X` right after each cast — if this is consistently below `bobber_edge_min` even when a bobber is visible, lower `bobber_edge_min` to match.
- A heartbeat every ~3s during `WAITING_SINK` showing the live `delta`/`vdrop` values. If you see a real sink happen but `delta` never crosses `sink_threshold`, lower it. If it false-triggers, raise it.

If the bot casts but nothing happens, the issue is almost always `bobber_edge_min` or `sink_threshold`; watch one full cycle with `--debug` and adjust.

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
