"""The blocking facade, discovery and the command-line tools on a connected analyzer.

Read-only, and gated as every hardware test is (``conftest.py``). Discovery
probes only the port under test: the analyzer's station and the one next to
it, which should be empty on the bench line.
"""

from __future__ import annotations

import json

import pytest

from fujilib import FujiModbusTimeoutError
from fujilib.cli import configure, discover, read
from fujilib.sync import Fuji, find_devices

pytestmark = pytest.mark.hardware


def test_the_sync_facade(hardware_port: str, hardware_address: int) -> None:
    with Fuji.open(hardware_port, address=hardware_address) as anz:
        frame = anz.poll()
        assert frame.analyzer is not None
        assert anz.snapshot().connected
        info = anz.info
        assert info is not None
        assert anz.read_metadata().serial_number == info.serial_number


def test_discovery_on_the_port(
    hardware_port: str, hardware_address: int, other_address: int
) -> None:
    results = find_devices(ports=[hardware_port], addresses=(hardware_address, other_address))
    found, empty = results
    assert found.ok
    assert found.model is not None
    assert found.model.startswith("ZP")
    assert found.device_info is not None
    assert found.error is None
    assert not empty.ok
    assert isinstance(empty.error, FujiModbusTimeoutError)


def test_the_command_line_tools(
    capsys: pytest.CaptureFixture[str], hardware_port: str, hardware_address: int
) -> None:
    address = str(hardware_address)
    assert read.main([hardware_port, "--address", address, "--all", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["identity"]["address"] == hardware_address
    assert discover.main([hardware_port, "--addresses", address]) == 0
    assert "station" in capsys.readouterr().out
    assert configure.main(["dump", hardware_port, "--address", address]) == 0
    document = json.loads(capsys.readouterr().out)
    assert "response_time.o2" in document["settings"]
