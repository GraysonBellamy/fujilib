"""Finding analyzers on serial ports (design §7.5, unified API §B).

Discovery is read-only. Each station gets one FC04 read of type-code digits
1-3, which on a ZP analyzer name its model (``ZPA``, ``ZPB``, ...):

- a reply naming a model is an analyzer, identified in full unless asked not to;
- an exception reply, or characters that name no model, is a Modbus device
  that is not a ZP analyzer;
- silence is an empty address (or one whose station number differs).

The baud rate is fixed at 38400, so a port is one sweep of station numbers.
Ports are scanned concurrently; the stations of one port one at a time, on
one bus. An absent station costs the probe timeout plus the 0.1 s quiet
window after it, so a full sweep of 31 stations takes about 12 s per port.

Discovery never raises for a failed probe or a port that will not open; each
becomes a row with ``ok=False``. It raises only for invalid arguments.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import anyio
from anyserial import canonical_port_name, list_serial_ports

from fujilib.devices.profile import DEVICE_PROFILES
from fujilib.devices.session import describe_identity
from fujilib.errors import (
    ErrorContext,
    FujiConnectionError,
    FujiError,
    FujiModbusError,
    FujiModbusTimeoutError,
    FujiProtocolUnsupportedError,
    FujiValidationError,
)
from fujilib.protocol.modbus.codec import decode_chars
from fujilib.protocol.modbus.port import MAX_STATION, MIN_STATION, ModbusPort
from fujilib.transport.serial import SerialTransport

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from fujilib.devices.models import DeviceInfo
    from fujilib.devices.profile import DeviceProfile
    from fujilib.protocol.base import ProtocolKind
    from fujilib.protocol.modbus.client import ModbusClient

__all__ = ["DiscoveryResult", "DiscoverySummary", "find_devices", "summarize_discovery"]

#: ``anymodbus`` accepts request timeouts up to 60 s.
_MAX_PROBE_TIMEOUT = 60.0


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """One probed station (unified API §B).

    Attributes:
        ok: Whether the station answered as an analyzer of a known family.
        port: The canonical port name.
        address: The station number probed.
        baudrate: The baud rate used, or ``None`` when the port did not open.
        protocol: The wire protocol when ``ok``, else ``None``.
        device_info: The full identity when ``ok`` and identification succeeded.
        error: Why the station is not ``ok``; or, when it is, why its
            identification failed. ``None`` otherwise.
        elapsed_s: Seconds spent on the station.
        model: The model the probe named (``"ZPA"``), even without identification.
    """

    ok: bool
    port: str
    address: int | None
    baudrate: int | None
    protocol: ProtocolKind | None
    device_info: DeviceInfo | None
    error: FujiError | None
    elapsed_s: float
    model: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoverySummary:
    """What discovery found on one port.

    Attributes:
        port: The canonical port name.
        ok: Whether any analyzer answered.
        addresses: The stations that answered as analyzers.
        probed: How many stations were probed.
        error: When nothing answered: why the port did not open, or else the
            first probe's error. ``None`` when something answered.
        elapsed_s: Seconds spent on the port's stations, summed.
    """

    port: str
    ok: bool
    addresses: tuple[int, ...]
    probed: int
    error: FujiError | None
    elapsed_s: float


async def find_devices(
    *,
    ports: Sequence[str] | None = None,
    addresses: Sequence[int] = (1,),
    profiles: Sequence[DeviceProfile] = DEVICE_PROFILES,
    per_probe_timeout_s: float = 0.3,
    identify: bool = True,
    max_concurrency: int = 8,
) -> list[DiscoveryResult]:
    """Probe ``addresses`` on each of ``ports`` for analyzers; read-only.

    Args:
        ports: Port names; every serial port of the host when ``None``. Two
            spellings of one port (``COM8``, ``com8``) are scanned once. Every
            port listed receives the probe frames, so name only ports whose
            instruments may receive a Modbus read request.
        addresses: Station numbers to probe, 1-31.
        profiles: Analyzer families to try, in order.
        per_probe_timeout_s: Seconds to wait for each station's reply. Probes
            are not retried.
        identify: Identify each analyzer found (six more transactions each).
        max_concurrency: How many ports are scanned at once.

    Returns:
        One result per port and station, in the order given.

    Raises:
        FujiValidationError: an argument is invalid; nothing was sent.
        FujiConnectionError: ``ports`` is ``None`` and the host's ports cannot be listed.
    """
    stations = _check_addresses(addresses)
    if not profiles:
        msg = "profiles must name at least one analyzer family"
        raise FujiValidationError(msg)
    if not (math.isfinite(per_probe_timeout_s) and 0 < per_probe_timeout_s <= _MAX_PROBE_TIMEOUT):
        msg = (
            f"per_probe_timeout_s must be in (0, {_MAX_PROBE_TIMEOUT:g}] seconds, "
            f"got {per_probe_timeout_s!r}"
        )
        raise FujiValidationError(msg)
    if not _is_int(max_concurrency) or max_concurrency < 1:
        msg = f"max_concurrency must be a positive integer, got {max_concurrency!r}"
        raise FujiValidationError(msg)
    names = _check_ports(ports) if ports is not None else await _host_ports()

    results: list[list[DiscoveryResult]] = [[] for _ in names]
    limiter = anyio.CapacityLimiter(max_concurrency)

    async def scan(index: int, name: str) -> None:
        async with limiter:
            results[index] = await _scan_port(
                name, stations, profiles, per_probe_timeout_s, identify=identify
            )

    async with anyio.create_task_group() as tg:
        for index, name in enumerate(names):
            _ = tg.start_soon(scan, index, name)
    return [row for rows in results for row in rows]


def summarize_discovery(results: Iterable[DiscoveryResult]) -> list[DiscoverySummary]:
    """One summary per port, in the order the ports first appear."""
    by_port: dict[str, list[DiscoveryResult]] = {}
    for result in results:
        by_port.setdefault(result.port, []).append(result)
    summaries: list[DiscoverySummary] = []
    for port, rows in by_port.items():
        found = tuple(r.address for r in rows if r.ok and r.address is not None)
        error = None if found else next((r.error for r in rows if r.error is not None), None)
        summaries.append(
            DiscoverySummary(
                port=port,
                ok=bool(found),
                addresses=found,
                probed=len(rows),
                error=error,
                elapsed_s=sum(r.elapsed_s for r in rows),
            )
        )
    return summaries


def _check_addresses(addresses: Sequence[int]) -> tuple[int, ...]:
    if isinstance(addresses, (str, bytes)) or not addresses:
        msg = f"addresses must be a non-empty sequence of station numbers, got {addresses!r}"
        raise FujiValidationError(msg)
    for address in addresses:
        if not (_is_int(address) and MIN_STATION <= address <= MAX_STATION):
            msg = f"station numbers are {MIN_STATION}-{MAX_STATION}, got {address!r}"
            raise FujiValidationError(msg)
    return tuple(dict.fromkeys(addresses))


def _check_ports(ports: Sequence[str]) -> list[str]:
    """The port names, each spelling of one port kept once, in the order given."""
    if isinstance(ports, str):
        msg = "ports must be a sequence of port names, not one string"
        raise FujiValidationError(msg)
    unique: dict[str, str] = {}
    for name in ports:
        if not _is_text(name) or not name.strip():
            msg = f"port names must be non-empty strings, got {name!r}"
            raise FujiValidationError(msg)
        unique.setdefault(canonical_port_name(name.strip()), name)
    return list(unique.values())


def _is_text(value: object) -> bool:
    return isinstance(value, str)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


async def _host_ports() -> list[str]:
    try:
        infos = await list_serial_ports()
    except OSError as exc:
        msg = f"cannot list the host's serial ports: {exc}"
        raise FujiConnectionError(msg) from exc
    return [info.device for info in infos]


async def _scan_port(
    name: str,
    stations: tuple[int, ...],
    profiles: Sequence[DeviceProfile],
    probe_timeout: float,
    *,
    identify: bool,
) -> list[DiscoveryResult]:
    found: dict[int, DiscoveryResult] = {}
    for profile in profiles:
        remaining = [a for a in stations if a not in found or not found[a].ok]
        if not remaining:
            break
        for row in await _scan_with(name, remaining, profile, probe_timeout, identify=identify):
            found[row.address or 0] = row
    return [found[a] for a in stations]


async def _scan_with(
    name: str,
    stations: Sequence[int],
    profile: DeviceProfile,
    probe_timeout: float,
    *,
    identify: bool,
) -> list[DiscoveryResult]:
    started = anyio.current_time()
    try:
        transport = await SerialTransport.open(replace(profile.default_serial, port=name))
    except FujiError as exc:
        label = canonical_port_name(name.strip())
        share = (anyio.current_time() - started) / len(stations)
        return [DiscoveryResult(False, label, a, None, None, None, exc, share) for a in stations]
    async with ModbusPort(
        transport, request_timeout=probe_timeout, read_retries=0, owns_transport=True
    ) as port:
        return [await _probe(port, a, profile, identify=identify) for a in stations]


async def _probe(
    port: ModbusPort, address: int, profile: DeviceProfile, *, identify: bool
) -> DiscoveryResult:
    started = anyio.current_time()
    client = port.client(address)
    model, error = await _recognize(client, profile)
    info: DeviceInfo | None = None
    if model is not None and identify:
        try:
            identity = await profile.identify(client, probe=True)
            info = describe_identity(
                identity, address=address, serial_settings=port.transport.settings
            )
        except FujiError as exc:
            error = exc
    ok = model is not None
    return DiscoveryResult(
        ok=ok,
        port=port.label,
        address=address,
        baudrate=port.transport.settings.baudrate,
        protocol=profile.default_protocol if ok else None,
        device_info=info,
        error=error,
        elapsed_s=anyio.current_time() - started,
        model=model,
    )


async def _recognize(
    client: ModbusClient, profile: DeviceProfile
) -> tuple[str | None, FujiError | None]:
    """The model the station names, or why it names none."""
    try:
        reply = await client.read(profile.discovery_probe, command="discover")
    except FujiModbusTimeoutError as exc:
        return None, exc
    except FujiModbusError as exc:
        # A well-formed exception reply: something answers Modbus at this address.
        answer = "with a Modbus exception"
        return None, _not_ours(client, profile, answer, cause=exc)
    except FujiError as exc:
        return None, exc
    model = profile.recognize(reply.words)
    if model is None:
        return None, _not_ours(client, profile, _characters(reply.words))
    return model, None


def _not_ours(
    client: ModbusClient, profile: DeviceProfile, answer: str, *, cause: FujiError | None = None
) -> FujiProtocolUnsupportedError:
    msg = f"station {client.address} answered {answer}; it is not a {profile.name} analyzer"
    error = FujiProtocolUnsupportedError(
        msg,
        context=ErrorContext(command_name="discover", port=client.label, address=client.address),
    )
    error.__cause__ = cause
    return error


def _characters(words: Sequence[int]) -> str:
    try:
        return repr(decode_chars(words))
    except FujiError:
        return " ".join(f"{w:04X}h" for w in words)
