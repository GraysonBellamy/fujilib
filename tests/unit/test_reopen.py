"""Reopening a session after a connection failure (design §7.6, §13.1 #35)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import pytest

from fujilib import (
    FujiConfigurationError,
    FujiConnectionError,
    FujiModbusTimeoutError,
    PollSourceAdapter,
    ReconnectPolicy,
    SessionState,
    open_device,
    record,
)
from fujilib.devices import factory
from fujilib.devices.profile import ZP_PROFILE
from fujilib.protocol.modbus.client import FailureKind
from fujilib.protocol.modbus.codec import encode_chars
from fujilib.registry.channels import ChannelId
from fujilib.testing import FaultKind
from fujilib.transport.serial import SerialTransport
from tests.facade import (
    FC04,
    IDENTIFY,
    POLL,
    PROBES,
    Cable,
    analyzer_on,
    bench,
    replugging,
    when,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from fujilib.devices.analyzer import Analyzer
    from fujilib.protocol.modbus.port import ModbusPort

pytestmark = pytest.mark.anyio


def state(anz: Analyzer) -> SessionState:
    return anz.session.state


async def test_reopen_after_the_cable_is_pulled_and_put_back() -> None:
    mock = bench()
    async with replugging(mock) as (anz, cable):
        mock.inject(FaultKind.CORRUPT_CRC)
        _ = await anz.poll()
        assert anz.session.recoverable_error_count == 1
        held = anz.session.counters  # a caller's reference keeps counting
        await cable.unplug()
        with pytest.raises(FujiConnectionError):
            _ = await anz.poll()
        assert state(anz) is SessionState.BROKEN
        mock.clear()
        with pytest.raises(FujiConnectionError, match="open the analyzer again"):
            _ = await anz.poll()
        assert mock.transactions() == []  # refused before any I/O

        cable.replug()
        info = await anz.reopen()
        assert state(anz) is SessionState.OPEN
        assert info.serial_number == "N8A0259T"
        assert [c.channel for c in anz.channels] == [ChannelId.CH1, ChannelId.CH2, ChannelId.CH3]
        assert mock.transactions() == IDENTIFY + PROBES
        mock.clear()
        frame = await anz.poll()
        assert frame.channel("CH3").gas.value == "o2"  # the asserted map is kept
        assert mock.transactions() == POLL
        assert anz.session.recoverable_error_count == 1  # counted across the reopen
        counters = anz.session.counters
        assert counters is held
        assert counters.recovered == 1
        assert counters.requests > len(IDENTIFY + PROBES + POLL)  # both ports' traffic
        assert counters.failures[FailureKind.CONNECTION] >= 1
        assert cable.plugs == 2


async def test_reopen_while_unplugged_leaves_the_session_broken() -> None:
    async with replugging(bench()) as (anz, cable):
        await cable.unplug()
        with pytest.raises(FujiConnectionError):
            _ = await anz.poll()
        with pytest.raises(FujiConnectionError, match="unplugged"):
            _ = await anz.reopen()
        assert state(anz) is SessionState.BROKEN
        last = anz.session.last_error
        assert last is not None
        assert last.command_name == "reopen"
        cable.replug()
        _ = await anz.reopen()
        _ = await anz.poll()


async def test_reopen_refuses_another_analyzer() -> None:
    mock = bench()
    async with replugging(mock) as (anz, cable):
        await cable.unplug()
        mock.set_register("identity.serial_number", encode_chars("X9Z99999", 8))
        cable.replug()
        with pytest.raises(FujiConfigurationError, match="not the analyzer that was open"):
            _ = await anz.reopen()
        assert state(anz) is SessionState.BROKEN


async def test_reopen_learns_the_identity_of_an_unidentified_session() -> None:
    async with replugging(bench(), identify=False) as (anz, _cable):
        assert anz.info is None
        info = await anz.reopen()
        assert info.model == "ZPA"


async def test_a_failed_identification_closes_the_new_port() -> None:
    mock = bench()
    async with replugging(mock, read_retries=0) as (anz, cable):
        mock.inject(FaultKind.DROP, times=None, when=when(FC04, 0x0425))
        with pytest.raises(FujiModbusTimeoutError):
            _ = await anz.reopen()
        assert state(anz) is SessionState.BROKEN
        assert cable.plugs == 2


async def test_a_caller_transport_cannot_be_reopened() -> None:
    async with analyzer_on(bench()) as (anz, _line):
        assert not anz.session.reopenable
        with pytest.raises(FujiConfigurationError, match="caller supplied"):
            _ = await anz.reopen()
        assert state(anz) is SessionState.OPEN


async def test_a_closed_analyzer_cannot_be_reopened() -> None:
    async with replugging(bench()) as (anz, _cable):
        await anz.close()
        with pytest.raises(FujiConfigurationError, match="was closed"):
            _ = await anz.reopen()


class SlowCable:
    """A reopener that waits to be let through, and remembers the ports it opened."""

    def __init__(self, cable: Cable) -> None:
        self.cable = cable
        self.entered = anyio.Event()
        self.release = anyio.Event()
        self.ports: list[ModbusPort] = []

    async def plug(self) -> ModbusPort:
        self.entered.set()
        await self.release.wait()
        port = await self.cable.plug()
        self.ports.append(port)
        return port


async def test_close_waits_for_a_reopen_in_progress() -> None:
    async with replugging(bench()) as (anz, cable):
        slow = SlowCable(cable)
        anz.session._reopener = slow.plug
        errors: list[BaseException] = []

        async def reopen() -> None:
            try:
                _ = await anz.reopen()
            except FujiConfigurationError as exc:
                errors.append(exc)

        async with anyio.create_task_group() as tg:
            _ = tg.start_soon(reopen)
            await slow.entered.wait()
            _ = tg.start_soon(anz.close)
            await anyio.wait_all_tasks_blocked()
            assert state(anz) is SessionState.CLOSED  # close() has begun and waits
            slow.release.set()
        assert len(errors) == 1
        assert "was closed" in str(errors[0])
        assert [p.closed for p in slow.ports] == [True]  # nothing left open


async def test_concurrent_reopens_open_the_port_once() -> None:
    async with replugging(bench()) as (anz, cable):
        slow = SlowCable(cable)
        anz.session._reopener = slow.plug
        async with anyio.create_task_group() as tg:
            _ = tg.start_soon(anz.reopen)
            await slow.entered.wait()
            _ = tg.start_soon(anz.reopen)
            await anyio.wait_all_tasks_blocked()
            slow.release.set()
        assert len(slow.ports) == 1
        assert state(anz) is SessionState.OPEN
        _ = await anz.poll()


async def test_a_closed_analyzer_ends_a_recording_with_a_reconnect_policy() -> None:
    async with replugging(bench()) as (anz, _cable):
        source = PollSourceAdapter("zpa", anz)
        errors: list[BaseException] = []

        async def body() -> None:
            async with record(source, rate_hz=50.0, reconnect=ReconnectPolicy((0.0,))) as rec:
                async for _batch in rec:
                    if rec.summary.samples_emitted == 2:
                        await anz.close()

        with anyio.fail_after(10):
            try:
                await body()
            except FujiConfigurationError as exc:
                errors.append(exc)
        assert len(errors) == 1
        assert "was closed" in str(errors[0])


class _SwapOnAcquire:
    """The old port's lock, which swaps the session to a new port once taken.

    As if ``reopen()`` finished while a call waited for the old lock.
    """

    def __init__(self, lock: anyio.Lock, swap: Callable[[], None]) -> None:
        self._lock = lock
        self._swap = swap

    def statistics(self) -> anyio.LockStatistics:
        return self._lock.statistics()

    async def __aenter__(self) -> None:
        await self._lock.acquire()
        self._swap()

    async def __aexit__(self, *exc: object) -> None:
        self._lock.release()


async def test_a_call_waiting_on_the_old_port_moves_to_the_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = bench()
    async with replugging(mock) as (anz, cable):
        session = anz.session
        old = session._port
        new = await cable.plug()

        def swap() -> None:
            session._port, session._client = new, new.client(1)

        with monkeypatch.context() as patch:
            patch.setattr(old, "_lock", _SwapOnAcquire(old.lock, swap))
            mock.clear()
            _ = await anz.poll()
        assert mock.transactions() == POLL
        await old.aclose()


async def test_open_device_by_name_is_reopenable(monkeypatch: pytest.MonkeyPatch) -> None:
    async with replugging(bench(), identify=False) as (_anz, cable):
        opened: list[str] = []

        async def fake_open(settings: object) -> object:
            opened.append(str(getattr(settings, "port", "")))
            return await cable.open_transport()

        monkeypatch.setattr(SerialTransport, "open", fake_open)
        anz = await open_device("COM99", timeout=0.25)
        try:
            assert anz.session.reopenable
            _ = await anz.reopen()
            assert opened == ["COM99", "COM99"]
        finally:
            await anz.close()


async def test_open_port_closes_the_transport_if_the_port_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with replugging(bench(), identify=False) as (_anz, cable):
        opened: list[object] = []

        async def fake_open(settings: object) -> object:
            del settings
            transport = await cable.open_transport()
            opened.append(transport)
            return transport

        def refuse(*args: object, **kwargs: object) -> ModbusPort:
            del args, kwargs
            msg = "no"
            raise FujiConfigurationError(msg)

        monkeypatch.setattr(SerialTransport, "open", fake_open)
        monkeypatch.setattr(factory, "ModbusPort", refuse)
        with pytest.raises(FujiConfigurationError):
            _ = await factory._open_port(ZP_PROFILE.default_serial, 0.25)
        assert len(opened) == 1
        assert not getattr(opened[0], "is_open", True)
