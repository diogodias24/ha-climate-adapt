# SPDX-License-Identifier: Apache-2.0
"""The outside sample ring.

Pure logic, no Home Assistant imports: the screen every outside reading
passes (stale, reset, spike, out of order) before it reaches the daily means.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .const import (
    OUTSIDE_MEAN_WINDOW_HOURS,
    OUTSIDE_RING_MAX_SAMPLES,
    OUTSIDE_SAMPLE_HOLD_MINUTES,
    OUTSIDE_SPIKE_DELTA_C,
    OUTSIDE_SPIKE_WINDOW_MINUTES,
)

_HOLD = timedelta(minutes=OUTSIDE_SAMPLE_HOLD_MINUTES)
_WINDOW = timedelta(hours=OUTSIDE_MEAN_WINDOW_HOURS)
_SPIKE_WINDOW = timedelta(minutes=OUTSIDE_SPIKE_WINDOW_MINUTES)

# Why a reading did not become a sample.
REJECT_STALE = "stale"
REJECT_RESET = "reset"
REJECT_SPIKE = "spike"
REJECT_OLD = "out_of_order"


class OutsideTrend:
    """The outside sample ring."""

    def __init__(self) -> None:
        self.samples: list[tuple[datetime, float]] = []
        self.last_rejection: str | None = None

    def add(
        self,
        reported: datetime,
        temp: float,
        humidity: float | None,
        now: datetime,
    ) -> str | None:
        """Offer a reading; return why it was refused, or None if kept.

        Idempotent per report time: the same report offered again is refused
        as out of order without side effects, and a refused report is judged
        identically each time it is re-offered.
        """
        if self.samples and reported <= self.samples[-1][0]:
            return REJECT_OLD
        if now - reported > _HOLD:
            return self._refuse(REJECT_STALE)
        if temp == 0.0 and humidity is not None and humidity == 0.0:
            return self._refuse(REJECT_RESET)
        if self.samples:
            last_time, last_temp = self.samples[-1]
            if (
                reported - last_time < _SPIKE_WINDOW
                and abs(temp - last_temp) > OUTSIDE_SPIKE_DELTA_C
            ):
                return self._refuse(REJECT_SPIKE)
        self.samples.append((reported, temp))
        self._prune(reported)
        return None

    def _refuse(self, reason: str) -> str:
        self.last_rejection = reason
        return reason

    def _prune(self, newest: datetime) -> None:
        """Drop samples that can no longer reach the window; keep the newest."""
        cutoff = newest - _WINDOW - _HOLD
        kept = [s for s in self.samples if s[0] >= cutoff]
        self.samples = (kept or self.samples[-1:])[-OUTSIDE_RING_MAX_SAMPLES:]

    def to_store(self) -> dict[str, Any]:
        return {"outside_samples": [[s.isoformat(), v] for s, v in self.samples]}

    def load(self, stored: dict[str, Any]) -> None:
        """Restore from the store; anything malformed is dropped, not raised."""
        samples: list[tuple[datetime, float]] = []
        for item in stored.get("outside_samples") or []:
            try:
                stamp = datetime.fromisoformat(item[0])
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                samples.append((stamp, float(item[1])))
            except (TypeError, ValueError, IndexError, KeyError):
                continue
        samples.sort(key=lambda s: s[0])
        self.samples = samples[-OUTSIDE_RING_MAX_SAMPLES:]
