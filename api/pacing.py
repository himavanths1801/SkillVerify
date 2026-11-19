"""The clock and the sleep of the question writer, and the pause between two calls to the same model.

``now`` and ``sleep`` go through module-level references so tests replace them: nothing in a test really waits.
"""
import threading
import time
from typing import Optional

_clock = time.monotonic
_sleep = time.sleep


def now() -> float:
    return _clock()


def sleep(seconds: float) -> None:
    _sleep(seconds)


class Pacer:
    """Keeps at least ``gap`` seconds between two calls to the SAME model (one pacer for the whole server, because the
    free per-minute limits belong to the key, not to a run). Calls to different models never wait for each other."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def wait(self, model: str, gap: float, deadline: Optional[float] = None) -> float:
        """Sleep until ``gap`` seconds have passed since the previous call to ``model``; returns the seconds waited.

        The pause is skipped when it would pass ``deadline`` (the run's time limit): the normal limit logic applies.
        The call's slot is reserved before sleeping, so two runs calling the same model queue behind each other.
        """
        with self._lock:
            current = now()
            last = self._last.get(model)
            pause = 0.0 if last is None else max(0.0, last + gap - current)
            if pause > 0 and deadline is not None and current + pause > deadline:
                pause = 0.0
            self._last[model] = current + pause
        if pause > 0:
            sleep(pause)
        return pause
