"""Settings documents: comparing them with the analyzer and applying them (design §6.3).

Against the simulated bench analyzer: a document dumped from it compares as
unchanged; a refused document writes nothing; writes go in dependency order and
stop at the first failure.
"""

from __future__ import annotations

from typing import Any

import pytest

from fujilib.cli._report import settings_report
from fujilib.devices.capability import SafetyTier
from fujilib.devices.settings import (
    SETTINGS_FORMAT,
    ApplyStatus,
    ChangeAction,
    SettingsDocument,
)
from fujilib.errors import (
    FujiConfirmationRequiredError,
    FujiModbusIllegalDataValueError,
    FujiValidationError,
)
from fujilib.testing import FaultKind
from tests.facade import FC03, analyzer_on, bench, when

pytestmark = pytest.mark.anyio

FC06 = 0x06
SERIAL = "N8A0259T"


def document(settings: dict[str, Any], serial: str | None = SERIAL) -> dict[str, Any]:
    analyzer = {"serial_number": serial} if serial is not None else {}
    return {"format": SETTINGS_FORMAT, "analyzer": analyzer, "settings": settings}


def writes_sent(mock: Any) -> list[int | None]:
    return [r.request.address for r in mock.exchanges if r.request.function == FC06]


# --- The document ------------------------------------------------------------------------------


def test_a_document_reads_entries_and_bare_values() -> None:
    doc = SettingsDocument.from_json(
        {
            "format": SETTINGS_FORMAT,
            "analyzer": {"model": "ZPA", "serial_number": SERIAL, "type_code": "ZPA", "port": 7},
            "settings": {
                "response_time.o2": {"value": 16, "unit": "s", "raw": 16, "access": "read_write"},
                "hold.mode": "setting",
                "alarm1.range1.high": {"value": None, "raw": 50},
                "output_hold.enabled": {"value": True, "raw": True},
            },
        }
    )
    assert (doc.model, doc.serial_number, doc.type_code) == ("ZPA", SERIAL, "ZPA")
    assert doc.settings["response_time.o2"].unit == "s"
    assert doc.settings["hold.mode"].value == "setting"
    assert doc.settings["alarm1.range1.high"].raw == 50
    assert doc.settings["output_hold.enabled"].raw is None  # a flag's raw is not a word


def test_a_minimal_document_needs_no_analyzer() -> None:
    doc = SettingsDocument.from_json({"format": SETTINGS_FORMAT, "settings": {"key_lock": False}})
    assert doc.serial_number is None
    assert doc.settings["key_lock"].value is False


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ([], "JSON object"),
        ({"format": "fujilib-settings/2", "settings": {}}, "expected format"),
        ({"format": SETTINGS_FORMAT}, "'settings' object"),
        ({"format": SETTINGS_FORMAT, "settings": {}, "analyzer": "ZPA"}, "'analyzer'"),
        ({"format": SETTINGS_FORMAT, "settings": {"hold.mode": [1]}}, "a flag, a number"),
        (
            {"format": SETTINGS_FORMAT, "settings": {}, "analyzer": {"serial_number": 12345}},
            "must be text",
        ),
        (
            {"format": SETTINGS_FORMAT, "settings": {"hold.mode": {"value": 1, "unit": 5}}},
            "a unit is text",
        ),
    ],
)
def test_a_malformed_document_is_refused(data: object, match: str) -> None:
    with pytest.raises(FujiValidationError, match=match):
        SettingsDocument.from_json(data)


# --- Comparing -------------------------------------------------------------------------------


async def test_a_dump_of_the_analyzer_compares_as_unchanged() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        dumped = document(settings_report(await anz.read_settings()))
        diff = await anz.diff_settings(dumped)
    assert diff.ok
    assert not diff.writes
    assert not diff.refused
    assert len(diff.unchanged) == 162
    assert diff.tier is SafetyTier.READ_ONLY
    assert writes_sent(mock) == []


async def test_what_a_document_would_change() -> None:
    mock = bench()
    settings = {
        "response_time.o2": {"value": 16, "unit": "s"},
        "response_time.ndir1": {"value": 15, "unit": "s"},
        "calibration_gas.ch3.range1.span": {"value": 20.9, "unit": "vol%"},
        "calibration_gas.ch3.range1.zero": {"value": 0, "unit": "vol%"},
        "range.ch3.method": "manual",
        "hold.mode": "last_value",
    }
    async with analyzer_on(mock) as (anz, _):
        diff = await anz.diff_settings(document(settings))
    actions = {c.name: c.action for c in diff.changes}
    assert actions == {
        "response_time.o2": ChangeAction.WRITE,
        "response_time.ndir1": ChangeAction.UNCHANGED,
        "calibration_gas.ch3.range1.span": ChangeAction.WRITE,
        "calibration_gas.ch3.range1.zero": ChangeAction.UNCHANGED,
        "range.ch3.method": ChangeAction.UNCHANGED,
        "hold.mode": ChangeAction.UNCHANGED,
    }
    assert diff.tier is SafetyTier.DANGEROUS
    assert [c.name for c in diff.writes] == ["response_time.o2", "calibration_gas.ch3.range1.span"]
    assert all(c.current is not None for c in diff.changes)
    assert writes_sent(mock) == []


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        ({"start_auto_calibration": 1}, "operation command"),
        ({"return_to_measurement": True}, "operation command"),
        ({"keylock": True}, "not a register of the map"),
        ({"reading.ch1.value": 5}, "input register"),
        ({"key_lock": True}, "read-only"),
        ({"alarm1.range1.high": {"value": None, "raw": 50}}, "read-only"),
        ({"response_time.o2": {"value": None, "raw": 20}}, "no value to write"),
        ({"response_time.o2": 61}, "outside 0-60"),
        ({"response_time.o2": {"value": 16, "unit": "ms"}}, "takes no unit 'ms'"),
        ({"hold.mode": 1}, "expects one of"),
        ({"calibration_gas.ch3.range1.span": {"value": 2090, "unit": "ppm"}}, "not ppm"),
        ({"calibration_gas.ch3.range1.span": {"value": "lots", "unit": "vol%"}}, "number"),
        ({"calibration_gas.ch3.range1.span": {"value": 20.9}}, "give the unit"),
        # Equal to the analyzer's value, but meaningless without a unit.
        ({"calibration_gas.ch3.range1.span": 20.95}, "give the unit"),
        ({"calibration_gas.ch1.range2.span": {"value": 5, "unit": "vol%"}}, "CH1 has 1 range"),
        ({"range.ch1.selected": "range_2"}, "CH1 has 1 range"),
        (
            {"range.ch3.selected": "range_2", "range.ch3.method": "auto"},
            "would be auto",
        ),
        ({"range.ch3.selected": "range_2", "range.ch3.method": "sideways"}, "expects one of"),
        ({"range.ch1.method": "remote"}, "only manual, auto"),
    ],
)
async def test_what_a_document_may_not_do(settings: dict[str, Any], reason: str) -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        diff = await anz.diff_settings(document(settings))
        with pytest.raises(FujiValidationError, match="refused, nothing was written") as info:
            await anz.apply_settings(document(settings), confirm=True)
    assert not diff.ok
    assert any(reason in (c.reason or "") for c in diff.refused)
    assert all(c.safety is SafetyTier.READ_ONLY for c in diff.refused)
    assert info.value.context.extra["refused"]
    assert writes_sent(mock) == []


async def test_a_range_is_selected_once_the_document_makes_its_method_manual() -> None:
    mock = bench()
    mock.set_register("range.ch3.method", 2)  # auto
    settings = {"range.ch3.selected": "range_2", "range.ch3.method": "manual"}
    async with analyzer_on(mock) as (anz, _):
        report = await anz.apply_settings(document(settings), confirm=True)
    assert report.status is ApplyStatus.OK
    assert [r.name for r in report.completed] == ["range.ch3.method", "range.ch3.selected"]
    assert writes_sent(mock) == [0x70, 0x6B]


async def test_another_analyzers_document_is_refused_unless_any_will_do() -> None:
    mock = bench()
    other = document({"response_time.o2": 16}, serial="Q1234567")
    async with analyzer_on(mock) as (anz, _):
        diff = await anz.diff_settings(other)
        assert diff.identity_mismatch is not None
        assert "'Q1234567'" in diff.identity_mismatch
        with pytest.raises(FujiValidationError, match="serial number 'Q1234567'"):
            await anz.apply_settings(other, confirm=True)
        report = await anz.apply_settings(other, confirm=True, any_analyzer=True)
    assert report.status is ApplyStatus.OK
    assert writes_sent(mock) == [0x53]


async def test_output_hold_goes_first_when_switched_on_and_last_when_off() -> None:
    mock = bench()
    on = {"hold.mode": "setting", "output_hold.enabled": True, "hold.ch1.value": 10}
    off = {"hold.mode": "last_value", "output_hold.enabled": False, "hold.ch1.value": 0}
    async with analyzer_on(mock) as (anz, _):
        first = await anz.apply_settings(document(on), confirm=True)
        second = await anz.apply_settings(document(off), confirm=True)
    assert [r.name for r in first.completed] == [
        "output_hold.enabled",
        "hold.mode",
        "hold.ch1.value",
    ]
    assert [r.name for r in second.completed] == [
        "hold.mode",
        "hold.ch1.value",
        "output_hold.enabled",
    ]


async def test_diff_identifies_an_analyzer_not_yet_identified() -> None:
    mock = bench()
    async with analyzer_on(mock, identify=False) as (anz, _):
        diff = await anz.diff_settings(document({"response_time.o2": 16}))
        assert anz.info is not None
    assert [c.name for c in diff.writes] == ["response_time.o2"]


# --- Applying --------------------------------------------------------------------------------


async def test_applying_a_document_with_nothing_to_change_writes_nothing() -> None:
    mock = bench()
    async with analyzer_on(mock) as (anz, _):
        report = await anz.apply_settings(document({"response_time.o2": 15}))
    assert report.status is ApplyStatus.OK
    assert report.completed == ()
    assert writes_sent(mock) == []


async def test_a_ceiling_refuses_writes_above_it() -> None:
    mock = bench()
    settings = {
        "response_time.o2": 16,
        "calibration_gas.ch3.range1.span": {"value": 20.9, "unit": "vol%"},
    }
    persistent = SafetyTier.PERSISTENT
    async with analyzer_on(mock) as (anz, _):
        with pytest.raises(
            FujiConfirmationRequiredError, match="DANGEROUS writes, above PERSISTENT"
        ):
            await anz.apply_settings(document(settings), confirm=True, max_tier=persistent)
        report = await anz.apply_settings(
            document({"response_time.o2": 16}), confirm=True, max_tier=persistent
        )
    assert report.status is ApplyStatus.OK
    assert writes_sent(mock) == [0x53]


async def test_applying_needs_confirm_and_writes_nothing_without_it() -> None:
    mock = bench()
    settings = {"calibration_gas.ch3.range1.span": {"value": 20.9, "unit": "vol%"}}
    async with analyzer_on(mock) as (anz, _):
        with pytest.raises(
            FujiConfirmationRequiredError, match="applying the settings is DANGEROUS"
        ):
            await anz.apply_settings(document(settings))
    assert writes_sent(mock) == []


async def test_a_document_is_applied_in_order_and_read_back() -> None:
    mock = bench()
    settings = {
        "calibration_gas.ch3.range1.span": {"value": 20.9, "unit": "vol%"},
        "response_time.o2": 16,
        "calibration.ch3.range_mode": "both",
    }
    async with analyzer_on(mock) as (anz, _):
        report = await anz.apply_settings(
            SettingsDocument.from_json(document(settings)), confirm=True, timeout=10
        )
    assert report.status is ApplyStatus.OK
    assert [r.name for r in report.completed] == [
        "response_time.o2",
        "calibration.ch3.range_mode",
        "calibration_gas.ch3.range1.span",
    ]
    assert all(r.verified for r in report.completed)
    assert (mock.holding[0x53], mock.holding[0x20], mock.holding[0x09]) == (16, 1, 2090)
    assert report.failed is None
    assert report.not_attempted == ()


@pytest.mark.parametrize(
    ("fault", "at", "status", "completed"),
    [
        (FaultKind.IGNORE, 0x20, ApplyStatus.VERIFY_FAILED, 1),
        (FaultKind.EXCEPTION, 0x53, ApplyStatus.FAILED, 0),
        (FaultKind.EXCEPTION, 0x20, ApplyStatus.PARTIAL, 1),
    ],
)
async def test_the_first_failed_write_stops_the_rest(
    fault: FaultKind, at: int, status: ApplyStatus, completed: int
) -> None:
    mock = bench()
    settings = {
        "calibration_gas.ch3.range1.span": {"value": 20.9, "unit": "vol%"},
        "response_time.o2": 16,
        "calibration.ch3.range_mode": "both",
    }
    async with analyzer_on(mock) as (anz, _):
        mock.inject(fault, when=when(FC06, at), exception_code=0x03)
        report = await anz.apply_settings(document(settings), confirm=True)
    assert report.status is status
    assert len(report.completed) == completed
    assert report.error is not None
    assert report.failed is not None
    rest = {
        0x53: ("calibration.ch3.range_mode", "calibration_gas.ch3.range1.span"),
        0x20: ("calibration_gas.ch3.range1.span",),
    }
    assert report.not_attempted == rest[at]
    assert 0x09 not in writes_sent(mock)
    if fault is FaultKind.EXCEPTION:
        assert isinstance(report.error, FujiModbusIllegalDataValueError)


async def test_a_write_whose_outcome_is_unknown_stops_the_apply() -> None:
    mock = bench()

    def lose_read_backs(request: Any) -> None:
        if request.function == FC06:
            mock.inject(FaultKind.DROP, times=None, when=when(FC03, 0x53))

    async with analyzer_on(mock) as (anz, _):
        mock.inject(FaultKind.DROP, when=when(FC06, 0x53))
        mock.on_request = lose_read_backs
        report = await anz.apply_settings(document({"response_time.o2": 16}), confirm=True)
    assert report.status is ApplyStatus.UNKNOWN
    assert report.failed == "response_time.o2"
