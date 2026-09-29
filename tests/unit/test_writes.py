"""Setting writes through the facade, against the simulated bench analyzer (design §6.1-§6.4).

Each write is checked for the exact transactions it sends; every refusal for
sending nothing, or no write; and every uncertain outcome for what the
read-back after it found.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import anyio
import anyserial
import pytest

from fujilib.devices._write_rate import WriteRateMonitor
from fujilib.devices.capability import Capability, SafetyTier
from fujilib.devices.decode import decode_register
from fujilib.devices.factory import open_device
from fujilib.devices.models import AnalyzerStatus, ChannelStatus
from fujilib.devices.reads import StatusRead
from fujilib.devices.session import SessionState
from fujilib.devices.writes import (
    WriteResult,
    WriteState,
    busy_reasons,
    describe,
    outcome_error,
)
from fujilib.errors import (
    FujiAnalyzerStateError,
    FujiConfirmationRequiredError,
    FujiConnectionError,
    FujiModbusIllegalDataValueError,
    FujiTimeoutError,
    FujiValidationError,
    FujiVerificationError,
    FujiWriteOutcomeUnknownError,
)
from fujilib.protocol.modbus.read_plan import plan_reads
from fujilib.registry.channels import ChannelId, Gas
from fujilib.registry.enums import HoldMode, RangeIndex, RangeMethod
from fujilib.registry.registers import REGISTRY
from fujilib.testing import DEFAULT_ZPA_BANK, FaultKind, MockRequest, mock_transport
from tests.conftest import approx
from tests.facade import FAST, FC03, FC04, POLL, RANGES, analyzer_on, bench, when

if TYPE_CHECKING:
    from fujilib.devices.analyzer import Analyzer
    from fujilib.testing import MockAnalyzer

pytestmark = pytest.mark.anyio

FC06 = 0x06
CH1, CH2, CH3, CH5, CH6 = (
    ChannelId.CH1,
    ChannelId.CH2,
    ChannelId.CH3,
    ChannelId.CH5,
    ChannelId.CH6,
)
O2_RESPONSE = 0x53  # response_time.o2
CH3_SELECTED = 0x6B  # range.ch3.selected
CH3_CURRENT = (FC04, 0x27, 1)  # a read of range.ch3.current


def single(address: int) -> list[tuple[int, int | None, int | None]]:
    """The read before, the write and the read-back of one setting."""
    return [(FC03, address, 1), (FC06, address, 1), (FC03, address, 1)]


def fail_drain_after(monkeypatch: pytest.MonkeyPatch, anz: Analyzer, sends: int) -> None:
    """Make the host port fail while draining its request after ``sends`` good ones."""
    stream = anz.session._port.transport.stream
    assert isinstance(stream, anyserial.SerialPort)
    cls = type(stream)
    original = cls.drain
    count = 0

    async def drain(self: anyserial.SerialPort) -> None:
        nonlocal count
        count += 1
        if count > sends:
            raise anyserial.SerialError("device reports an I/O error")
        await original(self)

    monkeypatch.setattr(cls, "drain", drain)


# --- A write, read back ------------------------------------------------------------------------


async def test_a_setting_is_written_once_and_read_back() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        result = await anz.write_parameter("response_time.o2", 16, confirm=True)
    assert mock.transactions() == POLL + single(O2_RESPONSE)
    assert mock.holding[O2_RESPONSE] == 16
    assert result.state is WriteState.VERIFIED
    assert result.verified
    assert result.acknowledged
    assert result.changed
    assert result.name == "response_time.o2"
    assert (result.previous.value, result.requested.value) == (15, 16)
    assert result.observed is not None
    assert result.observed.value == 16
    assert result.timing is not None
    assert result.read_back_error is None


async def test_writing_the_value_it_has_is_still_a_write() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        result = await anz.write_parameter("response_time.o2", 15, confirm=True)
    assert mock.transactions() == POLL + single(O2_RESPONSE)
    assert result.verified
    assert not result.changed


async def test_a_calibration_gas_is_scaled_by_its_range_read_just_before() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        result = await anz.write_parameter(
            "calibration_gas.ch3.range1.span", 20.9, unit="vol%", confirm=True
        )
    assert mock.transactions() == POLL + RANGES + single(0x09)
    assert mock.holding[0x09] == 2090
    assert result.requested.value == approx(20.9)
    assert result.requested.unit == "vol%"
    assert result.previous.value == approx(20.95)


async def test_a_calibration_gas_in_the_wrong_unit_is_not_written() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        with pytest.raises(FujiValidationError, match="is in vol%, not ppm"):
            await anz.write_parameter(
                "calibration_gas.ch3.range1.span", 2090, unit="ppm", confirm=True
            )
    assert mock.transactions() == POLL + RANGES + [(FC03, 0x09, 1)]
    assert FC06 not in {r.request.function for r in mock.exchanges}


async def test_a_range_is_selected_only_while_its_method_is_manual() -> None:
    mock = bench()
    names = ["range.ch3.selected", "range.ch3.method"]
    before = [b.key for b in plan_reads([REGISTRY.resolve(n) for n in names])]
    async with analyzer_on(mock) as (anz, _):
        result = await anz.set_range(CH3, 2, confirm=True)
        assert result.requested.value is RangeIndex.RANGE_2
        assert mock.holding[0x6B] == 1
        mock.clear()
        mock.set_register("range.ch3.method", int(RangeMethod.AUTO))
        with pytest.raises(
            FujiValidationError, match=r"range\.ch3\.method is manual, and it is auto"
        ):
            await anz.set_range(CH3, 1, confirm=True)
    assert mock.transactions() == POLL + RANGES + before
    assert mock.holding[0x6B] == 1


def range_write() -> list[tuple[int, int | None, int | None]]:
    """What selecting Ch3's range sends: the checks, the write, its read-back and its follow."""
    names = ["range.ch3.selected", "range.ch3.method"]
    before = [b.key for b in plan_reads([REGISTRY.resolve(n) for n in names])]
    after: list[tuple[int, int | None, int | None]] = [
        (FC06, CH3_SELECTED, 1),
        (FC03, CH3_SELECTED, 1),
        CH3_CURRENT,
    ]
    return POLL + RANGES + before + after


async def test_a_range_write_is_followed_until_the_channel_measures_on_it() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        result = await anz.set_range(CH3, 2, confirm=True)
    assert result.verified
    assert mock.transactions() == range_write()
    assert mock.register("range.ch3.current") == (1,)


async def test_a_range_write_waits_for_a_channel_that_switches_late() -> None:
    mock = bench(replace(DEFAULT_ZPA_BANK, range_lag_s=0.12))
    async with analyzer_on(mock) as (anz, _):
        result = await anz.set_range(CH3, 2, confirm=True)
        sent = mock.transactions()
        assert (await anz.channel_status(CH3)).range == 2
    assert result.verified
    assert sent[: len(range_write())] == range_write()
    assert set(sent[len(range_write()) :]) == {CH3_CURRENT}


async def test_a_channel_that_never_switches_fails_the_write() -> None:
    mock = bench(replace(DEFAULT_ZPA_BANK, range_lag_s=3600.0))
    async with analyzer_on(mock) as (anz, _):
        anz.session._verify_timeout = 0.2
        with pytest.raises(
            FujiVerificationError,
            match=r"wrote range_2 and it reads back, but CH3 still measures on range_1 after",
        ) as info:
            await anz.set_range(CH3, 2, confirm=True)
        assert anz.session.state is SessionState.OPEN
    extra = info.value.context.extra
    assert (extra["write_state"], extra["requested_raw"], extra["current_range_raw"]) == (
        "verified",
        1,
        0,
    )
    assert info.value.__cause__ is None
    assert mock.holding[CH3_SELECTED] == 1
    assert mock.transactions().count(CH3_CURRENT) > 1


async def test_a_current_range_that_cannot_be_read_fails_the_write() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        mock.inject(FaultKind.DROP, times=None, when=when(*CH3_CURRENT))
        with pytest.raises(FujiVerificationError, match="CH3's current range could not be read") as info:
            await anz.set_range(CH3, 2, confirm=True)
    assert isinstance(info.value.__cause__, FujiTimeoutError)
    assert info.value.context.extra["current_range_raw"] is None
    assert mock.holding[CH3_SELECTED] == 1


async def test_a_port_that_fails_while_a_range_is_followed_breaks_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        fail_drain_after(monkeypatch, anz, sends=len(range_write()) - 1)  # all but the follow
        with pytest.raises(FujiConnectionError):
            await anz.set_range(CH3, 2, confirm=True)
        assert anz.session.state is SessionState.BROKEN


async def test_a_range_the_channel_does_not_have_is_not_written() -> None:
    # Every channel's range tables list two ranges; its range count says which exist.
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        with pytest.raises(FujiValidationError, match="CH1 has 1 range"):
            await anz.set_range(CH1, 2, confirm=True)
        with pytest.raises(FujiValidationError, match="CH1 has 1 range"):
            await anz.set_calibration_gas(CH1, 2, "span", 10, unit="vol%", confirm=True)
    assert FC06 not in {r.request.function for r in mock.exchanges}


async def test_a_range_write_makes_the_range_tables_be_read_again() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        await anz.set_range(CH3, 2, confirm=True)
        mock.clear()
        await anz.read_parameter("calibration_gas.ch3.range2.span")
    assert mock.transactions()[:1] == RANGES


async def test_a_write_that_fails_still_makes_the_range_tables_be_read_again() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        mock.inject(FaultKind.EXCEPTION, when=when(FC06, 0x6B), exception_code=0x03)
        with pytest.raises(FujiModbusIllegalDataValueError):
            await anz.set_range(CH3, 2, confirm=True)
        mock.clear()
        await anz.read_parameter("calibration_gas.ch3.range2.span")
    assert mock.transactions()[:1] == RANGES


# --- Gates: refused before anything is sent -------------------------------------------------


WRITES: list[tuple[str, dict[str, Any]]] = [
    ("write_parameter", {"name": "response_time.o2", "value": 16}),
    ("set_response_time", {"target": "o2", "seconds": 16}),
    ("set_output_hold", {"enabled": True}),
    ("set_hold_mode", {"mode": "setting"}),
    ("set_hold_value", {"channel": "CH5", "percent_fs": 10}),
    ("set_range", {"channel": "CH3", "range_number": 2}),
    ("set_range_method", {"channel": "CH3", "method": "auto"}),
    (
        "set_calibration_gas",
        {"channel": "CH3", "range_number": 1, "kind": "span", "value": 20.9, "unit": "vol%"},
    ),
]


@pytest.mark.parametrize(("method", "kwargs"), WRITES, ids=[w[0] for w in WRITES])
@pytest.mark.parametrize("confirm", [False, 1, "yes"])
async def test_a_write_without_confirm_sends_nothing(
    method: str, kwargs: dict[str, Any], confirm: object
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        with pytest.raises(FujiConfirmationRequiredError, match="pass confirm=True") as info:
            await getattr(anz, method)(**kwargs, confirm=confirm)
    assert mock.exchanges == []
    assert info.value.context.extra["safety"] in {"persistent", "dangerous"}


#: The register each entry of WRITES writes, and the word it writes there.
WRITTEN = {
    "write_parameter": ("response_time.o2", 0x53, 16),
    "set_response_time": ("response_time.o2", 0x53, 16),
    "set_output_hold": ("output_hold.enabled", 0x5C, 1),
    "set_hold_mode": ("hold.mode", 0x8B, 1),
    "set_hold_value": ("hold.ch5.value", 0x90, 10),
    "set_range": ("range.ch3.selected", 0x6B, 1),
    "set_range_method": ("range.ch3.method", 0x70, 2),
    "set_calibration_gas": ("calibration_gas.ch3.range1.span", 0x09, 2090),
}


@pytest.mark.parametrize(("method", "kwargs"), WRITES, ids=[w[0] for w in WRITES])
async def test_each_write_helper_writes_its_register(method: str, kwargs: dict[str, Any]) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        result = await getattr(anz, method)(**kwargs, confirm=True)
    name, address, word = WRITTEN[method]
    assert isinstance(result, WriteResult)
    assert result.verified
    assert result.changed
    assert result.name == name
    writes = [
        (r.request.address, r.request.values) for r in mock.exchanges if r.request.function == FC06
    ]
    assert writes == [(address, (word,))]
    assert mock.holding[address] == word


async def test_the_tier_is_named_in_the_refusal() -> None:
    async with analyzer_on(bench()) as (anz, _):
        with pytest.raises(
            FujiConfirmationRequiredError, match=r"calibration_gas\.ch3\.range1\.span is DANGEROUS"
        ):
            await anz.write_parameter("calibration_gas.ch3.range1.span", 20, unit="vol%")
        with pytest.raises(FujiConfirmationRequiredError, match="PERSISTENT"):
            await anz.set_output_hold(True)


@pytest.mark.parametrize(
    ("name", "match"),
    [("key_lock", "read-only"), ("alarm1.mode", "read-only"), ("keylock", "unknown register")],
)
async def test_an_unknown_or_read_only_name_is_refused_before_the_confirm_check(
    name: str, match: str
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        with pytest.raises(FujiValidationError, match=match):
            await anz.write_parameter(name, 1)
    assert mock.exchanges == []


async def test_confirm_is_checked_before_the_value() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        with pytest.raises(FujiConfirmationRequiredError):
            await anz.write_parameter("response_time.o2", 999)
        with pytest.raises(FujiValidationError, match="outside 1-60"):
            await anz.write_parameter("response_time.o2", 999, confirm=True)
    assert mock.exchanges == []


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"channel": "CH6", "range_number": 1, "kind": "span"}, "derived channel"),
        ({"channel": "CH3", "range_number": 3, "kind": "span"}, "range 1 or 2"),
        ({"channel": "CH3", "range_number": True, "kind": "span"}, "range 1 or 2"),
        ({"channel": "CH3", "range_number": 1, "kind": "offset"}, "'zero' or 'span'"),
    ],
)
async def test_calibration_gas_arguments_are_checked_first(
    kwargs: dict[str, Any], match: str
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        with pytest.raises(FujiValidationError, match=match):
            await anz.set_calibration_gas(**kwargs, value=1, unit="vol%", confirm=True)
    assert mock.exchanges == []


async def test_range_arguments_are_checked_first() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        with pytest.raises(FujiValidationError, match="range_number must be 1 or 2"):
            await anz.set_range(CH3, 0, confirm=True)
        with pytest.raises(FujiValidationError, match="derived channel"):
            await anz.set_range_method(CH6, "manual", confirm=True)
        with pytest.raises(FujiValidationError, match="only manual, auto"):
            await anz.set_range_method(CH3, RangeMethod.REMOTE, confirm=True)
        with pytest.raises(FujiValidationError, match="derived channel"):
            await anz.set_hold_value(CH6, 10, confirm=True)
    assert mock.exchanges == []


# --- Response-time slots ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "address"),
    [
        (CH3, 0x53),  # asserted O2
        (CH1, 0x4B),  # the first NDIR channel
        (CH2, 0x4D),
        ("o2", 0x53),
        ("NDIR4", 0x51),
        (" ndir2 ", 0x4D),
    ],
)
async def test_a_response_time_goes_to_its_slot(target: ChannelId | str, address: int) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        await anz.set_response_time(target, 20, confirm=True)
    assert mock.holding[address] == 20


async def test_a_channel_maps_to_a_slot_only_through_asserted_gases() -> None:
    mock = bench()
    async with analyzer_on(mock, channel_map={CH2: Gas.CO}) as (anz, _):
        with pytest.raises(FujiValidationError, match="CH1 has none; name the slot"):
            await anz.set_response_time(CH2, 20, confirm=True)
        with pytest.raises(FujiValidationError, match="derived channel"):
            await anz.set_response_time(CH6, 20, confirm=True)
    all_ndir = {ChannelId.from_number(n): Gas.CO2 for n in range(1, 6)}
    async with analyzer_on(bench(), channel_map=all_ndir) as (anz, _):
        with pytest.raises(FujiValidationError, match="NDIR component 5; there are only four"):
            await anz.set_response_time(CH5, 20, confirm=True)
    assert mock.exchanges == []


async def test_an_o2_channel_before_an_ndir_one_takes_no_ndir_slot() -> None:
    mock = bench()
    async with analyzer_on(mock, channel_map={CH1: Gas.O2, CH2: Gas.CO2}) as (anz, _):
        await anz.set_response_time(CH2, 20, confirm=True)
    assert mock.holding[0x4B] == 20  # ndir1


# --- Not while the analyzer is busy -------------------------------------------------------


@pytest.mark.parametrize(
    ("register", "value", "reason"),
    [
        ("status.auto_calibration_running", 1, "auto calibration or auto zero calibration"),
        ("status.ch2.span_calibrating", 1, "CH2 is being calibrated"),
        ("status.ch1.auto_zero_running", 1, "CH1 is being calibrated"),
        ("display.screen", 8, "the maintenance screen"),
        ("display.screen", 42, "screen 42 screen"),
        ("display.calibration_step", 5, "manual calibration is in progress"),
    ],
)
async def test_nothing_is_written_while_the_analyzer_is_busy(
    register: str, value: int, reason: str
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        mock.set_register(register, value)
        with pytest.raises(FujiAnalyzerStateError, match=reason) as info:
            await anz.write_parameter("response_time.o2", 16, confirm=True)
        assert anz.session.last_error is not None
    assert mock.transactions() == POLL
    assert mock.holding[O2_RESPONSE] == 15
    assert info.value.context.extra["reasons"]


def test_busy_reasons_without_a_display() -> None:
    idle = ChannelStatus(1, False, False, False, False, False, frozenset())
    analyzer = AnalyzerStatus(False, False, frozenset(), (), 0, False, False, None)
    assert busy_reasons(StatusRead(analyzer, {CH1: idle}, ())) == []


# --- What the read-back finds --------------------------------------------------------------


async def test_a_write_acknowledged_but_not_stored_is_a_mismatch() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        mock.inject(FaultKind.IGNORE, when=when(FC06, O2_RESPONSE))
        with pytest.raises(
            FujiVerificationError, match="wrote 16 s, but it reads back as 15 s"
        ) as info:
            await anz.write_parameter("response_time.o2", 16, confirm=True)
        assert anz.session.state is SessionState.OPEN
    extra = info.value.context.extra
    assert extra["write_state"] == "mismatch"
    assert (extra["requested_raw"], extra["observed_raw"], extra["previous_raw"]) == (16, 15, 15)
    assert extra["acknowledged"] is True
    assert extra["setting"] == "response_time.o2"
    assert info.value.context.port is not None
    assert mock.transactions() == POLL + single(O2_RESPONSE)


async def test_a_write_whose_reply_is_lost_is_verified_by_its_read_back() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        mock.inject(FaultKind.DROP, when=when(FC06, O2_RESPONSE))
        result = await anz.write_parameter("response_time.o2", 16, confirm=True)
    assert result.verified
    assert not result.acknowledged
    assert result.timing is None
    assert mock.holding[O2_RESPONSE] == 16
    assert mock.transactions() == POLL + single(O2_RESPONSE)


async def test_a_write_whose_reply_is_lost_and_that_did_not_arrive_is_a_mismatch() -> None:
    mock = bench()

    def undo(request: MockRequest) -> None:
        if request.function == FC03 and request.address == O2_RESPONSE:
            mock.set_register("response_time.o2", 15)

    async with analyzer_on(mock) as (anz, _):
        mock.inject(FaultKind.DROP, when=when(FC06, O2_RESPONSE))
        mock.on_request = undo
        with pytest.raises(
            FujiVerificationError, match=r"was lost, .* the write was not applied"
        ) as info:
            await anz.write_parameter("response_time.o2", 16, confirm=True)
    assert info.value.context.extra["acknowledged"] is False


def _lose_read_backs(mock: MockAnalyzer, address: int) -> None:
    def arm(request: MockRequest) -> None:
        if request.function == FC06:
            mock.inject(FaultKind.DROP, times=None, when=when(FC03, address))

    mock.on_request = arm


async def test_a_lost_reply_and_a_failed_read_back_leave_the_outcome_unknown() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        mock.inject(FaultKind.DROP, when=when(FC06, O2_RESPONSE))
        _lose_read_backs(mock, O2_RESPONSE)
        with pytest.raises(
            FujiWriteOutcomeUnknownError, match="may or may not have been applied"
        ) as info:
            await anz.write_parameter("response_time.o2", 16, confirm=True)
        assert anz.session.state is SessionState.OPEN  # a timeout does not break the session
    extra = info.value.context.extra
    assert (extra["write_state"], extra["acknowledged"], extra["observed_raw"]) == (
        "unknown",
        False,
        None,
    )
    assert "failure" not in extra


async def test_an_acknowledged_write_whose_read_back_fails_is_unknown() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        _lose_read_backs(mock, O2_RESPONSE)
        with pytest.raises(
            FujiWriteOutcomeUnknownError,
            match="acknowledged writing 16 s, but reading it back failed",
        ):
            await anz.write_parameter("response_time.o2", 16, confirm=True)
    assert mock.holding[O2_RESPONSE] == 16


async def test_an_exception_reply_is_a_refusal_and_is_not_read_back() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        mock.inject(FaultKind.EXCEPTION, when=when(FC06, O2_RESPONSE), exception_code=0x03)
        with pytest.raises(FujiModbusIllegalDataValueError):
            await anz.write_parameter("response_time.o2", 16, confirm=True)
    assert mock.transactions() == POLL + single(O2_RESPONSE)[:2]
    assert mock.holding[O2_RESPONSE] == 15


async def test_a_caller_that_cancels_a_write_cancels_its_read_back_too() -> None:
    # Cancelling the call (not a deadline) propagates; the late-reply window
    # protects the next request, and a later read shows what happened.
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        mock.inject(FaultKind.DELAY, when=when(FC06, O2_RESPONSE), delay_s=0.3)
        with anyio.move_on_after(0.1) as scope:
            await anz.write_parameter("response_time.o2", 16, confirm=True)
        assert scope.cancelled_caught
        assert anz.session.state is SessionState.OPEN
        assert (await anz.read_parameter("response_time.o2")).value == 16
    sent = mock.transactions()
    assert sent[: len(POLL) + 2] == POLL + single(O2_RESPONSE)[:2]


async def test_a_lost_reply_and_a_third_value_is_neither() -> None:
    mock = bench()

    def change(request: MockRequest) -> None:
        read_back = request.function == FC03 and request.address == O2_RESPONSE
        if read_back and mock.holding[O2_RESPONSE] == 16:
            mock.set_register("response_time.o2", 30)  # the panel, meanwhile

    async with analyzer_on(mock) as (anz, _):
        mock.inject(FaultKind.DROP, when=when(FC06, O2_RESPONSE))
        mock.on_request = change
        with pytest.raises(FujiVerificationError, match="neither what was written nor what it was"):
            await anz.write_parameter("response_time.o2", 16, confirm=True)


async def test_the_session_derives_its_read_back_budget() -> None:
    # Two block reads, each with every retry and a late-reply window.
    expected = 2 * (FAST["resync_window"] + 3 * FAST["request_timeout"])
    async with analyzer_on(bench()) as (anz, _):
        assert anz.session.verify_timeout == approx(expected)


async def test_a_write_that_outlives_its_deadline_is_still_read_back() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        mock.inject(FaultKind.DELAY, when=when(FC06, O2_RESPONSE), delay_s=0.4)
        with anyio.fail_after(5):
            result = await anz.write_parameter("response_time.o2", 16, confirm=True, timeout=0.15)
    assert result.verified
    assert not result.acknowledged
    # The late write reply can land on the read-back, which then rejects it
    # (another function code) and reads again.
    sent = mock.transactions()
    assert sent[: len(POLL) + 2] == POLL + single(O2_RESPONSE)[:2]
    assert set(sent[len(POLL) + 2 :]) == {(FC03, O2_RESPONSE, 1)}


async def test_a_port_that_fails_during_the_write_breaks_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        fail_drain_after(monkeypatch, anz, sends=3)  # the status (2) and the read before
        with pytest.raises(FujiWriteOutcomeUnknownError) as info:
            await anz.write_parameter("response_time.o2", 16, confirm=True)
        assert anz.session.state is SessionState.BROKEN
    assert info.value.context.extra["failure"] == "connection"
    assert info.value.context.extra["setting"] == "response_time.o2"


async def test_a_port_that_fails_during_the_read_back_breaks_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        fail_drain_after(monkeypatch, anz, sends=4)  # ... and the write
        with pytest.raises(FujiWriteOutcomeUnknownError, match="acknowledged") as info:
            await anz.write_parameter("response_time.o2", 16, confirm=True)
        assert anz.session.state is SessionState.BROKEN
    assert info.value.context.extra["failure"] == "connection"


# --- Outcomes, described ----------------------------------------------------------------------


def _result(
    state: WriteState, *, observed: int | None = 16, acknowledged: bool = True
) -> WriteResult:
    spec = REGISTRY.resolve("hold.mode")
    value = decode_register(spec, (0,))
    seen = decode_register(spec, (observed,)) if observed is not None else None
    return WriteResult("hold.mode", value, value, seen, state, acknowledged, None)


def test_a_verified_write_has_no_error() -> None:
    assert outcome_error(_result(WriteState.VERIFIED, observed=0)) is None


def test_values_are_described_for_messages() -> None:
    mode = REGISTRY.resolve("hold.mode")
    assert describe(decode_register(mode, (1,))) == "setting"
    assert describe(decode_register(REGISTRY.resolve("response_time.o2"), (15,))) == "15 s"
    unscaled = decode_register(REGISTRY.resolve("calibration_gas.ch3.range1.span"), (2095,))
    assert describe(unscaled) == "2095"
    assert HoldMode(1).name.lower() == "setting"


# --- The write-rate warning -----------------------------------------------------------------


def test_the_write_rate_warning_fires_once_per_crossing(caplog: pytest.LogCaptureFixture) -> None:
    now = [0.0]
    monitor = WriteRateMonitor(warn_per_minute=2, clock=lambda: now[0])
    with caplog.at_level(logging.WARNING, logger="fujilib.session"):
        for _ in range(4):
            monitor.record("hold.mode", where="COM8 station 1")
        assert len(caplog.records) == 1
        assert "4 setting writes in the last minute" not in caplog.text
        assert "3 setting writes in the last minute (the latest hold.mode)" in caplog.text
        now[0] = 61.0
        assert monitor.count == 0
        monitor.record("hold.mode", where="COM8 station 1")
        assert len(caplog.records) == 1
        for _ in range(2):
            monitor.record("hold.mode", where="COM8 station 1")
        assert len(caplog.records) == 2


def test_a_zero_threshold_never_warns(caplog: pytest.LogCaptureFixture) -> None:
    monitor = WriteRateMonitor(warn_per_minute=0)
    with caplog.at_level(logging.WARNING, logger="fujilib.session"):
        for _ in range(50):
            monitor.record("hold.mode", where="here")
    assert caplog.records == []
    assert monitor.count == 0


async def test_open_device_sets_the_write_rate_threshold(caplog: pytest.LogCaptureFixture) -> None:
    async with mock_transport(bench()) as (transport, _):
        anz = await open_device(
            transport, channel_map={"CH3": "o2"}, write_warn_per_minute=1, timeout=0.25
        )
        async with anz:
            with caplog.at_level(logging.WARNING, logger="fujilib.session"):
                await anz.write_parameter("hold.ch5.value", 1, confirm=True)
                await anz.write_parameter("hold.ch5.value", 2, confirm=True)
            assert anz.session.write_rate.count == 2
    assert "2 setting writes in the last minute" in caplog.text


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"write_warn_per_minute": -1}, "write_warn_per_minute"),
        ({"write_warn_per_minute": True}, "write_warn_per_minute"),
        ({"options": Capability.CLOCK}, "option capabilities"),
        ({"options": "auto_zero"}, "option capabilities"),
    ],
)
async def test_open_device_checks_its_new_arguments(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(FujiValidationError, match=match):
        await open_device("COM_NOT_OPENED", **kwargs)


def test_the_tiers_order_their_effect() -> None:
    assert SafetyTier.READ_ONLY < SafetyTier.STATEFUL < SafetyTier.PERSISTENT < SafetyTier.DANGEROUS
