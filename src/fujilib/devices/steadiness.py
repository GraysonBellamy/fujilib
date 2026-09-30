"""Whether a calibration gas has settled at the inlet: the steadiness rule (design §13.1 #78).

A manual zero or span is right only if it is computed from the gas it names,
after the reading has stopped moving. The operator switches the valves and
names the gas; the analyzer's own guard is coarse (error 8: more than 100
counts of change within 60 s during the calibration, service manual p.33).
So before the key that calibrates, fujilib watches each channel's reading on
the wait step and calls it steady when, over a window of the last
:meth:`SteadinessRule.window_for` seconds:

- it has moved by no more than :attr:`SteadinessRule.band_percent_fs` of its
  range's full scale (highest less lowest reading), and
- its mean lies within :attr:`SteadinessRule.tolerance_percent_fs` of the gas
  the operator named, which catches the wrong cylinder or a valve left shut;
- and no two reads in it are more than :attr:`SteadinessRule.max_gap_s` apart,
  so that a verdict never rests on a couple of reads either side of a pause.

The window is the longer of :attr:`SteadinessRule.window_s` and
:attr:`SteadinessRule.response_factor` times the channel's response time.
On the bench, O2 settled within 0.01 vol% about 36 s after the gas changed,
with the response time at 15 s (protocol findings §14.4).

Every channel a calibration touches must be steady at once. The readings are
the Modbus concentrations, which are live only while output hold is off; a
remote calibration is refused while it is on (design §13.1 #86).

:class:`SteadinessJudge` is pure: it takes each read's readings with a
monotonic time and gives a :class:`SteadinessVerdict`. It does no I/O.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from itertools import pairwise
from types import MappingProxyType
from typing import TYPE_CHECKING

from fujilib.errors import FujiValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fujilib.registry.channels import ChannelId

__all__ = [
    "ChannelSteadiness",
    "SteadinessJudge",
    "SteadinessRule",
    "SteadinessTarget",
    "SteadinessVerdict",
]

# Reads exactly one window apart cover it, but on a millisecond clock (uvloop's) they
# are common, and their difference can come out a hair under the window.
_COVERAGE_SLACK_S = 1e-6


@dataclass(frozen=True, slots=True)
class SteadinessRule:
    """When a channel's reading counts as steady on the named gas (design §13.1 #78).

    The defaults are the design's starting values, to be tuned on the bench.
    """

    window_s: float = 30.0
    """The shortest window, in seconds."""
    response_factor: float = 2.0
    """The window is at least this many times the channel's response time."""
    band_percent_fs: float = 0.5
    """How far the reading may move over the window, in % of full scale."""
    tolerance_percent_fs: float = 10.0
    """How far the window's mean may lie from the named gas, in % of full scale."""
    timeout_s: float = 600.0
    """How long to wait for the gas to settle before giving up, in seconds."""
    max_gap_s: float = 5.0
    """The longest time between two reads in the window, in seconds."""

    def __post_init__(self) -> None:
        """Refuse a rule that could never, or always, be met.

        Raises:
            FujiValidationError: a value is not a positive finite number, or the
                response factor is negative.
        """
        for name in (
            "window_s",
            "band_percent_fs",
            "tolerance_percent_fs",
            "timeout_s",
            "max_gap_s",
        ):
            if not _number(getattr(self, name), minimum=0.0, inclusive=False):
                msg = f"{name} must be a positive number, got {getattr(self, name)!r}"
                raise FujiValidationError(msg)
        if not _number(self.response_factor, minimum=0.0, inclusive=True):
            msg = f"response_factor must be a number of 0 or more, got {self.response_factor!r}"
            raise FujiValidationError(msg)

    def window_for(self, response_time_s: float | None) -> float:
        """The window for a channel with ``response_time_s`` (``None``: not known)."""
        if response_time_s is None:
            return self.window_s
        return max(self.window_s, self.response_factor * response_time_s)


@dataclass(frozen=True, slots=True)
class SteadinessTarget:
    """One channel to watch: the gas named for it and its range."""

    gas: float
    """The named gas, in the channel's unit."""
    full_scale: float
    """The full scale of the range it is calibrated on, in the same unit."""
    unit: str
    response_time_s: float | None = None
    """The channel's response time, if known; it lengthens the window."""


@dataclass(frozen=True, slots=True)
class ChannelSteadiness:
    """One channel's part of a verdict."""

    channel: ChannelId
    steady: bool
    """Steady and on the named gas."""
    window_s: float
    covered_s: float
    """Seconds the reads kept cover, up to the window."""
    samples: int
    """Reads in the window."""
    gas: float
    unit: str
    last: float | None
    """The last reading."""
    mean: float | None
    spread_percent_fs: float | None
    """Highest less lowest reading over the window, in % of full scale."""
    offset_percent_fs: float | None
    """The window's mean less the named gas, in % of full scale."""
    reason: str
    """Why it is, or is not yet, steady."""


@dataclass(frozen=True, slots=True)
class SteadinessVerdict:
    """What the rule says after a read, for every channel watched."""

    steady: bool
    """Every channel is steady on its named gas."""
    channels: Mapping[ChannelId, ChannelSteadiness]
    elapsed_s: float
    """Seconds since the first read."""
    reads: int
    """Reads taken in so far."""

    @property
    def reasons(self) -> tuple[str, ...]:
        """Each channel's reason, prefixed with the channel."""
        return tuple(f"{c.value}: {s.reason}" for c, s in self.channels.items())


class SteadinessJudge:
    """Applies a :class:`SteadinessRule` to successive reads (see the module docstring)."""

    def __init__(self, rule: SteadinessRule, targets: Mapping[ChannelId, SteadinessTarget]) -> None:
        """Watch ``targets`` under ``rule``.

        Raises:
            FujiValidationError: no channel to watch, or a full scale that is not positive.
        """
        if not targets:
            msg = "a steadiness judge needs at least one channel"
            raise FujiValidationError(msg)
        for channel, target in targets.items():
            if not (math.isfinite(target.full_scale) and target.full_scale > 0):
                msg = f"{channel.value}: the full scale must be positive, got {target.full_scale}"
                raise FujiValidationError(msg)
        self._rule = rule
        self._targets = MappingProxyType(dict(targets))
        self._windows = {c: rule.window_for(t.response_time_s) for c, t in targets.items()}
        longest = max(self._windows.values())
        self._keep = longest
        self._samples: deque[tuple[float, Mapping[ChannelId, float | None]]] = deque()
        self._first: float | None = None
        self._reads = 0

    @property
    def rule(self) -> SteadinessRule:
        """The rule applied."""
        return self._rule

    @property
    def targets(self) -> Mapping[ChannelId, SteadinessTarget]:
        """The channels watched."""
        return self._targets

    def window(self, channel: ChannelId) -> float:
        """The window, in seconds, of ``channel``."""
        return self._windows[channel]

    def reset(self) -> None:
        """Forget every read, as when the gas is changed."""
        self._samples.clear()
        self._first = None
        self._reads = 0

    def feed(self, at_s: float, readings: Mapping[ChannelId, float | None]) -> SteadinessVerdict:
        """Take in one read at monotonic time ``at_s``; the verdict with it.

        A channel missing from ``readings`` counts as a reading that could not
        be decoded.

        Raises:
            FujiValidationError: ``at_s`` is earlier than the read before it.
        """
        if self._samples and at_s < self._samples[-1][0]:
            msg = f"reads must come in time order: {at_s} is before {self._samples[-1][0]}"
            raise FujiValidationError(msg)
        if self._first is None:
            self._first = at_s
        self._reads += 1
        self._samples.append((at_s, MappingProxyType(dict(readings))))
        # Keep one read at or before the start of the longest window, so it is covered.
        while len(self._samples) > 1 and self._samples[1][0] <= at_s - self._keep:
            self._samples.popleft()
        channels = {c: self._judge(c, at_s) for c in self._targets}
        return SteadinessVerdict(
            steady=all(s.steady for s in channels.values()),
            channels=MappingProxyType(channels),
            elapsed_s=at_s - self._first,
            reads=self._reads,
        )

    def _judge(self, channel: ChannelId, now: float) -> ChannelSteadiness:
        target = self._targets[channel]
        window = self._windows[channel]
        start = now - window
        # The reads in the window, and the last one before it, which covers its start.
        kept = [(t, r.get(channel)) for t, r in self._samples]
        inside = [(t, v) for t, v in kept if t >= start]
        before = [(t, v) for t, v in kept if t < start]
        span = [before[-1], *inside] if before else inside
        covered = now - span[0][0]
        covered = window if covered >= window - _COVERAGE_SLACK_S else covered
        values = [v for _, v in span]
        last = values[-1]
        rule = self._rule
        fs = target.full_scale

        def result(
            reason: str,
            *,
            steady: bool = False,
            mean: float | None = None,
            spread: float | None = None,
            offset: float | None = None,
        ) -> ChannelSteadiness:
            return ChannelSteadiness(
                channel=channel,
                steady=steady,
                window_s=window,
                covered_s=covered,
                samples=len(span),
                gas=target.gas,
                unit=target.unit,
                last=last,
                mean=mean,
                spread_percent_fs=spread,
                offset_percent_fs=offset,
                reason=reason,
            )

        if any(v is None for v in values):
            return result("a reading in the window could not be decoded")
        numbers = [v for v in values if v is not None]
        mean = sum(numbers) / len(numbers)
        spread = 100.0 * (max(numbers) - min(numbers)) / fs
        offset = 100.0 * (mean - target.gas) / fs
        steady = False
        if abs(offset) > rule.tolerance_percent_fs:
            reason = (
                f"reads {mean:g} {target.unit}, {offset:+.1f} %FS from the named gas "
                f"{target.gas:g} (at most ±{rule.tolerance_percent_fs:g} %FS)"
            )
        elif spread > rule.band_percent_fs:
            reason = (
                f"moved {spread:.2f} %FS over the last {covered:.1f} s "
                f"(at most {rule.band_percent_fs:g} %FS)"
            )
        elif (gap := _widest_gap(span)) > rule.max_gap_s:
            reason = f"reads {gap:.1f} s apart in the window (at most {rule.max_gap_s:g} s)"
        elif covered < window:
            reason = f"steady so far, for {covered:.1f} of {window:g} s"
        else:
            steady = True
            reason = (
                f"steady: moved {spread:.2f} %FS over {window:g} s, "
                f"{offset:+.1f} %FS from the named gas"
            )
        return result(reason, steady=steady, mean=mean, spread=spread, offset=offset)


def _widest_gap(span: list[tuple[float, float | None]]) -> float:
    """The longest time between two consecutive reads of ``span``; 0 for one read."""
    return max((b[0] - a[0] for a, b in pairwise(span)), default=0.0)


def _number(value: object, *, minimum: float, inclusive: bool) -> bool:
    """Whether ``value`` is a finite real number above ``minimum`` (or at it, if ``inclusive``)."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return False
    return value >= minimum if inclusive else value > minimum
