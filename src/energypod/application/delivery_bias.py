"""The delivery-bias estimator: evidence-only, no control path reads it.

DESIGN_POD_PARKING section 7 (the companion motivated by the filed +15-16 %
discharge overshoot, docs/DEFERRED_FINDINGS.md): a bounded per-unit deque of
``(authorized, measured)`` samples taken while the unit is ACTIVE, projecting
mean/max bias, sample count, and window.  Labeled evidence-only -- the
projection is a read for unit detail and MCP; nothing in the kernel, the
actor, or any adviser consumes it.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Final

# The window: the last 600 cycles OR 15 minutes, whichever the memory bound
# allows (the cap is the ~600-sample bound; at the 1.5 s control cadence 600
# cycles IS 15 minutes).
MAX_SAMPLES_PER_UNIT: Final[int] = 600
WINDOW_S: Final[float] = 900.0


def _bias_pct(authorized: float, measured: float) -> float | None:
    if authorized <= 0.0 or not math.isfinite(authorized) or not math.isfinite(measured):
        return None
    return (measured - authorized) / authorized * 100.0


class DeliveryBiasEstimator:
    """One bounded ``(authorized, measured)`` window per unit; passive."""

    def __init__(
        self, *, unit_ids: frozenset[str], max_samples: int = MAX_SAMPLES_PER_UNIT
    ) -> None:
        if not unit_ids:
            raise ValueError("unit_ids must not be empty")
        if type(max_samples) is not int or max_samples < 1:
            raise ValueError("max_samples must be a positive integer")
        self._max_samples = max_samples
        self._samples: dict[str, deque[tuple[float, float, float]]] = {
            unit_id: deque(maxlen=max_samples) for unit_id in unit_ids
        }

    def record(
        self, unit_id: str, *, authorized_w: float, measured_w: float | None, now_mono: float
    ) -> None:
        """Fold one cycle's pair while the unit is ACTIVE.

        The caller supplies the peeked authority and the poll's measured
        battery watts; an absent measurement or a non-positive authorization
        records nothing (there is no delivery to judge).
        """
        window = self._samples.get(unit_id)
        if window is None:
            raise LookupError(f"no unit with id {unit_id!r}")
        if measured_w is None or not math.isfinite(measured_w):
            return
        if not math.isfinite(authorized_w) or authorized_w <= 0.0:
            return
        window.append((float(authorized_w), float(measured_w), float(now_mono)))

    def projection(self, unit_id: str, *, now_mono: float) -> dict[str, float | int | None]:
        """The pinned evidence-only projection.

        ``{"mean_bias_pct", "max_bias_pct", "sample_count", "window_s"}`` --
        bias is (measured - authorized) / authorized in percent, signed the
        same way as the delivered power; the window is the span from the
        oldest retained sample to now.  An empty window carries null biases
        and a zero span -- never a fabricated zero bias.
        """
        window = self._samples.get(unit_id)
        if window is None:
            raise LookupError(f"no unit with id {unit_id!r}")
        biases = [
            bias
            for authorized, measured, _at in window
            if (bias := _bias_pct(authorized, measured)) is not None
        ]
        if not biases:
            return {
                "mean_bias_pct": None,
                "max_bias_pct": None,
                "sample_count": 0,
                "window_s": 0.0,
            }
        oldest = min(at for _a, _m, at in window)
        return {
            "mean_bias_pct": round(sum(biases) / len(biases), 3),
            "max_bias_pct": round(max(abs(bias) for bias in biases), 3),
            "sample_count": len(biases),
            "window_s": round(max(0.0, float(now_mono) - oldest), 3),
        }


__all__ = [
    "MAX_SAMPLES_PER_UNIT",
    "WINDOW_S",
    "DeliveryBiasEstimator",
]
