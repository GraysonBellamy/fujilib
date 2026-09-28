"""``DeviceResult`` and ``PollSourceAdapter`` (unified API §E)."""

from __future__ import annotations

import pytest

from fujilib import DeviceResult, FujiModbusTimeoutError, PollSourceAdapter
from fujilib.testing import FaultKind
from tests.facade import POLL, analyzer_on, bench

pytestmark = pytest.mark.anyio


def test_device_result() -> None:
    error = FujiModbusTimeoutError("no reply")
    good = DeviceResult.success(3)
    bad = DeviceResult[int].failure(error)
    assert (good.ok, good.value, good.error) == (True, 3, None)
    assert (bad.ok, bad.value, bad.error) == (False, None, error)


async def test_a_poll_is_one_result_under_the_name() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        source = PollSourceAdapter("zpa", anz)
        results = await source.poll()
        assert source.name == "zpa"
        assert source.device is anz
    assert mock.transactions() == POLL
    assert list(results) == ["zpa"]
    frame = results["zpa"].value
    assert frame is not None
    assert len(frame.readings) == 3


async def test_a_failed_poll_is_a_failed_result() -> None:
    mock = bench()
    async with analyzer_on(mock, read_retries=0, request_timeout=0.05) as (anz, _line):
        mock.inject(FaultKind.DROP)
        results = await PollSourceAdapter("zpa", anz).poll(["zpa"])
    assert not results["zpa"].ok
    assert isinstance(results["zpa"].error, FujiModbusTimeoutError)


async def test_another_name_polls_nothing() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _line):
        results = await PollSourceAdapter("zpa", anz).poll(["other", "zpa2"])
    assert dict(results) == {}
    assert mock.transactions() == []
