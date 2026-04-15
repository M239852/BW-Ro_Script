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
    hold_ms: int = 60
    sink_threshold: float = 18.0
    win_fill: float = 0.90
    fail_red: float = 0.50

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.cast_point is not None:
            d["cast_point"] = list(self.cast_point)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        cp = d.get("cast_point")
        if cp is not None:
            cp = tuple(cp)
        return cls(
            letter_region=Region(**d["letter_region"]),
            bobber_region=Region(**d["bobber_region"]),
            progress_region=Region(**d["progress_region"]),
            cast_point=cp,
            cast_key=d.get("cast_key", "1"),
            chest_key=d.get("chest_key", "e"),
            hold_ms=int(d.get("hold_ms", 60)),
            sink_threshold=float(d.get("sink_threshold", 18.0)),
            win_fill=float(d.get("win_fill", 0.90)),
            fail_red=float(d.get("fail_red", 0.50)),
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
