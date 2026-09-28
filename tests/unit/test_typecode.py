"""Type-code decoding and the channel layout (design §2.9, ZPA manual §5.3(3), §9.3)."""

from __future__ import annotations

import pytest

from fujilib.registry.channels import ChannelId, ChannelRole, Gas, LabelSource
from fujilib.registry.typecode import (
    O2Correction,
    O2Source,
    channel_layout,
    decode_type_code,
    suggest_labels,
)

#: The bench unit's code (protocol findings §2).
BENCH = "ZPACBJY1MPFYYYYYY2DEYAYAY0"

#: ZPA manual §5.3(3) (p.37), transcribed row by row: digit 6, O2 fitted, digit 21,
#: display contents.
LAYOUT_TABLE = [
    ("Y", True, "Y", "Ch1:O2"),
    ("P", False, "Y", "Ch1:NO"),
    ("A", False, "Y", "Ch1:SO2"),
    ("D", False, "Y", "Ch1:CO2"),
    ("B", False, "Y", "Ch1:CO"),
    ("E", False, "Y", "Ch1:CH4"),
    ("F", False, "Y", "Ch1:NO, Ch2:SO2"),
    ("G", False, "Y", "Ch1:NO, Ch2:CO"),
    ("H", False, "Y", "Ch1:SO2, Ch2:CO2"),
    ("J", False, "Y", "Ch1:CO2, Ch2:CO"),
    ("K", False, "Y", "Ch1:CH4, Ch2:CO"),
    ("L", False, "Y", "Ch1:CO2, Ch2:CH4"),
    ("N", False, "Y", "Ch1:NO, Ch2:SO2, Ch3:CO"),
    ("T", False, "Y", "Ch1:CO2, Ch2:CO, Ch3:CH4"),
    ("V", False, "Y", "Ch1:NO, Ch2:SO2, Ch3:CO2, Ch4:CO"),
    ("P", True, "Y", "Ch1:NO, Ch2:O2"),
    ("A", True, "Y", "Ch1:SO2, Ch2:O2"),
    ("D", True, "Y", "Ch1:CO2, Ch2:O2"),
    ("B", True, "Y", "Ch1:CO, Ch2:O2"),
    ("E", True, "Y", "Ch1:CH4, Ch2:O2"),
    ("F", True, "Y", "Ch1:NO, Ch2:SO2, Ch3:O2"),
    ("G", True, "Y", "Ch1:NO, Ch2:CO, Ch3:O2"),
    ("H", True, "Y", "Ch1:SO2, Ch2:CO2, Ch3:O2"),
    ("J", True, "Y", "Ch1:CO2, Ch2:CO, Ch3:O2"),
    ("K", True, "Y", "Ch1:CH4, Ch2:CO, Ch3:O2"),
    ("L", True, "Y", "Ch1:CO2, Ch2:CH4, Ch3:O2"),
    ("N", True, "Y", "Ch1:NO, Ch2:SO2, Ch3:CO, Ch4:O2"),
    ("T", True, "Y", "Ch1:CO2, Ch2:CO, Ch3:CH4, Ch4:O2"),
    ("V", True, "Y", "Ch1:NO, Ch2:SO2, Ch3:CO2, Ch4:CO, Ch5:O2"),
    ("P", True, "A", "Ch1:NOx, Ch2:O2, Ch3:corrected NOx"),
    ("A", True, "A", "Ch1:SO2, Ch2:O2, Ch3:corrected SO2"),
    ("B", True, "A", "Ch1:CO, Ch2:O2, Ch3:corrected CO"),
    ("F", True, "A", "Ch1:NOx, Ch2:SO2, Ch3:O2, Ch4:corrected NOx, Ch5:corrected SO2"),
    ("G", True, "A", "Ch1:NOx, Ch2:CO, Ch3:O2, Ch4:corrected NOx, Ch5:corrected CO"),
    ("J", True, "A", "Ch1:CO2, Ch2:CO, Ch3:O2, Ch4:corrected CO"),
    (
        "N",
        True,
        "A",
        "Ch1:NOx, Ch2:SO2, Ch3:CO, Ch4:O2, Ch5:corrected NOx, Ch6:corrected SO2, Ch7:corrected CO",
    ),
    (
        "V",
        True,
        "A",
        "Ch1:NOx, Ch2:SO2, Ch3:CO2, Ch4:CO, Ch5:O2, Ch6:corrected NOx, Ch7:corrected SO2, "
        "Ch8:corrected CO",
    ),
    ("P", True, "C", "Ch1:NOx, Ch2:O2, Ch3:corrected NOx, Ch4:corrected NOx average"),
    ("A", True, "C", "Ch1:SO2, Ch2:O2, Ch3:corrected SO2, Ch4:corrected SO2 average"),
    ("B", True, "C", "Ch1:CO, Ch2:O2, Ch3:corrected CO, Ch4:corrected CO average"),
    (
        "F",
        True,
        "C",
        "Ch1:NOx, Ch2:SO2, Ch3:O2, Ch4:corrected NOx, Ch5:corrected SO2, "
        "Ch6:corrected NOx average, Ch7:corrected SO2 average",
    ),
    (
        "G",
        True,
        "C",
        "Ch1:NOx, Ch2:CO, Ch3:O2, Ch4:corrected NOx, Ch5:corrected CO, "
        "Ch6:corrected NOx average, Ch7:corrected CO average",
    ),
    ("J", True, "C", "Ch1:CO2, Ch2:CO, Ch3:O2, Ch4:corrected CO, Ch5:corrected CO average"),
    (
        "N",
        True,
        "C",
        "Ch1:NOx, Ch2:SO2, Ch3:CO, Ch4:O2, Ch5:corrected NOx, Ch6:corrected SO2, "
        "Ch7:corrected CO, Ch8:corrected NOx average, Ch9:corrected SO2 average, "
        "Ch10:corrected CO average",
    ),
    (
        "V",
        True,
        "C",
        "Ch1:NOx, Ch2:SO2, Ch3:CO2, Ch4:CO, Ch5:O2, Ch6:corrected NOx, Ch7:corrected SO2, "
        "Ch8:corrected CO, Ch9:corrected NOx average, Ch10:corrected SO2 average, "
        "Ch11:corrected CO average",
    ),
]

_GASES = {"NO": Gas.NO, "NOx": Gas.NOX, "SO2": Gas.SO2, "CO2": Gas.CO2, "CO": Gas.CO,
          "CH4": Gas.CH4, "O2": Gas.O2}  # fmt: skip


def parse_row(text: str) -> list[tuple[ChannelId, Gas, ChannelRole]]:
    out: list[tuple[ChannelId, Gas, ChannelRole]] = []
    for item in text.split(", "):
        channel, content = item.split(":")
        role = ChannelRole.INSTANTANEOUS
        if content.startswith("corrected "):
            content = content.removeprefix("corrected ")
            role = ChannelRole.O2_CORRECTED
            if content.endswith(" average"):
                content = content.removesuffix(" average")
                role = ChannelRole.O2_CORRECTED_AVERAGE
        out.append((ChannelId(channel.upper()), _GASES[content], role))
    return out


def code_with(d6: str, d7: str, d21: str) -> str:
    chars = list(BENCH)
    chars[5], chars[6], chars[20] = d6, d7, d21
    return "".join(chars)


@pytest.mark.parametrize(("d6", "o2", "d21", "contents"), LAYOUT_TABLE)
@pytest.mark.parametrize("o2_code", ["1", "2", "3", "4", "D"])
def test_every_row_of_the_layout_table(
    d6: str, o2: bool, d21: str, contents: str, o2_code: str
) -> None:
    decoded = decode_type_code(code_with(d6, o2_code if o2 else "Y", d21))
    assert decoded.layout is not None
    got = [(s.channel, s.gas, s.role) for s in decoded.layout.channels]
    assert got == parse_row(contents)
    assert all(s.source is LabelSource.TYPE_CODE for s in decoded.layout.channels)
    for s in decoded.layout.channels:
        if s.role is not ChannelRole.INSTANTANEOUS:
            assert s.derived_from is not None
            assert decoded.layout.get(s.derived_from) is not None


def test_combinations_the_table_lacks_follow_the_rule_as_inferred() -> None:
    decoded = decode_type_code(code_with("H", "D", "A"))  # SO2/CO2 with correction
    assert decoded.layout is not None
    got = [(s.channel.value, s.gas, s.role) for s in decoded.layout.channels]
    assert got == [
        ("CH1", Gas.SO2, ChannelRole.INSTANTANEOUS),
        ("CH2", Gas.CO2, ChannelRole.INSTANTANEOUS),
        ("CH3", Gas.O2, ChannelRole.INSTANTANEOUS),
        ("CH4", Gas.SO2, ChannelRole.O2_CORRECTED),
    ]
    assert {s.source for s in decoded.layout.channels} == {LabelSource.INFERRED}
    only_average = decode_type_code(code_with("B", "3", "B")).layout
    assert only_average is not None
    assert [s.role for s in only_average.channels][-1] is ChannelRole.O2_CORRECTED_AVERAGE
    assert only_average.channels[0].source is LabelSource.INFERRED


def test_correction_without_o2_lays_out_no_corrected_channels() -> None:
    layout = channel_layout(
        (Gas.NO,), o2=False, correction=O2Correction.BOTH, source=LabelSource.INFERRED
    )
    assert [(s.gas, s.role) for s in layout.channels] == [(Gas.NO, ChannelRole.INSTANTANEOUS)]
    assert layout.o2_channel is None


# --- The bench unit ----------------------------------------------------------------------


def test_bench_code() -> None:
    decoded = decode_type_code(BENCH)
    assert decoded.decoded
    assert decoded.model == "ZPA"
    assert decoded.ndir_components == (Gas.CO2, Gas.CO)
    assert decoded.o2_source is O2Source.NONE  # stale: channel 3 measures O2
    assert decoded.o2_correction is O2Correction.NONE
    assert decoded.unknown_digits == (4, 25, 26)
    by_digit = {d.digit: d for d in decoded.digits}
    assert by_digit[9].meaning == "0-10 vol%"
    assert by_digit[11].meaning == "0-1000 ppm"
    assert by_digit[8].meaning == "revision 1"
    assert by_digit[22].meaning == "FAULT"
    assert by_digit[22].inferred
    assert not by_digit[6].inferred


def test_bench_suggestions_infer_o2_after_the_ndir_components() -> None:
    decoded = decode_type_code(BENCH)
    present = [ChannelId.CH1, ChannelId.CH2, ChannelId.CH3]
    suggestions = suggest_labels(decoded, present)
    assert suggestions[ChannelId.CH1].gas is Gas.CO2
    assert suggestions[ChannelId.CH1].source is LabelSource.TYPE_CODE
    assert suggestions[ChannelId.CH2].gas is Gas.CO
    assert suggestions[ChannelId.CH3].gas is Gas.O2
    assert suggestions[ChannelId.CH3].source is LabelSource.INFERRED
    assert ChannelId.CH4 not in suggest_labels(decoded, [ChannelId.CH4])


def test_no_suggestion_past_a_layout_with_o2_or_without_a_code() -> None:
    with_o2 = decode_type_code(code_with("J", "D", "Y"))
    assert ChannelId.CH4 not in suggest_labels(with_o2, [ChannelId.CH4])
    assert suggest_labels(None, [ChannelId.CH1]) == {}
    corrected = decode_type_code(code_with("B", "D", "A"))
    assert ChannelId.CH4 not in suggest_labels(corrected, [ChannelId.CH4])


# --- Undecodable codes ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "model"),
    [("ZPBABJY2MPFYYYYYY2DEYAYAAZ", "ZPB"), ("", None), ("XYZ", None)],
)
def test_other_models_stay_raw(raw: str, model: str | None) -> None:
    decoded = decode_type_code(raw)
    assert not decoded.decoded
    assert decoded.raw == raw
    assert decoded.model == model
    assert decoded.layout is None
    assert decoded.unknown_digits == ()


def test_unknown_component_code_has_no_layout() -> None:
    decoded = decode_type_code(code_with("Z", "D", "Y"))
    assert decoded.layout is None
    assert decoded.ndir_components is None
    assert 6 not in decoded.unknown_digits  # "Z" means "others", which the table lists


def test_short_code_decodes_what_it_has() -> None:
    decoded = decode_type_code("ZPACBJ")
    assert decoded.ndir_components == (Gas.CO2, Gas.CO)
    assert decoded.layout is None
    assert 7 in decoded.unknown_digits
