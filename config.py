"""Config dataclass + JSON load/save for the Bridger Western fishing bot."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Tuple

CONFIG_PATH = Path(__file__).with_name("config.json")


@dataclass
class Region:
    x: int
    y: int
    w: int
    h: int

    def as_mss(self) -> dict:
        return {"left": self.x, "top": self.y, "width": self.w, "height": self.h}


@dataclass
class Config:
    letter_region: Region
    bobber_region: Region
    progress_region: Region
    cast_point: Optional[Tuple[int, int]] = None
    cast_key: str = "1"
    chest_key: str = "e"
    # Retrieve ("hook the fish") action. If None, falls back to the cast action,
    # which is correct for most Roblox fishing games (same button casts and hooks).
    retrieve_point: Optional[Tuple[int, int]] = None
    retrieve_key: Optional[str] = None
    hold_ms: int = 60
    sink_threshold: float = 18.0
    win_fill: float = 0.90
    fail_red: float = 0.50
    # Bobber-presence check: Laplacian edge variance below this means "no bobber
    # visible in the region" (empty water). Recast instead of waiting for a sink.
    bobber_edge_min: float = 8.0
    # Max consecutive cast attempts that land no bobber before a longer RECOVER pause.
    max_recast_attempts: int = 4
    # Reaction delay between detecting a strike and firing the retrieve click.
    # With splash-based detection we're already at peak bite time — the old
    # 350 ms default was tuned for the old dip-based detection and now makes
    # the bot click *after* the fish has left. Default 60 ms; raise toward
    # 150-250 only if fish actually slip the hook in your game.
    retrieve_delay_ms: int = 60
    # Explicit mouse-button hold time for click(). A ~1 ms click (the default
    # for pydirectinput.click()) is sometimes dropped by Roblox; 40-80 ms is
    # reliable without feeling sluggish.
    click_hold_ms: int = 50
    # Window title substring to force-focus before sending cast/retrieve
    # clicks. Set to "" or null to disable.
    focus_window_title: Optional[str] = "Roblox"
    # Fraction of pixels in bobber_region that must match the red/orange
    # fish-approach trail color before we call a strike. Kept for backwards
    # compatibility; splash_min is the primary signal now.
    red_trail_min: float = 0.005
    # Fraction of pixels in bobber_region that are bright + saturated (any
    # hue) against the dark water — the main bite signal. The cyan/teal
    # splash in Bridger Western lights up this mask instantly. A single
    # frame crossing 2x this value fires a strike with no debounce.
    splash_min: float = 0.02
    # Laplacian-variance increase over the cast-time baseline at which we
    # call a strike (splash / smoke ring adds structural edges that weren't
    # there when the water was quiet). A single frame crossing 2x this fires
    # a strike with no debounce.
    strike_edge_min: float = 25.0
    # Weather-resistant bobber tracking. At cast-settle a small template is
    # cut from the center of the bobber region; each frame during WAITING_SINK
    # we rerun matchTemplate and compare. A bite drops the match score
    # sharply (bobber dipped / obscured) or shifts its position.
    #   bobber_score_drop: drop from rest score at which we call a strike.
    #     Rest is typically ~0.95+, bite typically drops to <0.5. A drop of
    #     0.35 is conservative — raise if false-triggers on weather, lower
    #     if real bites are missed.
    bobber_score_drop: float = 0.35
    #   bobber_pos_shift: Manhattan pixel distance from the cast-time bobber
    #     home position at which we call a strike. Rain doesn't move the
    #     bobber; a bite does. 6 px is usually enough.
    bobber_pos_shift: int = 6

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.cast_point is not None:
            d["cast_point"] = list(self.cast_point)
        if self.retrieve_point is not None:
            d["retrieve_point"] = list(self.retrieve_point)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        cp = d.get("cast_point")
        if cp is not None:
            cp = tuple(cp)
        rp = d.get("retrieve_point")
        if rp is not None:
            rp = tuple(rp)
        return cls(
            letter_region=Region(**d["letter_region"]),
            bobber_region=Region(**d["bobber_region"]),
            progress_region=Region(**d["progress_region"]),
            cast_point=cp,
            cast_key=d.get("cast_key", "1"),
            chest_key=d.get("chest_key", "e"),
            retrieve_point=rp,
            retrieve_key=d.get("retrieve_key"),
            hold_ms=int(d.get("hold_ms", 60)),
            sink_threshold=float(d.get("sink_threshold", 18.0)),
            win_fill=float(d.get("win_fill", 0.90)),
            fail_red=float(d.get("fail_red", 0.50)),
            bobber_edge_min=float(d.get("bobber_edge_min", 8.0)),
            max_recast_attempts=int(d.get("max_recast_attempts", 4)),
            retrieve_delay_ms=int(d.get("retrieve_delay_ms", 350)),
            click_hold_ms=int(d.get("click_hold_ms", 50)),
            focus_window_title=d.get("focus_window_title", "Roblox"),
            red_trail_min=float(d.get("red_trail_min", 0.005)),
            splash_min=float(d.get("splash_min", 0.02)),
            strike_edge_min=float(d.get("strike_edge_min", 25.0)),
            bobber_score_drop=float(d.get("bobber_score_drop", 0.35)),
            bobber_pos_shift=int(d.get("bobber_pos_shift", 6)),
        )


def exists() -> bool:
    return CONFIG_PATH.exists()


def load() -> Optional[Config]:
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text())
        return Config.from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"[config] failed to load {CONFIG_PATH}: {e}")
        return None


def save(cfg: Config) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg.to_dict(), indent=2))
    print(f"[config] saved {CONFIG_PATH}")
