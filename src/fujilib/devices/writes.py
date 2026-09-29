"""Write procedures: one setting, written once and read back (design §6.3, §6.4).

Like :mod:`fujilib.devices.reads`, these functions hold no state; the gates,
the caches and the write-rate warning belong to the session.

**One write, never retried.** :func:`write_setting` writes one word with FC06
and reads it back. The read-back runs in a scope shielded from cancellation,
with its own deadline (``verify_timeout``), so it happens even when the write
used up the operation's deadline or its reply was lost. A caller that cancels
the call itself (rather than a deadline running out) cancels it without a
read-back; the port's late-reply window still protects the next request. What
the read-back finds decides the outcome, a :class:`WriteState`:

- ``verified``: the register reads back as written, whether or not the
  write's own reply arrived;
- ``mismatch``: it reads back otherwise (the analyzer refused it silently,
  the front panel changed it, or a write whose reply was lost never arrived);
- ``unknown``: the read-back failed too, so nothing is established.

An exception reply to the write is a definite refusal and is raised as it is:
nothing was applied. A port that fails while the write waits for its reply
is raised as an unknown outcome at once; nothing more can be read.

**When not to write.** :func:`busy_reasons` says why the analyzer's status
forbids a write or a command now: a calibration running, or the front panel
in a menu or in a manual calibration. The operator may be changing the very
setting, and a setting changed during a calibration can change its scope
mid-run (design §6.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING

import anyio

from fujilib._deadline import Deadline
from fujilib.devices.decode import decode_register
from fujilib.errors import (
    ErrorContext,
    FujiConnectionError,
    FujiError,
    FujiTimeoutError,
    FujiVerificationError,
    FujiWriteOutcomeUnknownError,
)
from fujilib.protocol.modbus.client import FailureKind
from fujilib.protocol.modbus.read_plan import BlockRead
from fujilib.registry.enums import DisplayScreen, ManualCalibrationStep
from fujilib.registry.regions import FC_READ_HOLDING

if TYPE_CHECKING:
    from fujilib.devices.decode import RegisterValue
    from fujilib.devices.models import TransferTiming
    from fujilib.devices.reads import StatusRead
    from fujilib.protocol.base import ProtocolClient
    from fujilib.protocol.modbus.client import BlockReply
    from fujilib.registry.registers import RegisterSpec
    from fujilib.registry.units import Unit

__all__ = [
    "WriteResult",
    "WriteState",
    "busy_reasons",
    "describe",
    "outcome_error",
    "write_setting",
]


class WriteState(StrEnum):
    """What the read-back after a write established."""

    VERIFIED = "verified"
    """The register reads back as written."""
    MISMATCH = "mismatch"
    """The register reads back as something else."""
    UNKNOWN = "unknown"
    """The read-back failed too: the write may or may not have been applied."""


@dataclass(frozen=True, slots=True)
class WriteResult:
    """One setting write and what its read-back found."""

    name: str
    """The register's name in the register map."""
    requested: RegisterValue
    """The word written, decoded as the register."""
    previous: RegisterValue
    """The register as read just before the write."""
    observed: RegisterValue | None
    """The register as read back; ``None`` when the read-back failed."""
    state: WriteState
    acknowledged: bool
    """Whether the analyzer's reply to the write arrived."""
    timing: TransferTiming | None
    """The write's timing, when its reply arrived."""
    read_back_error: FujiError | None = None
    """Why the read-back failed, when it did."""

    @property
    def verified(self) -> bool:
        """Whether the register reads back as written."""
        return self.state is WriteState.VERIFIED

    @property
    def changed(self) -> bool:
        """Whether the value written differs from the one before."""
        return self.previous.words != self.requested.words


def describe(value: RegisterValue) -> str:
    """A register value for a message: ``15 s``, ``20.95 vol%``, ``manual``."""
    shown = value.value if value.value is not None else value.raw
    if isinstance(shown, IntEnum):
        shown = shown.name.lower()
    return f"{shown} {value.unit}" if value.unit else str(shown)


def busy_reasons(status: StatusRead) -> list[str]:
    """Why the analyzer's status forbids a write or a command now; empty when it does not."""
    reasons: list[str] = []
    if status.analyzer.auto_calibration_running:
        reasons.append("an auto calibration or auto zero calibration is running")
    for channel, channel_status in status.channels.items():
        if channel_status.calibrating:
            reasons.append(f"{channel.value} is being calibrated")
    display = status.analyzer.display
    if display is not None:
        if display.screen != DisplayScreen.MEASUREMENT:
            screen = (
                display.screen.name.lower().replace("_", " ")
                if isinstance(display.screen, DisplayScreen)
                else f"screen {display.screen}"
            )
            reasons.append(f"the front panel shows the {screen} screen")
        elif display.calibration_step != ManualCalibrationStep.NONE:
            reasons.append("a manual calibration is in progress at the front panel")
    return reasons


async def write_setting(
    client: ProtocolClient,
    spec: RegisterSpec,
    raw: int,
    *,
    previous: RegisterValue,
    scaling: tuple[int, Unit] | None,
    deadline: Deadline,
    verify_timeout: float,
    command: str,
) -> WriteResult:
    """Write ``raw`` to ``spec`` once with FC06, then read it back (see the module docstring).

    ``previous`` is the register as read just before; ``scaling`` decodes a
    range-scaled value. The result is returned whatever it says;
    :func:`outcome_error` turns one that is not verified into its error.

    Raises:
        FujiModbusError: the analyzer refused the write with an exception
            reply; nothing was applied.
        FujiWriteOutcomeUnknownError: the port failed while the write waited
            for its reply.
        FujiError: the write could not be sent (a closed port, an expired
            deadline): nothing was written.
    """
    requested = decode_register(spec, (raw,), scaling=scaling)
    acknowledged = False
    timing: TransferTiming | None = None
    try:
        timing = await client.write_register(spec.address, raw, deadline=deadline, command=command)
        acknowledged = True
    except FujiWriteOutcomeUnknownError as exc:
        if exc.context.extra.get("failure") == FailureKind.CONNECTION.value:
            raise exc.with_context(setting=spec.name) from exc.__cause__
    read_back = await _read_back(client, spec, scaling, verify_timeout, command)
    observed = read_back if not isinstance(read_back, FujiError) else None
    if observed is None:
        state = WriteState.UNKNOWN
    elif observed.words == requested.words:
        state = WriteState.VERIFIED
    else:
        state = WriteState.MISMATCH
    return WriteResult(
        name=spec.name,
        requested=requested,
        previous=previous,
        observed=observed,
        state=state,
        acknowledged=acknowledged,
        timing=timing,
        read_back_error=read_back if isinstance(read_back, FujiError) else None,
    )


async def _read_back(
    client: ProtocolClient,
    spec: RegisterSpec,
    scaling: tuple[int, Unit] | None,
    verify_timeout: float,
    command: str,
) -> RegisterValue | FujiError:
    block = BlockRead(function=FC_READ_HOLDING, address=spec.address, count=spec.count)
    # A shielded scope never swallows the block's end, but a checker cannot know that.
    outcome: BlockReply | FujiError = FujiTimeoutError("the read-back did not run")
    # Shielded: a write that used up the operation's deadline is still read
    # back, within its own deadline, even if that deadline expires meanwhile.
    with anyio.CancelScope(shield=True):
        deadline = Deadline.after(verify_timeout, operation=f"{command} read-back")
        try:
            outcome = await client.read(block, deadline=deadline, command=f"{command}_verify")
        except FujiError as exc:
            outcome = exc
    if isinstance(outcome, FujiError):
        return outcome
    return decode_register(spec, outcome.words, scaling=scaling)


def outcome_error(result: WriteResult) -> FujiError | None:
    """The error for a write that is not verified, or ``None`` for one that is."""
    context = ErrorContext(
        extra={
            "setting": result.name,
            "write_state": result.state.value,
            "acknowledged": result.acknowledged,
            "transmission_started": True,
            "requested_raw": result.requested.raw,
            "previous_raw": result.previous.raw,
            "observed_raw": result.observed.raw if result.observed is not None else None,
        }
    )
    wanted = describe(result.requested)
    if result.state is WriteState.MISMATCH:
        assert result.observed is not None  # noqa: S101 - a mismatch was read back
        found = describe(result.observed)
        if result.acknowledged:
            msg = f"{result.name}: wrote {wanted}, but it reads back as {found}"
        elif result.observed.words == result.previous.words:
            msg = (
                f"{result.name}: the reply to writing {wanted} was lost, and it reads back "
                f"as {found}, as before: the write was not applied"
            )
        else:
            msg = (
                f"{result.name}: the reply to writing {wanted} was lost, and it reads back "
                f"as {found}, neither what was written nor what it was before"
            )
        return FujiVerificationError(msg, context=context)
    if result.state is WriteState.UNKNOWN:
        if isinstance(result.read_back_error, FujiConnectionError):
            # The port failed: the session breaks, as on any connection failure.
            context = context.merged(failure=FailureKind.CONNECTION.value)
        if result.acknowledged:
            msg = (
                f"{result.name}: the analyzer acknowledged writing {wanted}, but reading it "
                "back failed"
            )
        else:
            msg = (
                f"{result.name}: the reply to writing {wanted} was lost and reading it back "
                "failed; it may or may not have been applied"
            )
        return FujiWriteOutcomeUnknownError(msg, context=context)
    return None
