"""The blocking analyzer facade: :class:`SyncAnalyzer` and :meth:`Fuji.open` (design §7.3).

Each method is a one-line blocking call of the :class:`~fujilib.devices.analyzer.Analyzer`
method of the same name, with the same parameters and defaults (a parity
test holds them together)::

    from fujilib.sync import Fuji

    with Fuji.open("COM8", channel_map={"CH3": "o2"}) as anz:
        print(anz.poll().channel("CH3"))
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Self

from fujilib.config import DEFAULTS
from fujilib.devices.capability import Capability, SafetyTier
from fujilib.devices.factory import open_device
from fujilib.devices.profile import ZP_PROFILE
from fujilib.sync.portal import SyncPortal

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Mapping
    from types import TracebackType

    from fujilib.devices.analyzer import Analyzer
    from fujilib.devices.capability import Availability
    from fujilib.devices.decode import RegisterValue
    from fujilib.devices.models import (
        AdcValues,
        AnalyzerMetadata,
        AnalyzerStatus,
        CalibrationLogEntry,
        ChannelInfo,
        ChannelStatus,
        DeviceInfo,
        ErrorLogEntry,
        Frame,
        RangeInfo,
        Reading,
    )
    from fujilib.devices.operations import (
        CalibrationPlan,
        CalibrationStatus,
        CalibrationWait,
        CommandResult,
    )
    from fujilib.devices.panel import (
        ManualCalibrationEvent,
        ManualCalibrationKind,
        ManualCalibrationPlan,
    )
    from fujilib.devices.profile import DeviceProfile
    from fujilib.devices.reads import ClockReading
    from fujilib.devices.session import Session
    from fujilib.devices.settings import ApplyReport, SettingsDiff, SettingsDocument
    from fujilib.devices.snapshot import FujiDeviceSnapshot
    from fujilib.devices.writes import WriteResult
    from fujilib.protocol.base import ProtocolKind
    from fujilib.registry.channels import ChannelId, Gas
    from fujilib.registry.enums import HoldMode, RangeMethod
    from fujilib.registry.units import Unit
    from fujilib.transport.base import SerialSettings, Transport

__all__ = ["Fuji", "SyncAnalyzer"]


class SyncAnalyzer:
    """A blocking view of an :class:`~fujilib.devices.analyzer.Analyzer`, bound to a portal."""

    def __init__(self, analyzer: Analyzer, portal: SyncPortal) -> None:
        """Wrap ``analyzer``, whose loop is ``portal``'s; :meth:`Fuji.open` does this."""
        self._anz = analyzer
        self._portal = portal

    # --- Lifecycle -------------------------------------------------------------------------

    def close(self) -> None:
        """Blocking :meth:`Analyzer.close`."""
        self._portal.call(self._anz.close)

    def reopen(self, *, timeout: float | None = None) -> DeviceInfo:
        """Blocking :meth:`Analyzer.reopen`."""
        return self._portal.call(self._anz.reopen, timeout=timeout)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- What is known, without I/O ------------------------------------------------------

    @property
    def analyzer(self) -> Analyzer:
        """The async analyzer; its coroutines must run on :attr:`portal`."""
        return self._anz

    @property
    def portal(self) -> SyncPortal:
        """The portal the analyzer's loop runs in."""
        return self._portal

    @property
    def session(self) -> Session:
        """:attr:`Analyzer.session`."""
        return self._anz.session

    @property
    def info(self) -> DeviceInfo | None:
        """:attr:`Analyzer.info`."""
        return self._anz.info

    @property
    def channels(self) -> tuple[ChannelInfo, ...]:
        """:attr:`Analyzer.channels`."""
        return self._anz.channels

    @property
    def address(self) -> int:
        """:attr:`Analyzer.address`."""
        return self._anz.address

    @property
    def port(self) -> str:
        """:attr:`Analyzer.port`."""
        return self._anz.port

    @property
    def options(self) -> Capability:
        """:attr:`Analyzer.options`."""
        return self._anz.options

    @property
    def protocol(self) -> ProtocolKind:
        """:attr:`Analyzer.protocol`."""
        return self._anz.protocol

    @property
    def last_frame(self) -> Frame | None:
        """:attr:`Analyzer.last_frame`."""
        return self._anz.last_frame

    def snapshot(self, *, name: str | None = None) -> FujiDeviceSnapshot:
        """Blocking :meth:`Analyzer.snapshot`."""
        return self._portal.call(self._anz.snapshot, name=name)

    # --- Identity ------------------------------------------------------------------------

    def identify(
        self,
        *,
        channel_map: Mapping[ChannelId | str, Gas | str] | None = None,
        timeout: float | None = None,
    ) -> DeviceInfo:
        """Blocking :meth:`Analyzer.identify`."""
        return self._portal.call(self._anz.identify, channel_map=channel_map, timeout=timeout)

    def read_ranges(self, *, timeout: float | None = None) -> tuple[RangeInfo, ...]:
        """Blocking :meth:`Analyzer.read_ranges`."""
        return self._portal.call(self._anz.read_ranges, timeout=timeout)

    def read_metadata(self, *, timeout: float | None = None) -> AnalyzerMetadata:
        """Blocking :meth:`Analyzer.read_metadata`."""
        return self._portal.call(self._anz.read_metadata, timeout=timeout)

    # --- Measurements --------------------------------------------------------------------

    def poll(self, *, detail: bool = True, timeout: float | None = None) -> Frame:
        """Blocking :meth:`Analyzer.poll`."""
        return self._portal.call(self._anz.poll, detail=detail, timeout=timeout)

    def read_channel(self, channel: ChannelId | str, *, timeout: float | None = None) -> Reading:
        """Blocking :meth:`Analyzer.read_channel`."""
        return self._portal.call(self._anz.read_channel, channel, timeout=timeout)

    def status(self, *, timeout: float | None = None) -> AnalyzerStatus:
        """Blocking :meth:`Analyzer.status`."""
        return self._portal.call(self._anz.status, timeout=timeout)

    def channel_status(
        self, channel: ChannelId | str, *, timeout: float | None = None
    ) -> ChannelStatus:
        """Blocking :meth:`Analyzer.channel_status`."""
        return self._portal.call(self._anz.channel_status, channel, timeout=timeout)

    # --- Diagnostics ---------------------------------------------------------------------

    def read_clock(self, *, timeout: float | None = None) -> ClockReading:
        """Blocking :meth:`Analyzer.read_clock`."""
        return self._portal.call(self._anz.read_clock, timeout=timeout)

    def read_adc(self, *, timeout: float | None = None) -> AdcValues:
        """Blocking :meth:`Analyzer.read_adc`."""
        return self._portal.call(self._anz.read_adc, timeout=timeout)

    def reprobe(self, capability: Capability, *, timeout: float | None = None) -> Availability:
        """Blocking :meth:`Analyzer.reprobe`."""
        return self._portal.call(self._anz.reprobe, capability, timeout=timeout)

    # --- Logs ----------------------------------------------------------------------------

    def read_error_log(self, *, timeout: float | None = None) -> tuple[ErrorLogEntry, ...]:
        """Blocking :meth:`Analyzer.read_error_log`."""
        return self._portal.call(self._anz.read_error_log, timeout=timeout)

    def read_calibration_log(
        self, channel: ChannelId | str | None = None, *, timeout: float | None = None
    ) -> tuple[CalibrationLogEntry, ...]:
        """Blocking :meth:`Analyzer.read_calibration_log`."""
        return self._portal.call(self._anz.read_calibration_log, channel, timeout=timeout)

    # --- Parameters ----------------------------------------------------------------------

    def read_parameter(
        self,
        name: str,
        *,
        alarm_targets: Mapping[int, ChannelId | str] | None = None,
        timeout: float | None = None,
    ) -> RegisterValue:
        """Blocking :meth:`Analyzer.read_parameter`."""
        return self._portal.call(
            self._anz.read_parameter, name, alarm_targets=alarm_targets, timeout=timeout
        )

    def read_parameters(
        self,
        names: Iterable[str],
        *,
        alarm_targets: Mapping[int, ChannelId | str] | None = None,
        timeout: float | None = None,
    ) -> Mapping[str, RegisterValue]:
        """Blocking :meth:`Analyzer.read_parameters`."""
        return self._portal.call(
            self._anz.read_parameters, names, alarm_targets=alarm_targets, timeout=timeout
        )

    def read_settings(
        self,
        *,
        alarm_targets: Mapping[int, ChannelId | str] | None = None,
        timeout: float | None = None,
    ) -> Mapping[str, RegisterValue]:
        """Blocking :meth:`Analyzer.read_settings`."""
        return self._portal.call(
            self._anz.read_settings, alarm_targets=alarm_targets, timeout=timeout
        )

    # --- Settings ------------------------------------------------------------------------

    def write_parameter(
        self,
        name: str,
        value: object,
        *,
        unit: Unit | str | None = None,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> WriteResult:
        """Blocking :meth:`Analyzer.write_parameter`."""
        return self._portal.call(
            self._anz.write_parameter, name, value, unit=unit, confirm=confirm, timeout=timeout
        )

    def set_response_time(
        self,
        target: ChannelId | str,
        seconds: int,
        *,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> WriteResult:
        """Blocking :meth:`Analyzer.set_response_time`."""
        return self._portal.call(
            self._anz.set_response_time, target, seconds, confirm=confirm, timeout=timeout
        )

    def set_output_hold(
        self, enabled: bool, *, confirm: bool = False, timeout: float | None = None
    ) -> WriteResult:
        """Blocking :meth:`Analyzer.set_output_hold`."""
        return self._portal.call(
            self._anz.set_output_hold, enabled, confirm=confirm, timeout=timeout
        )

    def set_hold_mode(
        self, mode: HoldMode | str, *, confirm: bool = False, timeout: float | None = None
    ) -> WriteResult:
        """Blocking :meth:`Analyzer.set_hold_mode`."""
        return self._portal.call(self._anz.set_hold_mode, mode, confirm=confirm, timeout=timeout)

    def set_hold_value(
        self,
        channel: ChannelId | str,
        percent_fs: int,
        *,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> WriteResult:
        """Blocking :meth:`Analyzer.set_hold_value`."""
        return self._portal.call(
            self._anz.set_hold_value, channel, percent_fs, confirm=confirm, timeout=timeout
        )

    def set_range(
        self,
        channel: ChannelId | str,
        range_number: int,
        *,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> WriteResult:
        """Blocking :meth:`Analyzer.set_range`."""
        return self._portal.call(
            self._anz.set_range, channel, range_number, confirm=confirm, timeout=timeout
        )

    def set_range_method(
        self,
        channel: ChannelId | str,
        method: RangeMethod | str,
        *,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> WriteResult:
        """Blocking :meth:`Analyzer.set_range_method`."""
        return self._portal.call(
            self._anz.set_range_method, channel, method, confirm=confirm, timeout=timeout
        )

    def set_calibration_gas(
        self,
        channel: ChannelId | str,
        range_number: int,
        kind: str,
        value: float | str,
        *,
        unit: Unit | str,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> WriteResult:
        """Blocking :meth:`Analyzer.set_calibration_gas`."""
        return self._portal.call(
            self._anz.set_calibration_gas,
            channel,
            range_number,
            kind,
            value,
            unit=unit,
            confirm=confirm,
            timeout=timeout,
        )

    def diff_settings(
        self,
        document: SettingsDocument | Mapping[str, object],
        *,
        any_analyzer: bool = False,
        timeout: float | None = None,
    ) -> SettingsDiff:
        """Blocking :meth:`Analyzer.diff_settings`."""
        return self._portal.call(
            self._anz.diff_settings, document, any_analyzer=any_analyzer, timeout=timeout
        )

    def apply_settings(
        self,
        document: SettingsDocument | Mapping[str, object],
        *,
        confirm: bool = False,
        any_analyzer: bool = False,
        max_tier: SafetyTier = SafetyTier.DANGEROUS,
        timeout: float | None = None,
    ) -> ApplyReport:
        """Blocking :meth:`Analyzer.apply_settings`."""
        return self._portal.call(
            self._anz.apply_settings,
            document,
            confirm=confirm,
            any_analyzer=any_analyzer,
            max_tier=max_tier,
            timeout=timeout,
        )

    # --- Operations ----------------------------------------------------------------------

    def calibration_status(self, *, timeout: float | None = None) -> CalibrationStatus:
        """Blocking :meth:`Analyzer.calibration_status`."""
        return self._portal.call(self._anz.calibration_status, timeout=timeout)

    def plan_auto_calibration(self, *, timeout: float | None = None) -> CalibrationPlan:
        """Blocking :meth:`Analyzer.plan_auto_calibration`."""
        return self._portal.call(self._anz.plan_auto_calibration, timeout=timeout)

    def plan_auto_zero_calibration(self, *, timeout: float | None = None) -> CalibrationPlan:
        """Blocking :meth:`Analyzer.plan_auto_zero_calibration`."""
        return self._portal.call(self._anz.plan_auto_zero_calibration, timeout=timeout)

    def start_auto_calibration(
        self, *, confirm: bool = False, timeout: float | None = None
    ) -> CommandResult:
        """Blocking :meth:`Analyzer.start_auto_calibration`."""
        return self._portal.call(self._anz.start_auto_calibration, confirm=confirm, timeout=timeout)

    def start_auto_zero_calibration(
        self, *, confirm: bool = False, timeout: float | None = None
    ) -> CommandResult:
        """Blocking :meth:`Analyzer.start_auto_zero_calibration`."""
        return self._portal.call(
            self._anz.start_auto_zero_calibration, confirm=confirm, timeout=timeout
        )

    def start_blowback(
        self, *, confirm: bool = False, timeout: float | None = None
    ) -> CommandResult:
        """Blocking :meth:`Analyzer.start_blowback`."""
        return self._portal.call(self._anz.start_blowback, confirm=confirm, timeout=timeout)

    def return_to_measurement(
        self, *, confirm: bool = False, timeout: float | None = None
    ) -> CommandResult:
        """Blocking :meth:`Analyzer.return_to_measurement`."""
        return self._portal.call(self._anz.return_to_measurement, confirm=confirm, timeout=timeout)

    def wait_for_calibration(
        self,
        *,
        timeout: float,
        interval: float = 2.0,
        since: CalibrationStatus | None = None,
    ) -> CalibrationWait:
        """Blocking :meth:`Analyzer.wait_for_calibration`."""
        return self._portal.call(
            self._anz.wait_for_calibration, timeout=timeout, interval=interval, since=since
        )

    def plan_manual_calibration(
        self,
        channel: ChannelId | str,
        kind: ManualCalibrationKind | str,
        *,
        timeout: float | None = None,
    ) -> ManualCalibrationPlan:
        """Blocking :meth:`Analyzer.plan_manual_calibration`."""
        return self._portal.call(self._anz.plan_manual_calibration, channel, kind, timeout=timeout)

    def wait_for_manual_calibration(
        self, *, timeout: float, interval: float = 0.5, adc: bool = False
    ) -> ManualCalibrationEvent:
        """Blocking :meth:`Analyzer.wait_for_manual_calibration`."""
        return self._portal.call(
            self._anz.wait_for_manual_calibration, timeout=timeout, interval=interval, adc=adc
        )

    def __repr__(self) -> str:
        return "<Sync" + repr(self._anz).removeprefix("<")


class Fuji:
    """The sync entry point: ``with Fuji.open(...) as anz:``."""

    @staticmethod
    @contextmanager
    def open(
        port: str | Transport,
        *,
        profile: DeviceProfile = ZP_PROFILE,
        protocol: ProtocolKind | str | None = None,
        address: int = 1,
        serial_settings: SerialSettings | None = None,
        timeout: float = DEFAULTS.request_timeout_s,
        identify: bool = True,
        channel_map: Mapping[ChannelId | str, Gas | str] | None = None,
        options: Capability = Capability.NONE,
        write_warn_per_minute: int = DEFAULTS.write_warn_per_minute,
        portal: SyncPortal | None = None,
    ) -> Generator[SyncAnalyzer]:
        """Open an analyzer for the ``with`` block; the arguments are :func:`open_device`'s.

        Without ``portal`` the analyzer gets a portal of its own, closed with
        it. A :class:`~fujilib.transport.base.Transport` passed as ``port``
        must belong to ``portal``'s loop.
        """
        with ExitStack() as stack:
            active = portal if portal is not None else stack.enter_context(SyncPortal())
            analyzer = active.call(
                open_device,
                port,
                profile=profile,
                protocol=protocol,
                address=address,
                serial_settings=serial_settings,
                timeout=timeout,
                identify=identify,
                channel_map=channel_map,
                options=options,
                write_warn_per_minute=write_warn_per_minute,
            )
            stack.callback(active.call, analyzer.close)
            yield SyncAnalyzer(analyzer, active)
