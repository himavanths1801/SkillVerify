"""ModelChain: which Gemini model the next run should use. In memory, thread-safe, one shared instance per server."""
import math
import os
import threading
import time
from typing import Callable, Optional, Sequence

from dotenv import load_dotenv

from api import settings

READY, COOLING_DOWN, UNAVAILABLE = "ready", "cooling_down", "unavailable"


class ModelChain:
    """An ordered list of models. A model that was rate limited rests for a while; the earliest ready model wins.

    ``models=None`` reads the chain from the environment the first time it is needed (so importing the app never
    reads the ``.env`` file).
    """

    def __init__(self, models: Optional[Sequence[str]] = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._models = tuple(models) if models is not None else None
        self._clock = clock
        self._lock = threading.Lock()
        self._cool_until: dict[str, float] = {}
        self._unavailable_until: dict[str, float] = {}
        self._limits_in_a_row: dict[str, int] = {}

    @property
    def models(self) -> tuple[str, ...]:
        with self._lock:
            return self._chain()

    def _chain(self) -> tuple[str, ...]:
        """The lock is held by the caller."""
        if self._models is None:
            load_dotenv()  # real environment variables win over the .env file
            self._models = settings.load_model_chain(os.environ)
        return self._models

    def _left(self, model: str, now: float) -> tuple[float, float]:
        return (max(0.0, self._cool_until.get(model, 0.0) - now),
                max(0.0, self._unavailable_until.get(model, 0.0) - now))

    def pick(self) -> Optional[str]:
        """The first model that is neither cooling down nor unavailable, or None."""
        with self._lock:
            now = self._clock()
            for model in self._chain():
                if self._left(model, now) == (0.0, 0.0):
                    return model
            return None

    def mark_rate_limited(self, model: str) -> None:
        with self._lock:
            repeated = self._limits_in_a_row.get(model, 0) >= 1
            seconds = settings.REPEATED_LIMIT_COOLDOWN_SECONDS if repeated else settings.RATE_LIMIT_COOLDOWN_SECONDS
            self._limits_in_a_row[model] = self._limits_in_a_row.get(model, 0) + 1
            self._cool_until[model] = self._clock() + seconds

    def mark_success(self, model: str) -> None:
        with self._lock:
            self._limits_in_a_row[model] = 0

    def mark_unavailable(self, model: str) -> None:
        with self._lock:
            self._unavailable_until[model] = self._clock() + settings.MODEL_UNAVAILABLE_SECONDS

    def snapshot(self) -> list[dict]:
        """``[{model, state, seconds_left}]`` in chain order. Model names only; nothing secret."""
        with self._lock:
            now = self._clock()
            rows = []
            for model in self._chain():
                cooling, unavailable = self._left(model, now)
                if unavailable > 0:
                    state, left = UNAVAILABLE, unavailable
                elif cooling > 0:
                    state, left = COOLING_DOWN, cooling
                else:
                    state, left = READY, 0.0
                rows.append({"model": model, "state": state, "seconds_left": math.ceil(left)})
            return rows
