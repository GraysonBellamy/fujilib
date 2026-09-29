"""The type code and the channel layout it implies (design §2.9).

The type code (FC04 0448h, one character per register) names the ordered
configuration: which NDIR components, which O2 source, which O2-corrected
outputs. The ZPA manual (§5.3(3), p.37) derives the channel layout from three
of its digits:

1. the NDIR components of digit 6, in order (NO is shown as NOx when digit 21
   is A or C);
2. then O2, when digit 7 names an O2 source;
3. then the O2-corrected NO/SO2/CO values, when digit 21 is A or C;
4. then their averages, when digit 21 is C.

**The type code is a hint, not an authority.** The bench unit's digit 7 says
"no O2" although channel 3 measures O2 with a cell fitted later, and three of
its digits are not in the current table. Labels that feed a calculation are
asserted by the caller; everything here is a *suggestion* with a
:class:`~fujilib.registry.channels.LabelSource`.

**Options.** Digits 21 (O2-corrected outputs) and 22 (the DIO contacts,
which carry the auto-calibration valve drive and the alarm outputs) say which
options were ordered. :attr:`TypeCode.options` is that suggestion; like a
label, it is a hint the caller can override by asserting the options.
:data:`MODEL_OPTIONS` is what each model's manual describes at all.

Only the ZPA code table ships. Other models' codes are kept raw.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol

from fujilib.devices.capability import Capability
from fujilib.registry.channels import ChannelId, ChannelRole, Gas, LabelSource

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "MODEL_OPTIONS",
    "TYPECODE_DECODERS",
    "ZPA_DECODER",
    "ChannelLayout",
    "ChannelSuggestion",
    "DigitMeaning",
    "O2Correction",
    "O2Source",
    "TypeCode",
    "TypeCodeDecoder",
    "channel_layout",
    "decode_type_code",
    "suggest_labels",
]


class O2Source(StrEnum):
    """Where the O2 value comes from (type-code digit 7)."""

    NONE = "none"
    EXTERNAL_ANALYZER = "external_analyzer"
    """An external O2 analyzer through the A/I connector, as a 0-1 V DC signal."""
    EXTERNAL_ZIRCONIA = "external_zirconia"
    """An external zirconia analyzer (ZFK7) through the same connector."""
    GALVANIC = "galvanic"
    """A built-in galvanic fuel cell."""
    PARAMAGNETIC_HEAT_TREATMENT = "paramagnetic_heat_treatment"
    """A built-in paramagnetic cell, heat-treatment variant."""
    PARAMAGNETIC_ENVIRONMENTAL = "paramagnetic_environmental"
    """A built-in paramagnetic cell, environmental-measurement variant."""


class O2Correction(StrEnum):
    """Which O2-corrected outputs are fitted (type-code digit 21)."""

    NONE = "none"
    CORRECTED = "corrected"
    CORRECTED_AVERAGE = "corrected_average"
    BOTH = "both"

    @property
    def has_corrected(self) -> bool:
        """Whether corrected instantaneous values are output."""
        return self in {O2Correction.CORRECTED, O2Correction.BOTH}

    @property
    def has_average(self) -> bool:
        """Whether corrected averages are output."""
        return self in {O2Correction.CORRECTED_AVERAGE, O2Correction.BOTH}


@dataclass(frozen=True, slots=True)
class DigitMeaning:
    """One digit of a type code and what the table says it means."""

    digit: int
    code: str
    item: str
    meaning: str | None
    """``None`` when the table does not list ``code`` for this digit."""
    inferred: bool = False
    """The meaning was reconstructed, not read from the table."""


@dataclass(frozen=True, slots=True)
class ChannelSuggestion:
    """What the type code suggests a channel carries."""

    channel: ChannelId
    gas: Gas
    role: ChannelRole
    source: LabelSource
    derived_from: ChannelId | None = None
    """For an O2-corrected value or average, the channel of the corrected component."""


@dataclass(frozen=True, slots=True)
class ChannelLayout:
    """The channels a type code implies, in channel order."""

    channels: tuple[ChannelSuggestion, ...]

    def get(self, channel: ChannelId) -> ChannelSuggestion | None:
        """The suggestion for ``channel``, if the layout has one."""
        for suggestion in self.channels:
            if suggestion.channel is channel:
                return suggestion
        return None

    @property
    def o2_channel(self) -> ChannelId | None:
        """The channel carrying instantaneous O2, if any."""
        for suggestion in self.channels:
            if suggestion.gas is Gas.O2 and suggestion.role is ChannelRole.INSTANTANEOUS:
                return suggestion.channel
        return None


@dataclass(frozen=True, slots=True)
class TypeCode:
    """A type code: the raw string plus whatever of it decodes."""

    raw: str
    model: str | None = None
    decoder: str | None = None
    digits: tuple[DigitMeaning, ...] = ()
    ndir_components: tuple[Gas, ...] | None = None
    o2_source: O2Source | None = None
    o2_correction: O2Correction | None = None
    layout: ChannelLayout | None = None
    options: Capability | None = None
    """The options the code lists, or ``None`` when its option digits do not decode."""

    @property
    def unknown_digits(self) -> tuple[int, ...]:
        """Positions whose code the table does not list."""
        return tuple(d.digit for d in self.digits if d.meaning is None)

    @property
    def decoded(self) -> bool:
        """Whether a code table was applied at all."""
        return self.decoder is not None


class TypeCodeDecoder(Protocol):
    """Decodes one model's type codes."""

    @property
    def name(self) -> str:
        """The model this decoder handles, e.g. ``"ZPA"``."""
        ...

    def decode(self, raw: str) -> TypeCode:
        """Decode ``raw``. Never raises; undecodable digits have no meaning."""
        ...


# --- The channel-layout rule --------------------------------------------------------

#: O2 correction applies to these components only (ZPA manual p.100, note 6).
_CORRECTABLE: Final = (Gas.NO, Gas.SO2, Gas.CO)


def channel_layout(
    ndir: Sequence[Gas],
    *,
    o2: bool,
    correction: O2Correction,
    source: LabelSource,
) -> ChannelLayout:
    """Apply the ZPA manual's layout rule (§5.3(3)); see the module docstring.

    Correction needs an O2 value, so without ``o2`` no corrected channels are
    laid out.
    """
    corrected = correction.has_corrected or correction.has_average
    shown = tuple(Gas.NOX if g is Gas.NO and corrected and o2 else g for g in ndir)
    out: list[ChannelSuggestion] = []

    def add(gas: Gas, role: ChannelRole, derived_from: ChannelId | None = None) -> ChannelId:
        channel = ChannelId.from_number(len(out) + 1)
        out.append(ChannelSuggestion(channel, gas, role, source, derived_from))
        return channel

    component_channel = {gas: add(gas, ChannelRole.INSTANTANEOUS) for gas in shown}
    if o2:
        add(Gas.O2, ChannelRole.INSTANTANEOUS)
        targets = [g for g, orig in zip(shown, ndir, strict=True) if orig in _CORRECTABLE]
        if correction.has_corrected:
            for gas in targets:
                add(gas, ChannelRole.O2_CORRECTED, component_channel[gas])
        if correction.has_average:
            for gas in targets:
                add(gas, ChannelRole.O2_CORRECTED_AVERAGE, component_channel[gas])
    return ChannelLayout(tuple(out))


# --- The ZPA code table (ZPA manual §9.3, p.99-100) ------------------------------------

_NDIR_BY_CODE: Final[Mapping[str, tuple[Gas, ...]]] = MappingProxyType(
    {
        "Y": (),
        "P": (Gas.NO,),
        "A": (Gas.SO2,),
        "D": (Gas.CO2,),
        "B": (Gas.CO,),
        "E": (Gas.CH4,),
        "F": (Gas.NO, Gas.SO2),
        "G": (Gas.NO, Gas.CO),
        "H": (Gas.SO2, Gas.CO2),
        "J": (Gas.CO2, Gas.CO),
        "K": (Gas.CH4, Gas.CO),
        "L": (Gas.CO2, Gas.CH4),
        "N": (Gas.NO, Gas.SO2, Gas.CO),
        "T": (Gas.CO2, Gas.CO, Gas.CH4),
        "V": (Gas.NO, Gas.SO2, Gas.CO2, Gas.CO),
    }
)

_O2_BY_CODE: Final[Mapping[str, O2Source]] = MappingProxyType(
    {
        "Y": O2Source.NONE,
        "1": O2Source.EXTERNAL_ANALYZER,
        "2": O2Source.EXTERNAL_ZIRCONIA,
        "3": O2Source.GALVANIC,
        "4": O2Source.PARAMAGNETIC_HEAT_TREATMENT,
        "D": O2Source.PARAMAGNETIC_ENVIRONMENTAL,
    }
)

_CORRECTION_BY_CODE: Final[Mapping[str, O2Correction]] = MappingProxyType(
    {
        "Y": O2Correction.NONE,
        "A": O2Correction.CORRECTED,
        "B": O2Correction.CORRECTED_AVERAGE,
        "C": O2Correction.BOTH,
    }
)

_NDIR_RANGES: Final = {
    "Y": "none",
    **{
        code: f"0-{value} ppm"
        for code, value in zip(
            "BCDSEFGUTH", (100, 200, 250, 300, 500, 1000, 2000, 2500, 3000, 5000), strict=True
        )
    },
    **{
        code: f"0-{value} vol%"
        for code, value in zip(
            "JKQLMNVWPXR", (1, 2, 3, 5, 10, 20, 25, 40, 50, 70, 100), strict=True
        )
    },
    "Z": "others",
}

_O2_RANGES: Final = {
    "Y": "none",
    "A": "0-5/10 vol%",
    "B": "0-5/25 vol%",
    "C": "0-10/25 vol%",
    "L": "0-5 vol%",
    "M": "0-10 vol%",
    "V": "0-25 vol%",
    "P": "0-50 vol%",
    "R": "0-100 vol%",
    "S": "100-95 vol%",
    "Z": "others",
}

#: The DIO option digit lost its table marks in extraction; these meanings are
#: reconstructed from the DO allocation table (ZPA manual p.29). The auto
#: calibration contacts drive the external zero and span gas valves, which auto
#: zero calibration uses too (ZPA manual p.29, p.59); the analyzer has no
#: calibration valves of its own (TN5A1191b p.10).
_DIO: Final = {
    "Y": "none",
    "A": "FAULT",
    "B": "FAULT, auto calibration",
    "C": "FAULT, H/L alarm",
    "D": "FAULT, range ID / remote range",
    "E": "FAULT, auto calibration, H/L alarm",
    "F": "FAULT, H/L alarm, range ID / remote range",
    "G": "FAULT, auto calibration, range ID / remote range",
    "H": "FAULT, auto calibration, H/L alarm, range ID / remote range",
}

_DIO_OPTIONS: Final[Mapping[str, Capability]] = MappingProxyType(
    {
        code: (
            (
                Capability.AUTO_CALIBRATION | Capability.AUTO_ZERO
                if "auto calibration" in meaning
                else Capability.NONE
            )
            | (Capability.ALARMS if "H/L alarm" in meaning else Capability.NONE)
        )
        for code, meaning in _DIO.items()
    }
)

#: The options each model's manual describes. The ZPA manual has no blowback,
#: measurement-point switching or reference gas, nor has its code table or its
#: parts list (ZPA manual p.99-100; TN5A1191b p.10); those registers serve other
#: models.
MODEL_OPTIONS: Final[Mapping[str, Capability]] = MappingProxyType(
    {
        "ZPA": (
            Capability.ALARMS
            | Capability.AUTO_CALIBRATION
            | Capability.AUTO_ZERO
            | Capability.AVERAGING
            | Capability.O2_CORRECTION
        ),
    }
)


def _options(d21: str, d22: str) -> Capability | None:
    correction = _CORRECTION_BY_CODE.get(d21)
    dio = _DIO_OPTIONS.get(d22)
    if correction is None or dio is None:
        return None
    options = dio
    if correction.has_corrected or correction.has_average:
        options |= Capability.O2_CORRECTION
    if correction.has_average:
        options |= Capability.AVERAGING
    return options


_NDIR_ITEM: Final = {
    9: "NDIR range: component 1, range 1",
    10: "NDIR range: component 1, range 2",
    11: "NDIR range: component 2, range 1",
    12: "NDIR range: component 2, range 2",
    13: "NDIR range: component 3, range 1",
    14: "NDIR range: component 3, range 2",
    15: "NDIR range: component 4, range 1",
    16: "NDIR range: component 4, range 2",
}

#: digit -> (item, {code: meaning}, meanings inferred?)
_ZPA_TABLE: Final[Mapping[int, tuple[str, Mapping[str, str], bool]]] = MappingProxyType(
    {
        4: (
            "Specification/structure",
            {"A": "horizontal, power terminal block", "D": "horizontal, power inlet with lock"},
            False,
        ),
        5: ("Mounting", {"B": "19-inch rack, EIA"}, False),
        6: (
            "Measurable component (NDIR)",
            {
                **{
                    c: " / ".join(g.name for g in gases) or "none"
                    for c, gases in _NDIR_BY_CODE.items()
                },
                "Z": "others",
            },
            False,
        ),
        7: ("Measurable component (O2)", {c: s.value for c, s in _O2_BY_CODE.items()}, False),
        **{d: (item, _NDIR_RANGES, False) for d, item in _NDIR_ITEM.items()},
        17: ("O2 range", _O2_RANGES, False),
        18: ("Gas connection", {"1": "Rc1/4", "2": "NPT1/4"}, False),
        19: (
            "Output",
            {
                "A": "0-1 V DC",
                "B": "4-20 mA DC",
                "C": "0-1 V DC + communication",
                "D": "4-20 mA DC + communication",
            },
            False,
        ),
        20: (
            "Indication / power cord",
            {
                "J": "Japanese, 125 V (PSE)",
                "E": "English, 125 V (UL)",
                "U": "English, 250 V (CEE)",
                "C": "Chinese, 250 V (CCC)",
            },
            False,
        ),
        21: ("O2 correction outputs", {c: s.value for c, s in _CORRECTION_BY_CODE.items()}, False),
        22: ("Optional function (DIO)", _DIO, True),
        23: ("Pressure compensation", {"Y": "none", "1": "pressure compensation"}, True),
        24: ("Unit", {"A": "ppm, vol%", "B": "mg/m3, g/m3"}, False),
        25: (
            "Adjustment",
            {"A": "standard", "C": "heat treatment furnace", "D": "converter", "Z": "others"},
            False,
        ),
        26: ("Others", {"Z": "non-standard"}, False),
    }
)

#: (digit 6, digit 21) pairs the manual's layout table lists; the O2 digit must
#: name a source whenever digit 21 is not Y.
_TABLE_ROWS: Final = frozenset(
    {
        *((d6, "Y") for d6 in "PADBEFGHJKLNTV"),
        ("Y", "Y"),
        *((d6, c) for d6 in "PABFGJNV" for c in "AC"),
    }
)


#: The revision-code digit: any code is a revision, so none is "unknown".
_REVISION_DIGIT: Final = 8


def _digit(raw: str, position: int) -> str:
    return raw[position - 1] if len(raw) >= position else ""


class _ZpaDecoder:
    """The ZPA type-code table (ZPA manual §9.3)."""

    @property
    def name(self) -> str:
        return "ZPA"

    def decode(self, raw: str) -> TypeCode:
        digits = [DigitMeaning(1, raw[:3], "Model", "ZPA")]
        for position in range(4, 27):
            code = _digit(raw, position)
            if position == _REVISION_DIGIT:
                meaning = f"revision {code}" if code else None
                digits.append(DigitMeaning(position, code, "Revision code", meaning))
                continue
            item, meanings, inferred = _ZPA_TABLE[position]
            meaning = meanings.get(code)
            digits.append(
                DigitMeaning(position, code, item, meaning, inferred and meaning is not None)
            )
        d6, d7, d21 = _digit(raw, 6), _digit(raw, 7), _digit(raw, 21)
        ndir = _NDIR_BY_CODE.get(d6)
        o2_source = _O2_BY_CODE.get(d7)
        correction = _CORRECTION_BY_CODE.get(d21)
        layout = None
        if ndir is not None and o2_source is not None and correction is not None:
            has_o2 = o2_source is not O2Source.NONE
            listed = (d6, d21) in _TABLE_ROWS and (has_o2 or (d21 == "Y" and d6 != "Y"))
            layout = channel_layout(
                ndir,
                o2=has_o2,
                correction=correction,
                source=LabelSource.TYPE_CODE if listed else LabelSource.INFERRED,
            )
        return TypeCode(
            raw=raw,
            model="ZPA",
            decoder=self.name,
            digits=tuple(digits),
            ndir_components=ndir,
            o2_source=o2_source,
            o2_correction=correction,
            layout=layout,
            options=_options(d21, _digit(raw, 22)),
        )


ZPA_DECODER: Final[TypeCodeDecoder] = _ZpaDecoder()

#: Decoders by model; only the ZPA table ships (design §2.9).
TYPECODE_DECODERS: Final[Mapping[str, TypeCodeDecoder]] = MappingProxyType({"ZPA": ZPA_DECODER})


def decode_type_code(
    raw: str,
    decoders: Mapping[str, TypeCodeDecoder] = TYPECODE_DECODERS,
) -> TypeCode:
    """Decode ``raw`` with the table for its model, or keep it raw.

    Never raises. A code whose first three characters name no known model
    comes back with only ``raw`` (and ``model`` when it starts with ``ZP``).
    """
    text = raw.rstrip()
    model = text[:3]
    decoder = decoders.get(model)
    if decoder is None:
        return TypeCode(raw=text, model=model if model.startswith("ZP") else None)
    return decoder.decode(text)


# --- Suggestions for present channels -------------------------------------------------


def suggest_labels(
    type_code: TypeCode | None,
    present: Iterable[ChannelId],
) -> Mapping[ChannelId, ChannelSuggestion]:
    """Suggest a label for each present channel (design §2.9, step 4).

    A channel the type-code layout covers gets its suggestion. A present
    channel just after the layout's NDIR components, when the layout has no
    O2, is suggested as O2 by the layout rule, labelled ``INFERRED``: this is
    the bench unit, whose code says "no O2" while channel 3 measures O2. Any
    other present channel gets no suggestion. A suggestion never selects a
    scientific channel by itself; only an asserted label does.
    """
    layout = type_code.layout if type_code is not None else None
    suggestions: dict[ChannelId, ChannelSuggestion] = {}
    if layout is None:
        return MappingProxyType(suggestions)
    after_ndir = ChannelId.from_number(len(layout.channels) + 1) if layout.channels else None
    for channel in present:
        suggestion = layout.get(channel)
        if suggestion is not None:
            suggestions[channel] = suggestion
        elif layout.o2_channel is None and channel is after_ndir and not _has_derived(layout):
            suggestions[channel] = ChannelSuggestion(
                channel, Gas.O2, ChannelRole.INSTANTANEOUS, LabelSource.INFERRED
            )
    return MappingProxyType(suggestions)


def _has_derived(layout: ChannelLayout) -> bool:
    return any(s.role is not ChannelRole.INSTANTANEOUS for s in layout.channels)
