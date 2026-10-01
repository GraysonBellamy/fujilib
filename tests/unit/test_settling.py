"""The settling period after a reopen that follows a connection failure (design §8, §13.1 #81)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from fujilib import (
    FujiConnectionError,
    FujiValidationError,
    ReadingState,
    Sample,
    open_device,
    sample_to_row,
)
from fujilib.config import DEFAULTS
from fujilib.devices.profile import ZP_PROFILE
from fujilib.devices.session import Session
from fujilib.testing import mock_port
from tests.facade import Cable, bench, replugging

if TYPE_CHECKING:
    from fujilib.devices.analyzer import Analyzer
    from fujilib.devices.models import Frame

pytestmark = pytest.mark.anyio


async def pull_and_put_back(anz: Analyzer, cable: Cable) -> None:
    """A connection failure, seen by a poll, and the reopen after it."""
    await cable.unplug()
    with pytest.raises(FujiConnectionError):
        _ = await anz.poll()
    cable.replug()
    _ = await anz.reopen()


def states(frame: Frame) -> set[ReadingState]:
    return {r.state for r in frame.readings}


def settling_until(anz: Analyzer) -> datetime | None:
    return anz.session.settling_until


def test_the_default_period_covers_the_warm_up_seen_on_the_bench() -> None:
    assert DEFAULTS.settle_after_reopen_s == 90.0
    assert ReadingState.SETTLING.value == "settling"
    assert ReadingState.SETTLING.valid is False


async def test_readings_settle_after_a_reopen_that_follows_a_connection_failure() -> None:
    async with replugging(bench()) as (anz, cable):
        before = await anz.poll()
        assert states(before) == {ReadingState.OK}
        assert settling_until(anz) is None

        await pull_and_put_back(anz, cable)
        until = settling_until(anz)
        assert until is not None
        assert timedelta(seconds=80) < until - datetime.now(UTC) <= timedelta(seconds=90)

        frame = await anz.poll()
        assert states(frame) == {ReadingState.SETTLING}
        assert all(r.valid is False for r in frame.readings)
        # The values are kept as read; only the state says they are not to be trusted.
        assert [(r.channel, r.raw_value, r.value) for r in frame.readings] == [
            (r.channel, r.raw_value, r.value) for r in before.readings
        ]
        assert anz.last_frame is frame
        row = sample_to_row(Sample.from_frame(frame, device="zpa", address=1))
        assert (row["ch3_state"], row["ch3_valid"], row["ch3_value"]) == (
            "settling",
            False,
            before.channel("CH3").value,
        )
        assert states(await anz.poll()) == {ReadingState.SETTLING}  # and the polls after it


async def test_a_reading_with_a_reason_of_its_own_keeps_it() -> None:
    mock = bench()
    async with replugging(mock) as (anz, cable):
        await pull_and_put_back(anz, cable)
        mock.set_register("status.ch3.hold", 1)
        frame = await anz.poll()
        assert frame.channel("CH3").state is ReadingState.HOLD
        assert frame.channel("CH1").state is ReadingState.SETTLING


async def test_a_poll_without_the_status_stays_unknown() -> None:
    async with replugging(bench()) as (anz, cable):
        await pull_and_put_back(anz, cable)
        assert states(await anz.poll(detail=False)) == {ReadingState.UNKNOWN}


async def test_the_period_ends() -> None:
    async with replugging(bench(), settle_after_reopen_s=0.05) as (anz, cable):
        await pull_and_put_back(anz, cable)
        await anyio.sleep(0.25)
        assert settling_until(anz) is None
        assert states(await anz.poll()) == {ReadingState.OK}
        assert states(await anz.poll()) == {ReadingState.OK}


async def test_a_period_of_zero_marks_nothing() -> None:
    async with replugging(bench(), settle_after_reopen_s=0) as (anz, cable):
        await pull_and_put_back(anz, cable)
        assert settling_until(anz) is None
        assert states(await anz.poll()) == {ReadingState.OK}


async def test_a_reopen_of_a_working_session_marks_nothing() -> None:
    async with replugging(bench()) as (anz, _cable):
        _ = await anz.reopen()
        assert settling_until(anz) is None
        assert states(await anz.poll()) == {ReadingState.OK}


async def test_a_reopen_that_fails_first_still_starts_the_period() -> None:
    async with replugging(bench()) as (anz, cable):
        await cable.unplug()
        with pytest.raises(FujiConnectionError):
            _ = await anz.poll()
        with pytest.raises(FujiConnectionError):
            _ = await anz.reopen()
        assert settling_until(anz) is None
        cable.replug()
        _ = await anz.reopen()
        assert settling_until(anz) is not None
        assert states(await anz.poll()) == {ReadingState.SETTLING}


@pytest.mark.parametrize("seconds", [-1, -0.5, math.inf, math.nan, True, "90", None])
async def test_open_device_checks_the_period(seconds: Any) -> None:
    with pytest.raises(FujiValidationError, match="settle_after_reopen_s"):
        await open_device("COM_NOT_OPENED", settle_after_reopen_s=seconds)


@pytest.mark.parametrize("seconds", [-1.0, math.inf, math.nan])
async def test_a_session_checks_the_period(seconds: float) -> None:
    async with mock_port(bench()) as (port, _line):
        with pytest.raises(FujiValidationError, match="settle_after_reopen_s"):
            Session(port, address=1, profile=ZP_PROFILE, settle_after_reopen_s=seconds)
