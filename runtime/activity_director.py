"""Small local activity director inspired by ds-local-pet.

This module deliberately only schedules clips that exist in the DSH manifest.
It keeps idle micro-actions separate from Codex-driven states, applies cooldowns,
and avoids repeating the same activity back-to-back.
"""

from __future__ import annotations

import random
import time
from collections import deque


class ActivityDirector:
    """Choose low-interruption local actions for an otherwise idle companion."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()
        self.activity_level = "normal"
        self.movement_mode = "follow"
        self._next_at = 0.0
        self._recent: deque[str] = deque(maxlen=3)
        self._cooldown_until: dict[str, float] = {}

    def set_level(self, value: object) -> None:
        value = str(value or "normal").lower()
        self.activity_level = value if value in {"quiet", "normal", "lively"} else "normal"

    def set_mode(self, value: object) -> None:
        value = str(value or "follow").lower()
        self.movement_mode = {
            "still": "quiet",
            "wander": "lively",
        }.get(value, value if value in {"follow", "quiet", "lively"} else "follow")

    def schedule(self, now: float | None = None, *, initial: bool = False) -> None:
        now = time.monotonic() if now is None else now
        ranges = {
            "quiet": (18.0, 34.0),
            "normal": (10.0, 22.0),
            "lively": (5.0, 13.0),
        }
        if self.movement_mode == "quiet":
            ranges["quiet"] = (18.0, 30.0)
        elif self.movement_mode == "lively":
            # Lively mode is intentionally predictable: roughly one authored
            # activity every ten seconds, with a small amount of variation.
            ranges["lively"] = (9.0, 11.0)
        lower, upper = ranges[self.activity_level]
        if initial:
            lower, upper = (8.0, 16.0)
        self._next_at = now + self.rng.uniform(lower, upper)

    def choose_idle_clip(self, now: float | None = None) -> str | None:
        """Return one available manifest clip, or None when idle activity is due."""
        now = time.monotonic() if now is None else now
        if self._next_at <= 0:
            self.schedule(now, initial=True)
            return None
        if now < self._next_at:
            return None
        # These are all shipped by the DSH manifest.  Keep micro-actions and
        # full activities in the same low-frequency scheduler so an idle pet
        # feels alive without interrupting task states.
        # Prefer the authored multi-frame actions from ds-local-pet. The
        # previous list mostly reached clips whose generated frames were
        # visually identical, so the pet appeared frozen even though the
        # timer advanced correctly.
        if self.movement_mode == "quiet":
            candidates = ["sleep", "blink", "glance"]
        elif self.movement_mode == "lively":
            # Lively mode should visibly do more than pace. Give the authored
            # full-body activities priority; blink/glance remain occasional
            # fillers between them instead of winning most random picks.
            candidates = [
                "happy", "happy", "talk", "talk", "sweep", "sweep",
                "eating", "eating", "glance", "blink",
            ]
        elif self.movement_mode == "follow":
            # Follow mode spends its non-walking time in quiet observation;
            # full activities belong to lively mode and should not fire while
            # the cursor is merely resting near the pet.
            candidates = ["blink", "glance", "sleep"]
        else:
            candidates = ["blink", "glance", "happy", "talk", "sweep", "sleep", "eating"]
        available = [clip for clip in candidates if now >= self._cooldown_until.get(clip, 0.0)]
        if self._recent:
            available = [clip for clip in available if clip != self._recent[-1]] or available
        if not available:
            self.schedule(now)
            return None
        clip = self.rng.choice(available)
        self._recent.append(clip)
        self._cooldown_until[clip] = now + (26.0 if clip in {"happy", "sweep", "talk", "sleep", "eating"} else 12.0)
        self.schedule(now)
        return clip
