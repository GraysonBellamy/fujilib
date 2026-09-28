"""The serial transport: an ``anyserial.SerialPort`` and the settings it was opened with.

:attr:`SerialTransport.stream` is the real ``SerialPort``, never a wrapper, so
``anymodbus`` drains each request before listening and clears stale input
before each request (design §4.1). The same class wraps one end of
``anyserial.testing.serial_port_pair()``, whose ends are real ``SerialPort``
objects too, so tests exercise the hardware code path.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Self

import anyio
from anyserial import FlowControl, SerialConfig, open_serial_port

from fujilib.errors import ErrorContext, FujiConfigurationError, FujiConnectionError
from fujilib.transport.ports import canonical_port

if TYPE_CHECKING:
    from types import TracebackType

    from anyserial import SerialPort

    from fujilib.transport.base import SerialSettings

__all__ = ["SerialTransport", "serial_config"]


def serial_config(settings: SerialSettings) -> SerialConfig:
    """The ``anyserial`` configuration for ``settings``."""
    return SerialConfig(
        baudrate=settings.baudrate,
        byte_size=settings.bytesize,
        parity=settings.parity,
        stop_bits=settings.stopbits,
        flow_control=FlowControl(xon_xoff=settings.xonxoff, rts_cts=settings.rtscts),
        exclusive=settings.exclusive,
    )


class SerialTransport:
    """An open serial port. Satisfies :class:`~fujilib.transport.base.Transport`."""

    __slots__ = ("__weakref__", "_port", "_settings")

    def __init__(self, port: SerialPort, settings: SerialSettings) -> None:
        """Wrap an already open ``port``; :meth:`open` is the usual constructor."""
        self._port = port
        self._settings = settings

    @classmethod
    async def open(cls, settings: SerialSettings) -> Self:
        """Open the port named by ``settings.port``, under its canonical name.

        Raises:
            FujiConfigurationError: ``anyserial`` rejects the settings.
            FujiConnectionError: the port does not exist, is busy or cannot be opened.
        """
        name = canonical_port(settings.port)
        context = ErrorContext(port=name, command_name="open")
        try:
            port = await open_serial_port(name, serial_config(settings))
        except ValueError as exc:  # anyserial's ConfigurationError is also a ValueError
            msg = f"cannot open {name} with these settings: {exc}"
            raise FujiConfigurationError(msg, context=context) from exc
        except OSError as exc:  # anyserial's SerialError is an OSError
            msg = f"cannot open {name}: {exc}"
            raise FujiConnectionError(msg, context=context) from exc
        return cls(port, replace(settings, port=name))

    @property
    def label(self) -> str:
        """The canonical port name."""
        return self._settings.port

    @property
    def is_open(self) -> bool:
        """Whether the port is open."""
        return self._port.is_open

    @property
    def settings(self) -> SerialSettings:
        """The settings the port was opened with."""
        return self._settings

    @property
    def stream(self) -> SerialPort:
        """The real ``SerialPort``."""
        return self._port

    async def aclose(self) -> None:
        """Close the port. Idempotent, and completes even when the caller is cancelled."""
        if not self._port.is_open:
            return
        with anyio.CancelScope(shield=True):
            await self._port.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        state = "open" if self.is_open else "closed"
        return f"<SerialTransport {self.label} {state}>"
