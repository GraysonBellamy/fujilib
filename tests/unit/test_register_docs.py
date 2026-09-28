"""The committed register reference matches the registry (design §5.1)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_register_docs_are_current() -> None:
    """If this fails, run ``python scripts/gen_register_docs.py`` and commit the result."""
    result = subprocess.run(
        [sys.executable, "scripts/gen_register_docs.py", "--check"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, f"stale docs/registers.md:\n{result.stderr}"
