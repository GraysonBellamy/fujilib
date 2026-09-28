"""Pure decoders against the bench unit's words, and the validity rule (design §2.5-§2.9, §8)."""

from __future__ import annotations

import dataclasses
from datetime import datetime

import pytest

from fujilib.devices.decode import (
    decode_adc,
    decode_analyzer_status,
    decode_calibration_log,
    decode_channel_status,
    decode_clock,
    decode_error_log,
    decode_frame,
    decode_identity,
    decode_metadata,
    decode_ranges,
    decode_register,
    derive_states,
    label_channels,
    nonzero_channels,
    range_scaling,
    words_of,
)
from fujilib.devices.models import ChannelInfo, ReadingState
from fujilib.errors import FujiDecodeError
from fujilib.protocol.modbus.codec import encode_bcd, encode_uint32_lh
from fujilib.registry.channels import ChannelId, ChannelRole, Gas, LabelSource
from fujilib.registry.enums import (
    AlarmState,
    CalibrationKind,
    DayOfWeek,
    DisplayScreen,
    ErrorCode,
    PeriodUnit,
    ScheduleCycleUnit,
)
from fujilib.registry.registers import CALIBRATION_LOG, REGISTRY, Scaling, ScalingKind
from fujilib.registry.units import Unit
from tests.factories import T0, analyzer, bench_banks, status, timing

HOLDING, INPUT = bench_banks()
CH1, CH2, CH3 = ChannelId.CH1, ChannelId.CH2, ChannelId.CH3
ASSERTED = {CH1: Gas.CO2, CH2: Gas.CO, CH3: Gas.O2}


def bench_channels() -> tuple[ChannelInfo, ...]:
    type_code, _serial = decode_identity(INPUT)
    return label_channels(nonzero_channels(INPUT), asserted=ASSERTED, type_code=type_code)


# --- Identity, ranges, presence, labels -----------------------------------------------------


def test_bench_identity() -> None:
    type_code, serial = decode_identity(INPUT)
    assert type_code.raw == "ZPACBJY1MPFYYYYYY2DEYAYAY0"
    assert serial == "N8A0259T"


def test_identity_reads_the_extension_digits_when_present() -> None:
    bank = dict(INPUT)
    bank.update({0x47A: ord("A"), 0x47B: ord("B"), 0x47C: 0})
    type_code, _ = decode_identity(bank)
    assert type_code.raw == "ZPACBJY1MPFYYYYYY2DEYAYAY0AB"


def test_bench_ranges() -> None:
    ranges = {r.channel: r for r in decode_ranges(INPUT)}
    assert ranges[CH1].count == 1
    assert ranges[CH1].of(1) == (Unit.VOL_PERCENT, 10.0, 2)
    assert ranges[CH2].of(1) == (Unit.VOL_PERCENT, 1.0, 3)
    assert ranges[CH3].count == 2
    assert ranges[CH3].full_scale == (21.0, 25.0)
    # Unused channels still report ranges (design §2.9).
    assert ranges[ChannelId.CH4].count == 2
    assert ranges[ChannelId.CH4].units == (Unit.PPM, Unit.PPM)


def test_bad_range_decimal_point_gives_nan() -> None:
    bank = dict(INPUT)
    bank[0x43E] = 9
    assert str(decode_ranges(bank)[0].full_scale[0]) == "nan"


def test_bench_presence() -> None:
    assert nonzero_channels(INPUT) == {CH1, CH2, CH3}


def test_asserted_labels_win() -> None:
    infos = {c.channel: c for c in bench_channels()}
    assert set(infos) == {CH1, CH2, CH3}
    for channel, gas in ASSERTED.items():
        assert infos[channel].gas is gas
        assert infos[channel].label_source is LabelSource.ASSERTED
        assert infos[channel].role is ChannelRole.INSTANTANEOUS
    assert infos[CH3].suggested_gas is Gas.O2


def test_unasserted_labels_are_only_suggested() -> None:
    type_code, _ = decode_identity(INPUT)
    infos = {c.channel: c for c in label_channels(nonzero_channels(INPUT), type_code=type_code)}
    assert infos[CH1].gas is Gas.UNKNOWN
    assert infos[CH1].suggested_gas is Gas.CO2
    assert infos[CH1].label_source is LabelSource.TYPE_CODE
    assert infos[CH3].suggested_gas is Gas.O2
    assert infos[CH3].label_source is LabelSource.INFERRED


def test_an_asserted_channel_is_established_even_at_zero() -> None:
    infos = label_channels([], asserted={ChannelId.CH4: Gas.CH4})
    assert [c.channel for c in infos] == [ChannelId.CH4]
    assert infos[0].role is ChannelRole.INSTANTANEOUS
    unlabelled = label_channels([ChannelId.CH7])
    assert unlabelled[0].role is ChannelRole.UNKNOWN
    assert unlabelled[0].label_source is LabelSource.UNKNOWN


# --- Poll frames -------------------------------------------------------------------------


def test_bench_frame() -> None:
    frame = decode_frame(
        INPUT, bench_channels(), readings_timing=timing(0), status_timing=timing(25)
    )
    assert frame.channel("CH1").value == -0.11
    assert frame.channel("CH2").value == -0.009
    assert frame.channel("CH3").value == 20.3
    assert frame.channel("CH3").raw_value == 2030
    assert all(r.state is ReadingState.OK for r in frame.readings)
    assert frame.channel("CH3").status is not None
    assert frame.channel("CH3").status.range == 1  # type: ignore[union-attr]
    assert frame.analyzer is not None
    assert frame.analyzer.alarms == (AlarmState.NONE,) * 6
    assert frame.analyzer.display is not None
    assert frame.analyzer.display.screen is DisplayScreen.MEASUREMENT
    assert frame.analyzer.display.cursor_channel is CH3  # 00BCh reads 2


def test_readings_only_frame_has_unknown_validity() -> None:
    frame = decode_frame(INPUT, bench_channels(), readings_timing=timing(0))
    assert frame.analyzer is None
    assert frame.status_timing is None
    assert all(r.state is ReadingState.UNKNOWN and r.valid is None for r in frame.readings)
    assert all(r.status is None for r in frame.readings)


def test_an_undecodable_triple_does_not_fail_the_poll() -> None:
    bank = dict(INPUT)
    bank[0x0007] = 7  # CH3 decimal point
    frame = decode_frame(
        bank, bench_channels(), readings_timing=timing(0), status_timing=timing(25)
    )
    assert frame.channel("CH3").value is None
    assert frame.channel("CH3").state is ReadingState.UNKNOWN
    assert frame.channel("CH1").state is ReadingState.OK


def test_status_decoding() -> None:
    bank = dict(INPUT)
    bank.update(
        {0xB3: 1, 0x087 + 6 * 4 + 2: 1, 0x0029: 1, 0x0083: 1}
    )  # CH5 hold, CH5 e6, range 2, e1
    ch5 = decode_channel_status(bank, ChannelId.CH5)
    assert ch5.hold
    assert ch5.errors == {ErrorCode.SPAN_OUT_OF_RANGE}
    assert ch5.range == 2
    assert decode_analyzer_status(bank).errors == {ErrorCode.LIGHT_SOURCE}
    with pytest.raises(FujiDecodeError):
        decode_channel_status(bank, ChannelId.CH6)


# --- Validity rule -------------------------------------------------------------------------

MEASURED = (
    ChannelInfo(CH1, Gas.NOX, None, ChannelRole.INSTANTANEOUS, LabelSource.ASSERTED),
    ChannelInfo(CH2, Gas.O2, None, ChannelRole.INSTANTANEOUS, LabelSource.ASSERTED),
)
CORRECTED = ChannelInfo(
    CH3, Gas.NOX, None, ChannelRole.O2_CORRECTED, LabelSource.TYPE_CODE, derived_from=CH1
)
AVERAGE_O2 = ChannelInfo(ChannelId.CH12, Gas.O2, None, ChannelRole.O2_AVERAGE, LabelSource.ASSERTED)
MYSTERY = ChannelInfo(ChannelId.CH9, Gas.UNKNOWN, None, ChannelRole.UNKNOWN, LabelSource.UNKNOWN)


@pytest.mark.parametrize(
    ("ch1", "status_kw", "expected"),
    [
        (status(), {}, ReadingState.OK),
        (status(hold=True), {}, ReadingState.HOLD),
        (status(zero=True), {}, ReadingState.CALIBRATING),
        (status(errors=(ErrorCode.UNSTABLE,)), {}, ReadingState.CHANNEL_ERROR),
        (status(), {"auto_calibration": True}, ReadingState.AUTO_CALIBRATION),
        (status(), {"instrument_error": True}, ReadingState.ANALYZER_ERROR),
        (status(), {"errors": (ErrorCode.OUTPUT_CIRCUIT,)}, ReadingState.ANALYZER_ERROR),
        # Precedence: the analyzer error wins over a held, erroring channel.
        (
            status(hold=True, errors=(ErrorCode.UNSTABLE,)),
            {"instrument_error": True},
            ReadingState.ANALYZER_ERROR,
        ),
        (status(hold=True, errors=(ErrorCode.UNSTABLE,)), {}, ReadingState.CHANNEL_ERROR),
    ],
)
def test_measured_channel_states(
    ch1: object, status_kw: dict[str, object], expected: ReadingState
) -> None:
    states = derive_states(MEASURED, {CH1: ch1, CH2: status()}, analyzer(**status_kw))  # type: ignore[dict-item, arg-type]
    assert states[CH1] is expected


def test_derived_channel_follows_its_source_and_o2() -> None:
    channels = (*MEASURED, CORRECTED, AVERAGE_O2, MYSTERY)
    ok = derive_states(channels, {CH1: status(), CH2: status()}, analyzer())
    assert all(s is ReadingState.OK for s in ok.values())
    held_source = derive_states(channels, {CH1: status(hold=True), CH2: status()}, analyzer())
    assert held_source[CORRECTED.channel] is ReadingState.SOURCE_INVALID
    assert held_source[AVERAGE_O2.channel] is ReadingState.OK  # depends on O2 only
    assert held_source[MYSTERY.channel] is ReadingState.SOURCE_INVALID  # source unknown
    bad_o2 = derive_states(channels, {CH1: status(), CH2: status(zero=True)}, analyzer())
    assert bad_o2[CORRECTED.channel] is ReadingState.SOURCE_INVALID
    assert bad_o2[AVERAGE_O2.channel] is ReadingState.SOURCE_INVALID


def test_undecodable_and_readings_only_states() -> None:
    states = derive_states(MEASURED, {CH1: status(), CH2: status()}, analyzer(), undecodable=[CH2])
    assert states[CH2] is ReadingState.UNKNOWN
    assert set(derive_states(MEASURED, {}, None).values()) == {ReadingState.UNKNOWN}


# --- Logs -------------------------------------------------------------------------------------


def test_bench_error_log() -> None:
    log = decode_error_log(INPUT)
    assert len(log) == 14  # the bench log is full
    newest = log[0]
    assert newest.code is ErrorCode.SPAN_OUT_OF_RANGE
    assert newest.channel is CH1
    assert (newest.at.month, newest.at.day, newest.at.hour, newest.at.minute) == (None, 6, 15, 49)
    assert {e.code for e in log} <= {5, 6, 7}


def test_error_log_skips_empty_entries_and_analyzer_errors_have_no_channel() -> None:
    bank = dict(INPUT)
    bank.update({0x3D: 0xFFFF, 0x42: 2, 0x46: 0})  # entry 0 empty; entry 1 is error 3
    log = decode_error_log(bank)
    assert len(log) == 13
    assert log[0].code is ErrorCode.AD_CONVERSION
    assert log[0].channel is None


def calibration_log_bank(channel: int) -> dict[int, int]:
    bank: dict[int, int] = {}
    for index in range(CALIBRATION_LOG.records):
        base = CALIBRATION_LOG.record_address(channel, index)
        bank.update(dict.fromkeys(range(base, base + 9), 0xFFFF))
    base = CALIBRATION_LOG.record_address(channel, 0)
    low, high = encode_uint32_lh(70_123)
    record = (channel, 1, low, high, 0xFFF6, 9, 27, 14, 5)  # S1, deviation -1.0 %FS
    bank.update(zip(range(base, base + 9), record, strict=True))
    second = CALIBRATION_LOG.record_address(channel, 1)
    bank.update(zip(range(second, second + 9), (0x00FF, 0, 0, 0, 0, 0, 0, 0, 0), strict=True))
    return bank


def test_calibration_log() -> None:
    log = decode_calibration_log(calibration_log_bank(3), CH3)
    assert len(log) == 1
    entry = log[0]
    assert entry.channel is CH3
    assert entry.kind is CalibrationKind.SPAN_RANGE1
    assert entry.range == 1
    assert entry.detector_count == 70_123
    assert entry.deviation_percent_fs == -1.0
    assert (entry.at.month, entry.at.day, entry.at.hour, entry.at.minute) == (9, 27, 14, 5)
    with pytest.raises(FujiDecodeError):
        decode_calibration_log({}, ChannelId.CH6)


def test_calibration_log_keeps_an_undocumented_kind() -> None:
    bank = calibration_log_bank(1)
    bank[CALIBRATION_LOG.record_address(1, 0) + 1] = 5
    entry = decode_calibration_log(bank, CH1)[0]
    assert entry.kind == 5
    assert entry.range == 3


# --- Clock and A/D -------------------------------------------------------------------------------


def test_bench_clock() -> None:
    clock = decode_clock(words_of(INPUT, 0x3E8, 7))
    assert clock == datetime(2026, 9, 28, 11, 37, 44)  # a Monday, weekday register 1


@pytest.mark.parametrize(
    "words",
    [
        (0x26, 0x09, 0x28, 0x02, 0x11, 0x37, 0x44),  # weekday says Tuesday
        (0x26, 0x13, 0x28, 0x01, 0x11, 0x37, 0x44),  # month 13
        (0x26, 0x09, 0x28, 0x01, 0x0C, 0x37, 0x44),  # not BCD
        (0x26, 0x09, 0x28),
    ],
)
def test_invalid_clock(words: tuple[int, ...]) -> None:
    with pytest.raises(FujiDecodeError):
        decode_clock(words)


def test_bench_adc() -> None:
    adc = decode_adc(words_of(INPUT, 0x3EF, 42), received_at=T0, t_mono_ns=1)
    assert adc.reference_voltage == 38_929  # inside the service manual's 35,000-80,000
    assert adc.inputs[:2] == (65_725, 69_683)
    assert len(adc.raw) == 21
    assert len(adc.resistances) == 8
    with pytest.raises(FujiDecodeError):
        decode_adc((0,) * 40, received_at=T0, t_mono_ns=1)


# --- Registers and metadata ---------------------------------------------------------------


def test_decode_register_scaling_and_enums() -> None:
    ranges = decode_ranges(INPUT)
    span = REGISTRY.resolve("calibration_gas.ch3.range1.span")
    value = decode_register(
        span, words_of(HOLDING, span.address), scaling=range_scaling(span, ranges)
    )
    assert value.value == 20.95
    assert value.unit == "vol%"
    unscaled = decode_register(span, words_of(HOLDING, span.address))
    assert unscaled.value is None
    assert unscaled.raw == 2095
    mode = REGISTRY.resolve("auto_calibration.cycle_unit")
    assert decode_register(mode, words_of(HOLDING, mode.address)).value is ScheduleCycleUnit.DAYS
    coefficient = REGISTRY.resolve("interference.coefficient1")
    assert decode_register(coefficient, words_of(HOLDING, 0xA4, 2)).value == 1_000_000
    year = REGISTRY.resolve("clock.year")
    assert decode_register(year, (encode_bcd(26),)).value == 26


def test_alarm_limits_scale_only_with_a_known_target() -> None:
    ranges = decode_ranges(INPUT)
    limit = REGISTRY.resolve("alarm1.range1.high")
    assert range_scaling(limit, ranges) is None
    assert range_scaling(limit, ranges, alarm_targets={1: CH3}) == (2, Unit.VOL_PERCENT)
    assert range_scaling(REGISTRY.resolve("key_lock"), ranges) is None
    assert range_scaling(REGISTRY.resolve("calibration_gas.ch1.range2.span"), ranges[:1]) == (
        2,
        Unit.VOL_PERCENT,
    )


def test_range_scaling_without_the_channel_range() -> None:
    span = REGISTRY.resolve("calibration_gas.ch3.range2.span")
    assert range_scaling(span, ()) is None
    ch1_only = decode_ranges(INPUT)[:1]
    assert range_scaling(span, ch1_only) is None


def test_fixed_scaling() -> None:
    deviation = dataclasses.replace(
        REGISTRY.resolve("reading.ch1.value"),
        scaling=Scaling(ScalingKind.FIXED, 1),
    )
    assert decode_register(deviation, (0xFFF6,)).value == -1.0


def test_bench_metadata() -> None:
    ranges = decode_ranges(INPUT)
    meta = decode_metadata(
        HOLDING,
        serial_number="N8A0259T",
        ranges=ranges,
        channels=bench_channels(),
        current_range={CH1: 1, CH2: 1, CH3: 1},
        captured_at=T0,
    )
    assert meta.response_time_s == {CH1: 15, CH2: 15, CH3: 15}
    assert meta.response_time_o2_s == 15
    assert meta.calibration_gas[CH3, 1] == (0.0, 20.95)
    assert meta.calibration_gas[CH1, 1] == (0.0, 0.2)
    assert (CH1, 2) not in meta.calibration_gas  # CH1 has one range
    assert meta.auto_calibration.schedule.enabled is False
    assert meta.auto_calibration.schedule.start_day is DayOfWeek.SUNDAY
    assert meta.auto_calibration.schedule.start_hour_raw == 12  # contested encoding, kept raw
    assert meta.auto_calibration.schedule.cycle == 7
    assert meta.auto_calibration.schedule.cycle_unit is ScheduleCycleUnit.DAYS
    assert meta.auto_calibration.ranges[CH3] == 1
    assert meta.auto_calibration.flow_times_s == (300,) * 7
    assert meta.auto_zero.flow_time_s == 300
    assert meta.moving_average[0].unit is PeriodUnit.HOURS
    assert meta.output_hold is False
    assert meta.clock is None


def test_response_times_need_labels() -> None:
    unlabelled = tuple(
        dataclasses.replace(c, gas=Gas.UNKNOWN, suggested_gas=None) for c in bench_channels()
    )
    meta = decode_metadata(
        HOLDING,
        serial_number="x",
        ranges=decode_ranges(INPUT),
        channels=unlabelled,
        current_range={},
        captured_at=T0,
    )
    assert meta.response_time_s == {}
    assert meta.response_time_ndir_s == (15, 15, 15, 15)


def test_a_missing_word_is_a_decode_error() -> None:
    with pytest.raises(FujiDecodeError, match="0x00C2"):
        words_of(INPUT, 0x00C1, 2)
