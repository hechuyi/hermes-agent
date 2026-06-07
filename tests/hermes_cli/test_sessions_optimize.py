"""Tests for ``hermes sessions optimize``."""

from __future__ import annotations

import os
import subprocess
import sys


def test_sessions_optimize_cli_smoke(tmp_path):
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / ".hermes")
    env["HERMES_DISABLE_HOOKS"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "sessions", "optimize"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "Optimizing session store" in result.stdout
    assert "Optimized" in result.stdout
    assert "Database size:" in result.stdout
