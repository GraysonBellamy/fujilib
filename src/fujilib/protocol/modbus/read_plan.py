"""Region-aware block-read planning (design §4.3).

Turns a set of registers into the fewest block reads such that:

- every block lies inside a single region (a block that crosses a region end
  draws exception 03 on the bench unit);
- no block exceeds the per-request word limit (64 on the ZP series);
- a multi-word value is never split across blocks;
- gaps are bridged only inside a region and only up to ``max_gap`` words.

Planning is pure and used **only for reads**; the write path never coalesces
or bridges (design §6.3). The hot paths are planned once, at import, and
tested against exact transaction lists.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from fujilib.errors import ErrorContext, FujiConfigurationError, FujiValidationError
from fujilib.registry.regions import ZP_REGIONS, RegionMap, RegisterTable
from fujilib.registry.registers import CALIBRATION_LOG, ERROR_LOG, REGISTRY, LogSpec, RegisterSpec

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "ADC_PLAN",
    "CALIBRATION_LOG_PROBE",
    "CLOCK_PLAN",
    "DEFAULT_READ_POLICY",
    "ERROR_LOG_PLAN",
    "IDENTIFY_PLAN",
    "METADATA_PLAN",
    "POLL_PLAN",
    "RANGES_PLAN",
    "SERVICE_PLAN",
    "SETTINGS_PLAN",
    "TYPE_CODE_EXT_PLAN",
    "BlockRead",
    "ReadPolicy",
    "calibration_log_plan",
    "plan_log_reads",
    "plan_reads",
]

#: The ZP series' per-request word limit for FC03, FC04 and FC10 (design §2.2).
ZP_MAX_WORDS: Final = 64


@dataclass(frozen=True, slots=True)
class ReadPolicy:
    """How aggressively adjacent registers are merged into one read."""

    max_words: int = ZP_MAX_WORDS
    max_gap: int = 8

    def __post_init__(self) -> None:
        if self.max_words < 1:
            msg = f"max_words must be positive, got {self.max_words}"
            raise FujiConfigurationError(msg)
        if self.max_gap < 0:
            msg = f"max_gap must be non-negative, got {self.max_gap}"
            raise FujiConfigurationError(msg)

    def strict(self) -> ReadPolicy:
        """The gap-free variant of this policy."""
        return replace(self, max_gap=0)


DEFAULT_READ_POLICY: Final = ReadPolicy()


@dataclass(frozen=True, slots=True)
class BlockRead:
    """One block read, and the registers it covers."""

    function: int
    address: int
    count: int
    specs: tuple[RegisterSpec, ...] = ()

    @property
    def last_address(self) -> int:
        """The last address the block reads."""
        return self.address + self.count - 1

    @property
    def key(self) -> tuple[int, int, int]:
        """``(function, address, count)``, as a transaction log records it."""
        return (self.function, self.address, self.count)

    def words_for(self, spec: RegisterSpec, words: Sequence[int]) -> tuple[int, ...]:
        """Slice ``spec``'s words out of this block's reply ``words``.

        Raises:
            FujiValidationError: ``spec`` is not inside the block, or ``words``
                is not the block's length.
        """
        if not self.address <= spec.address <= spec.last_address <= self.last_address:
            msg = f"{spec.name} is not inside block 0x{self.address:04X}+{self.count}"
            raise FujiValidationError(msg)
        self._check_length(words)
        offset = spec.address - self.address
        return tuple(words[offset : offset + spec.count])

    def to_bank(self, words: Sequence[int]) -> dict[int, int]:
        """Map each address of the block to its word from reply ``words``.

        Raises:
            FujiValidationError: ``words`` is not the block's length.
        """
        self._check_length(words)
        return dict(zip(range(self.address, self.address + self.count), words, strict=True))

    def _check_length(self, words: Sequence[int]) -> None:
        if len(words) != self.count:
            msg = f"block 0x{self.address:04X}+{self.count} got {len(words)} words"
            raise FujiValidationError(
                msg,
                context=ErrorContext(function_code=self.function, register_address=self.address),
            )


def plan_reads(
    specs: Iterable[RegisterSpec],
    *,
    regions: RegionMap = ZP_REGIONS,
    policy: ReadPolicy = DEFAULT_READ_POLICY,
) -> tuple[BlockRead, ...]:
    """Plan the fewest block reads covering ``specs``.

    Blocks come out holding table first, then input, each in address order.

    Raises:
        FujiConfigurationError: a register lies outside every region, or is
            wider than one request.
    """
    unique = {s.name: s for s in specs}.values()
    blocks: list[BlockRead] = []
    for table in RegisterTable:
        fc = table.read_function
        ordered = sorted((s for s in unique if s.table is table), key=lambda s: s.address)
        run: list[RegisterSpec] = []
        run_region = None
        for spec in ordered:
            region = regions.region_for(fc, spec.address, spec.count)
            if region is None or spec.count > policy.max_words:
                msg = f"{spec.name} cannot be read in one FC{fc:02X} request inside a region"
                raise FujiConfigurationError(
                    msg, context=ErrorContext(function_code=fc, register_address=spec.address)
                )
            if run:
                start, end = run[0].address, max(s.last_address for s in run)
                gap = spec.address - end - 1
                merged = max(end, spec.last_address) - start + 1
                if region is run_region and gap <= policy.max_gap and merged <= policy.max_words:
                    run.append(spec)
                    continue
                blocks.append(_block(fc, run))
            run, run_region = [spec], region
        if run:
            blocks.append(_block(fc, run))
    return tuple(blocks)


def _block(fc: int, run: Sequence[RegisterSpec]) -> BlockRead:
    start = run[0].address
    end = max(s.last_address for s in run)
    return BlockRead(function=fc, address=start, count=end - start + 1, specs=tuple(run))


def plan_log_reads(
    log: LogSpec,
    channel: int = 1,
    *,
    policy: ReadPolicy = DEFAULT_READ_POLICY,
) -> tuple[BlockRead, ...]:
    """Plan reads of one channel's region of ``log``, in whole records.

    Each block holds as many whole records as fit, so a block ends exactly at
    a record boundary (the calibration log reads as 5 × 63 + 45 words).

    Raises:
        FujiValidationError: ``channel`` is not a channel of ``log``.
        FujiConfigurationError: one record is wider than a request.
    """
    per_block = policy.max_words // log.record_words
    if per_block < 1:
        msg = f"a {log.name} record does not fit in one request"
        raise FujiConfigurationError(msg)
    fc = log.table.read_function
    blocks: list[BlockRead] = []
    for first in range(0, log.records, per_block):
        records = min(per_block, log.records - first)
        address = log.record_address(channel, first)
        blocks.append(BlockRead(function=fc, address=address, count=records * log.record_words))
    return tuple(blocks)


def calibration_log_plan(channel: int) -> tuple[BlockRead, ...]:
    """The block reads of one channel's calibration log (design §4.3)."""
    return plan_log_reads(CALIBRATION_LOG, channel)


def _select(*prefixes: str) -> tuple[RegisterSpec, ...]:
    return tuple(s for p in prefixes for s in REGISTRY.select(p))


def _in_region(name: str) -> tuple[RegisterSpec, ...]:
    fc = RegisterTable.INPUT.read_function
    return tuple(
        s
        for s in REGISTRY.in_table(RegisterTable.INPUT)
        if (region := ZP_REGIONS.region_for(fc, s.address, s.count)) and region.name == name
    )


#: ``poll()``: every concentration and all status, two transactions.
POLL_PLAN: Final = plan_reads(_in_region("measurement"))

#: ``read_ranges()``: the range tables of channels 1-5.
RANGES_PLAN: Final = plan_reads(
    s for s in _in_region("fixed_settings") if s.name.startswith("range.")
)

#: ``identify()``: ranges, type code and serial, and the readings (for presence).
#: Three separate plans, so ranges, identity and readings stay separate blocks
#: rather than being packed together.
IDENTIFY_PLAN: Final = (
    RANGES_PLAN
    + plan_reads(_select("identity.type_code", "identity.serial_number"))
    + plan_reads(_select("reading"))
)

#: ``read_settings()``: the whole holding map.
SETTINGS_PLAN: Final = plan_reads(REGISTRY.in_table(RegisterTable.HOLDING))

#: ``read_metadata()``'s holding reads: every setting but the inferred coefficients.
METADATA_PLAN: Final = plan_reads(
    s for s in REGISTRY.in_table(RegisterTable.HOLDING) if not s.name.startswith("interference.")
)

#: The undocumented clock, and the A/D values, apart and together.
CLOCK_PLAN: Final = plan_reads(_select("clock"))
ADC_PLAN: Final = plan_reads(_select("adc"))
SERVICE_PLAN: Final = plan_reads(_select("clock", "adc"))

#: Capability probes (design §6.6).
TYPE_CODE_EXT_PLAN: Final = plan_reads(_select("identity.type_code_ext"))
CALIBRATION_LOG_PROBE: Final = BlockRead(
    function=RegisterTable.INPUT.read_function,
    address=CALIBRATION_LOG.base,
    count=CALIBRATION_LOG.record_words,
)

#: The error log, in whole records.
ERROR_LOG_PLAN: Final = plan_log_reads(ERROR_LOG)
