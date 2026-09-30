"""``fuji-calibrate``, run in-process against the bundled bench bank (design §7.7, §6.5).

With ``--fixture`` the named gas flows into the simulated analyzer's inlet as
soon as the wait step opens, as an operator switching the valves would.
"""

from __future__ import annotations

import io
import json
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from fujilib.cli import calibrate
from fujilib.devices.keys import RunState
from fujilib.devices.panel import ManualCalibrationOutcome
from fujilib.errors import FujiAnalyzerStateError
from fujilib.registry.channels import ChannelId
from fujilib.testing import BENCH_BANK_PATH

if TYPE_CHECKING:
    from pathlib import Path

#: A steadiness rule a simulated gas meets within a second or two.
QUICK = ["--window", "0.3", "--response-factor", "0", "--interval", "0.05"]
GO = ["--confirm", calibrate.DESTRUCTIVE_FLAG]
SPAN = ["--fixture", "bench", "--channel", "CH3", "--kind", "span", "--gas-value", "20.95"]


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = calibrate.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def usage_error(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    with pytest.raises(SystemExit) as info:
        calibrate.main(list(argv))
    assert info.value.code == 2
    return capsys.readouterr().err


def test_the_plan_alone_sends_no_key(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "--fixture", "bench", "--channel", "CH1", "--kind", "zero", "--plan")
    assert code == 0
    assert "A manual zero of CH1 calibrates:" in out
    assert "  CH1: range 1 against 0 vol%" in out
    assert "(not established: may not be fitted)" in out
    assert "note: CH1 is set to zero 'at once'" in out
    assert out.rstrip().endswith("status: plan")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([*SPAN, calibrate.DESTRUCTIVE_FLAG], "pass --confirm"),
        ([*SPAN, "--confirm"], f"pass {calibrate.DESTRUCTIVE_FLAG}"),
        (["--fixture", "bench", "--channel", "CH3", "--kind", "zero", *GO], "--gas-value"),
        (["--fixture", "bench", "--channel", "CH9", "--kind", "zero", "--plan"], "not a measured"),
        (["--fixture", "bench", "--channel", "CHX", "--kind", "zero", "--plan"], "CHX"),
        ([*SPAN, *GO], "needs its unit"),
        ([*SPAN, "--gas-unit", "furlongs", *GO], "not a unit of the ZP series"),
        ([*SPAN, "--gas-unit", "vol%", "--window", "0", *GO], "positive number"),
        ([*SPAN, "--gas-unit", "vol%", "--response-factor", "-1", *GO], "0 or more"),
        (
            ["--fixture", "bench", "--channel", "CH3", "--kind", "zero", "--gas-value", "x"],
            "number",
        ),
        ([*SPAN, "--gas-unit", "vol%", "--band", "x", *GO], "positive number"),
        ([*SPAN, "--gas-unit", "vol%", "--response-factor", "x", *GO], "0 or more"),
        ([*SPAN, "--gas-unit", "vol%", "--interval", "5", *GO], "shorter than --max-gap"),
        ([*SPAN, "--gas-unit", "vol%", "--out", "no/such/dir/r.json", *GO], "not a directory"),
    ],
)
def test_bad_arguments_stop_before_the_port(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    assert message in usage_error(capsys, *argv)


def test_an_existing_record_is_not_replaced_without_force(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "record.json"
    out.write_text("{}", encoding="utf-8")
    argv = [*SPAN, "--gas-unit", "vol%", *GO, "--out", str(out)]
    assert "exists; pass --force" in usage_error(capsys, *argv)


def test_a_span_on_the_simulator_with_auto(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "span.json"
    code, text, _ = run(
        capsys,
        *SPAN,
        "--gas-unit",
        "vol%",
        "--gas-label",
        "20.95 % O2 in N2",
        *GO,
        "--auto",
        *QUICK,
        "--out",
        str(out),
        "--operator",
        "GB",
    )
    assert code == 0, text
    assert "Switch the gas at the inlet to 20.95 % O2 in N2 now" in text
    assert "Steady: CH3: steady" in text
    assert "The span completed: a read showed it running" in text
    assert "CH3: before" in text
    assert "detector counts: [" in text
    assert text.rstrip().endswith("status: completed")
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["format"] == "fujilib-calibration/1"
    assert (record["kind"], record["outcome"], record["operator"]) == ("span", "completed", "GB")
    assert record["named_gas"]["CH3"]["label"] == "20.95 % O2 in N2"
    assert record["analyzer"]["serial_number"] == "N8A0259T"


def test_asking_before_the_key_that_calibrates(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Yes each time it asks: it asks again if the gas moves before the key.
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n" * 5))
    out = tmp_path / "yes.json"
    code, text, _ = run(capsys, *SPAN, "--gas-unit", "vol%", *GO, *QUICK, "--out", str(out))
    assert code == 0, text
    assert "Calibrate CH3 now? [y/N]" in text
    assert text.rstrip().endswith("status: completed")


def test_answering_no_cancels(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))
    monkeypatch.chdir(tmp_path)
    code, text, _ = run(capsys, *SPAN, "--gas-unit", "vol%", *GO, *QUICK, "--no-adc")
    assert code == 1
    assert "The span cancelled" in text
    assert text.rstrip().endswith("status: cancelled")
    [path] = tmp_path.glob("fuji-calibration_N8A0259T_CH3_span_*.json")
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["outcome"] == "cancelled"
    assert record["detector_counts"] is None
    assert [k["key"] for k in record["keys"]] == ["SPAN", "DOWN", "DOWN", "ENT", "ESC"]


def test_a_gas_that_never_settles_is_cancelled(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    argv = [*SPAN, "--gas-unit", "vol%", *GO, "--auto", "--window", "5", "--response-factor"]
    argv += [
        "0",
        "--interval",
        "0.05",
        "--settle-timeout",
        "0.3",
        "--out",
        str(tmp_path / "t.json"),
    ]
    code, text, _ = run(capsys, *argv)
    assert code == 1
    assert "error: the gas was not steady within 0.3 s" in text
    assert text.rstrip().endswith("status: cancelled")


def test_a_refusal_before_any_key_is_recorded(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bank: dict[str, Any] = json.loads(BENCH_BANK_PATH.read_text(encoding="utf-8"))
    bank["holding"]["0049"] = 1  # key lock on
    fixture = tmp_path / "locked.json"
    fixture.write_text(json.dumps(bank), encoding="utf-8")
    out = tmp_path / "refused.json"
    argv = ["--fixture", str(fixture), "--channel", "CH3", "--kind", "span", "--gas-value"]
    argv += ["20.95", "--gas-unit", "vol%", *GO, *QUICK, "--out", str(out)]
    code, text, _ = run(capsys, *argv)
    assert code == 1
    assert "key lock is on" in text
    assert text.rstrip().endswith("status: refused")
    assert json.loads(out.read_text(encoding="utf-8"))["keys"] == []


def test_ctrl_c_reports_how_the_run_ended(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def interrupted(*_: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(calibrate, "_calibrate", interrupted)
    code, out, err = run(capsys, *SPAN, "--gas-unit", "vol%", *GO)
    assert code == 1
    assert err == "stopped by Ctrl-C\n"
    assert out.rstrip().endswith("status: cancelled")

    unclean = SimpleNamespace(cleanup=SimpleNamespace(clean=False), outcome=None)

    async def interrupted_unclean(_args: object, _gas: object, outcome: Any) -> int:
        outcome.result = unclean
        raise KeyboardInterrupt

    monkeypatch.setattr(calibrate, "_calibrate", interrupted_unclean)
    code, out, err = run(capsys, *SPAN, "--gas-unit", "vol%", *GO)
    assert code == 1
    assert "was not left clean" in err
    assert out.rstrip().endswith("status: not_clean")


def test_a_record_that_cannot_be_written_is_said_and_the_run_still_ends(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    argv = [*SPAN, "--gas-unit", "vol%", *GO, "--auto", *QUICK, "--out", str(tmp_path), "--force"]
    code, text, _ = run(capsys, *argv)
    assert code == 0
    assert "error: the record could not be written to" in text
    assert text.rstrip().endswith("status: completed")


class _Blocking:
    """Standard input that gives no answer until released."""

    def __init__(self) -> None:
        self.released = threading.Event()

    def readline(self) -> str:
        self.released.wait(5)
        return ""


@pytest.mark.anyio
async def test_a_read_failing_while_it_asks_ends_the_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = _Blocking()
    monkeypatch.setattr("sys.stdin", stdin)

    class Failing:
        async def read(self) -> None:
            raise FujiAnalyzerStateError("the panel left the wait step")

    try:
        with pytest.raises(FujiAnalyzerStateError, match="left the wait step"):
            await calibrate._ask_reading(Failing(), "Calibrate? ", 0.01)  # type: ignore[arg-type]
    finally:
        stdin.released.set()


def test_the_status_follows_the_outcome() -> None:
    assert calibrate._status("refused", None) == "refused"
    completed = SimpleNamespace(
        cleanup=SimpleNamespace(clean=True), outcome=ManualCalibrationOutcome.COMPLETED
    )
    assert calibrate._status("cancelled", completed) == "completed"  # type: ignore[arg-type]
    unclean = SimpleNamespace(cleanup=SimpleNamespace(clean=False), outcome=None)
    assert calibrate._status("stopped", unclean) == "not_clean"  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_a_run_never_entered_has_no_record(tmp_path: Path) -> None:
    run = SimpleNamespace(result=None)
    args = SimpleNamespace(out=tmp_path / "none.json")
    await calibrate._record(args, None, run)  # type: ignore[arg-type]
    assert not args.out.exists()


class _Run:
    """A run whose ``calibrate`` is refused as told, leaving the steadiness as told."""

    def __init__(self, refusals: list[tuple[bool | None, RunState]]) -> None:
        self.gases = {ChannelId.CH3: None}
        self.refusals = refusals
        self.state = RunState.WAITING
        self.steadiness: Any = None
        self.calibrated = 0

    async def wait_steady(self, *, progress: Any) -> Any:
        del progress
        return SimpleNamespace(reasons=("CH3: steady",))

    async def calibrate(self, *, confirm: bool) -> None:
        assert confirm
        if self.refusals:
            steady, self.state = self.refusals.pop(0)
            self.steadiness = None if steady is None else SimpleNamespace(steady=steady)
            raise FujiAnalyzerStateError("calibrate refused")
        self.calibrated += 1


@pytest.mark.anyio
async def test_a_gas_that_moves_before_the_key_is_waited_for_again(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = _Run([(False, RunState.WAITING)])
    assert await calibrate._decide(SimpleNamespace(auto=True), run) == "calibrated"  # type: ignore[arg-type]
    assert run.calibrated == 1
    assert "moved again before the key" in capsys.readouterr().out


@pytest.mark.anyio
@pytest.mark.parametrize(
    "refusal", [(True, RunState.WAITING), (None, RunState.WAITING), (False, RunState.ENDED)]
)
async def test_any_other_refusal_of_the_key_stands(refusal: tuple[bool | None, RunState]) -> None:
    run = _Run([refusal])
    with pytest.raises(FujiAnalyzerStateError, match="calibrate refused"):
        await calibrate._decide(SimpleNamespace(auto=True), run)  # type: ignore[arg-type]


def test_no_answer_at_all_is_no(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # input closed, as from a script
    out = tmp_path / "closed.json"
    code, text, _ = run(capsys, *SPAN, "--gas-unit", "vol%", *GO, *QUICK, "--out", str(out))
    assert code == 1
    assert text.rstrip().endswith("status: cancelled")
