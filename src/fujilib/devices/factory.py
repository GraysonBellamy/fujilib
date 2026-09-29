"""``open_device``: the entry point (design §7.1, unified API §A).

It opens the serial port (or takes the caller's open transport), binds one
Modbus bus to it, attaches a session to the station, and by default
identifies the analyzer. If anything fails or is cancelled on the way, what
this call opened is closed again; a transport the caller passed in is never
closed by it.

A port opened by name can be opened again the same way after a connection
failure (``Analyzer.reopen()``); a transport the caller passed in cannot.
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

import anyio
from anyserial import canonical_port_name

from fujilib.config import DEFAULTS
from fujilib.devices.analyzer import Analyzer
from fujilib.devices.capability import OPTION_CAPABILITIES, Capability
from fujilib.devices.profile import ZP_PROFILE
from fujilib.devices.session import Session
from fujilib.errors import ErrorContext, FujiConnectionError, FujiValidationError
from fujilib.protocol.base import ProtocolKind
from fujilib.protocol.modbus.port import MAX_STATION, MIN_STATION, ModbusPort
from fujilib.registry.channels import coerce_channel_map
from fujilib.transport.base import Transport
from fujilib.transport.serial import SerialTransport

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fujilib.devices.profile import DeviceProfile
    from fujilib.devices.session import Reopener
    from fujilib.registry.channels import ChannelId, Gas
    from fujilib.transport.base import SerialSettings

__all__ = ["open_device"]


async def open_device(
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
) -> Analyzer:
    r"""Open the analyzer at station ``address`` on ``port``.

    Use the result as an async context manager, or call ``close()``::

        async with await open_device("COM8", channel_map={"CH3": "o2"}) as anz:
            frame = await anz.poll()

    Args:
        port: A serial port name (``"COM8"``, ``"/dev/ttyUSB0"``; any spelling
            of a port, such as ``\\.\COM8``, names the same port), or an open
            :class:`~fujilib.transport.base.Transport`, which stays the
            caller's to close.
        profile: The analyzer family.
        protocol: The wire protocol; the profile's (MODBUS RTU) when ``None``.
        address: The station number, 1-31, as set on the front panel.
        serial_settings: The serial framing, with ``port`` agreeing with
            ``port``; the profile's (38400 8-N-1) when ``None``. Not accepted
            with an open transport, whose settings are fixed already.
        timeout: Seconds to wait for each reply. A per-call ``timeout=`` on
            the analyzer's methods is a deadline for a whole operation instead.
        identify: Identify the analyzer before returning (six transactions).
        channel_map: The gas on each channel, e.g. ``{"CH1": "co2", "CH3":
            "o2"}``. Only an asserted label is fit for calculation (design §2.9).
        options: Options the analyzer has, whatever its type code says, e.g.
            ``Capability.AUTO_CALIBRATION | Capability.AUTO_ZERO`` for a unit
            whose calibration gases are plumbed. The type code only suggests
            them, like gas labels (design §6.1).
        write_warn_per_minute: Setting writes a minute above which a warning is
            logged; 0 for never (design §6.3).

    Raises:
        FujiValidationError: an argument is invalid; nothing was opened.
        FujiConnectionError: the port cannot be opened, or the transport is closed.
        FujiConfigurationError: the transport already carries an open analyzer.
        FujiProtocolUnsupportedError: the station does not identify as a ZP analyzer.
        FujiError: identification failed.
    """
    _check_protocol(profile, protocol)
    _check_address(address)
    _check_options(options, write_warn_per_minute)
    asserted = coerce_channel_map(channel_map) if channel_map is not None else None
    reopener: Reopener | None = None
    if isinstance(port, str):
        settings = _serial_settings(profile, port, serial_settings)
        transport: Transport = await SerialTransport.open(settings)
        owns_transport = True
        reopener = partial(_open_port, settings, timeout)
    else:
        if not _is_transport(port):
            msg = f"port must be a port name or an open Transport, got {type(port).__name__}"
            raise FujiValidationError(msg)
        if serial_settings is not None:
            msg = "serial_settings cannot be given with an open transport"
            raise FujiValidationError(msg, context=ErrorContext(port=port.label))
        if not port.is_open:
            msg = f"the transport of {port.label} is closed"
            raise FujiConnectionError(msg, context=ErrorContext(port=port.label))
        transport, owns_transport = port, False

    modbus: ModbusPort | None = None
    try:
        modbus = ModbusPort(transport, request_timeout=timeout, owns_transport=owns_transport)
        session = Session(
            modbus,
            address=address,
            profile=profile,
            channel_map=asserted,
            reopener=reopener,
            options=options,
            write_warn_per_minute=write_warn_per_minute,
        )
        analyzer = Analyzer(session)
        if identify:
            await analyzer.identify()
    except BaseException:
        with anyio.CancelScope(shield=True):
            if modbus is not None:
                await modbus.aclose()
            elif owns_transport:
                await transport.aclose()
        raise
    return analyzer


async def _open_port(settings: SerialSettings, timeout: float) -> ModbusPort:
    """Open the port of ``settings`` and bind a Modbus port to it, as ``open_device`` does."""
    transport = await SerialTransport.open(settings)
    try:
        return ModbusPort(transport, request_timeout=timeout, owns_transport=True)
    except BaseException:
        with anyio.CancelScope(shield=True):
            await transport.aclose()
        raise


def _check_protocol(profile: DeviceProfile, protocol: ProtocolKind | str | None) -> None:
    if protocol is None:
        return
    try:
        kind = ProtocolKind(protocol)
    except ValueError:
        kind = None
    if kind is not profile.default_protocol:
        expected = profile.default_protocol.value
        msg = f"the {profile.name} analyzers speak only {expected}, got {protocol!r}"
        raise FujiValidationError(msg)


def _is_transport(value: object) -> bool:
    return isinstance(value, Transport)


def _check_options(options: object, write_warn_per_minute: object) -> None:
    if not isinstance(options, Capability) or options & ~OPTION_CAPABILITIES:
        msg = f"options must be option capabilities, e.g. Capability.AUTO_ZERO; got {options!r}"
        raise FujiValidationError(msg)
    if not (
        isinstance(write_warn_per_minute, int)
        and not isinstance(write_warn_per_minute, bool)
        and write_warn_per_minute >= 0
    ):
        msg = (
            f"write_warn_per_minute must be a whole number 0 or more, got {write_warn_per_minute!r}"
        )
        raise FujiValidationError(msg)


def _check_address(address: int) -> None:
    if not _is_int(address):
        msg = f"address must be an integer, got {address!r}"
        raise FujiValidationError(msg)
    if not MIN_STATION <= address <= MAX_STATION:
        msg = f"address must be a station number {MIN_STATION}-{MAX_STATION}, got {address}"
        raise FujiValidationError(msg, context=ErrorContext(address=address))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _serial_settings(
    profile: DeviceProfile, port: str, settings: SerialSettings | None
) -> SerialSettings:
    if settings is None:
        return replace(profile.default_serial, port=port)
    if not settings.port.strip():
        return replace(settings, port=port)
    if canonical_port_name(settings.port.strip()) != canonical_port_name(port.strip()):
        msg = f"serial_settings name port {settings.port!r}, but port is {port!r}"
        raise FujiValidationError(msg, context=ErrorContext(port=port))
    return settings
