"""Test helpers: the family's arrow-format frame fixtures (design §2.10, §10).

An arrow fixture is plain text, one frame per line, as hex bytes::

    > 01 03 00 04 00 02 85 CA          # a request the host sends
    < 01 03 04 00 00 03 E8 FA 8D       # the analyzer's reply

- ``>`` starts an exchange with a request; ``<`` adds reply bytes to it
  (several ``<`` lines are concatenated). A request with no ``<`` has no reply.
- ``#`` starts a comment anywhere on a line; blank lines are ignored.
- Hex may be separated by spaces, colons or commas.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from fujilib.errors import ErrorContext, FujiValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["Exchange", "hex_to_bytes", "parse_arrow_fixture", "replay_script"]


@dataclass(frozen=True, slots=True)
class Exchange:
    """One request and the reply that followed it, from an arrow fixture."""

    request: bytes
    response: bytes | None
    line: int
    """The 1-based line of the request."""
    comment: str
    """The request line's comment, without the ``#``."""


def hex_to_bytes(text: str) -> bytes:
    """Parse hex bytes separated by spaces, colons or commas.

    Raises:
        FujiValidationError: a token is not one or two hex digits, or ``text`` is empty.
    """
    tokens = text.replace(":", " ").replace(",", " ").split()
    if not tokens:
        msg = "no hex bytes"
        raise FujiValidationError(msg)
    out = bytearray()
    for token in tokens:
        if len(token) > 2:  # noqa: PLR2004
            if len(token) % 2:
                msg = f"odd-length hex {token!r}"
                raise FujiValidationError(msg)
            pairs = [token[i : i + 2] for i in range(0, len(token), 2)]
        else:
            pairs = [token]
        for pair in pairs:
            try:
                out.append(int(pair, 16))
            except ValueError:
                msg = f"{pair!r} is not a hex byte"
                raise FujiValidationError(msg) from None
    return bytes(out)


def parse_arrow_fixture(source: str | Path, *, name: str | None = None) -> tuple[Exchange, ...]:
    """Parse an arrow fixture from a path or from its text.

    Args:
        source: A :class:`~pathlib.Path` to read, or the fixture text itself.
        name: The label for error messages; defaults to the path, or ``<string>``.

    Raises:
        FujiValidationError: a malformed line; the message starts with ``name:line``.
    """
    if isinstance(source, Path):
        label = name or str(source)
        text = source.read_text(encoding="utf-8")
    else:
        label = name or "<string>"
        text = source
    exchanges: list[Exchange] = []
    request: tuple[bytes, int, str] | None = None
    response: bytearray | None = None

    def flush() -> None:
        if request is not None:
            data, line, comment = request
            reply = bytes(response) if response is not None else None
            exchanges.append(Exchange(data, reply, line, comment))

    for number, raw in enumerate(text.splitlines(), start=1):
        content, _, comment = raw.partition("#")
        content = content.strip()
        if not content:
            continue
        marker, _, payload = content.partition(" ")
        try:
            data = hex_to_bytes(payload)
        except FujiValidationError as exc:
            msg = f"{label}:{number}: {exc}"
            raise FujiValidationError(msg, context=ErrorContext(extra={"line": number})) from None
        if marker == ">":
            flush()
            request, response = (data, number, comment.strip()), None
        elif marker == "<":
            if request is None:
                msg = f"{label}:{number}: a reply before any request"
                raise FujiValidationError(msg, context=ErrorContext(extra={"line": number}))
            response = (response or bytearray()) + data
        else:
            msg = f"{label}:{number}: a line must start with '>' or '<', not {marker!r}"
            raise FujiValidationError(msg, context=ErrorContext(extra={"line": number}))
    flush()
    return tuple(exchanges)


def replay_script(exchanges: tuple[Exchange, ...]) -> Mapping[bytes, bytes]:
    """Map each request to its reply, for replaying a fixture to a client.

    Requests without a reply are left out.

    Raises:
        FujiValidationError: the same request appears with two different replies.
    """
    script: dict[bytes, bytes] = {}
    for exchange in exchanges:
        if exchange.response is None:
            continue
        known = script.get(exchange.request)
        if known is not None and known != exchange.response:
            msg = f"line {exchange.line}: this request already has a different reply"
            raise FujiValidationError(msg, context=ErrorContext(extra={"line": exchange.line}))
        script[exchange.request] = exchange.response
    return MappingProxyType(script)
