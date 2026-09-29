"""A warning when setting writes come too fast (design §6.3).

No manual states how many writes the analyzer's memory tolerates, so fujilib
never writes periodically. :class:`WriteRateMonitor` catches a caller that
does, as ``alicatlib``'s EEPROM-wear guard does: when more than
``warn_per_minute`` setting writes fall within a rolling minute, it logs one
warning, and another only after the rate has dropped below the threshold and
risen again. It never refuses a write.
"""

from __future__ import annotations

import time
from collections import deque
from typing import TYPE_CHECKING, Final

from fujilib._logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["WriteRateMonitor"]

_LOG = get_logger("session")
_WINDOW_S: Final = 60.0


class WriteRateMonitor:
    """Counts one session's setting writes over a rolling minute."""

    def __init__(
        self, *, warn_per_minute: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Warn above ``warn_per_minute`` writes a minute; 0 or less never warns."""
        self.warn_per_minute = warn_per_minute
        self._clock = clock
        self._writes: deque[float] = deque()
        self._warned = False

    @property
    def count(self) -> int:
        """Writes in the last minute."""
        self._forget(self._clock())
        return len(self._writes)

    def record(self, name: str, *, where: str) -> None:
        """Count a write of setting ``name``; ``where`` names the port and station for the log."""
        if self.warn_per_minute <= 0:
            return
        now = self._clock()
        self._forget(now)
        self._writes.append(now)
        if len(self._writes) <= self.warn_per_minute:
            self._warned = False
        elif not self._warned:
            self._warned = True
            _LOG.warning(
                "%s: %d setting writes in the last minute (the latest %s), more than %d; "
                "the analyzer's write endurance is not documented, so avoid writing "
                "settings periodically",
                where,
                len(self._writes),
                name,
                self.warn_per_minute,
            )

    def _forget(self, now: float) -> None:
        while self._writes and self._writes[0] <= now - _WINDOW_S:
            self._writes.popleft()
