from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE2_HOOK = REPO_ROOT / "docker" / "stage2-hook.sh"


@pytest.fixture(scope="module")
def stage2_text() -> str:
    return STAGE2_HOOK.read_text(encoding="utf-8")


def _toplevel_chown_loop(text: str) -> str:
    match = re.search(r"(for f in \\\n(?:.*\\\n)*?.*; do\n(?:.*\n)*?done)", text)
    assert match, "stage2 hook must contain a top-level state-file chown allowlist"
    block = match.group(1)
    assert 'chown hermes:hermes "$HERMES_HOME/$f"' in block
    return block


def test_toplevel_chown_allowlist_covers_runtime_state_files(stage2_text: str) -> None:
    block = _toplevel_chown_loop(stage2_text)
    for filename in ("auth.json", "state.db", "gateway.lock", "gateway_state.json"):
        assert filename in block


def test_toplevel_chown_does_not_blanket_sweep_host_files(stage2_text: str) -> None:
    assert not re.search(r"find\s+\"?\$\{?HERMES_HOME\}?\"?[^\n]*-user\s+root", stage2_text)


def _run_loop(stage2_text: str, present_files: list[str]) -> list[str]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "home"
        home.mkdir()
        for filename in present_files:
            (home / filename).touch()
        (home / "host_secret.json").touch()

        script = (
            "set -eu\n"
            f'HERMES_HOME="{home}"\n'
            f'chown() {{ for arg in "$@"; do :; done; echo "${{arg##*/}}" >> "{tmp_path}/chown.log"; }}\n'
            + _toplevel_chown_loop(stage2_text)
        )
        harness = tmp_path / "harness.sh"
        harness.write_text(script, encoding="utf-8")
        proc = subprocess.run([bash, str(harness)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

        log = tmp_path / "chown.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_toplevel_chown_only_touches_present_allowlisted_files(stage2_text: str) -> None:
    touched = _run_loop(stage2_text, ["auth.json", "state.db", "gateway.lock"])
    assert "auth.json" in touched
    assert "state.db" in touched
    assert "gateway.lock" in touched
    assert "gateway_state.json" not in touched


def test_toplevel_chown_skips_nonallowlisted_host_file(stage2_text: str) -> None:
    touched = _run_loop(stage2_text, ["auth.json"])
    assert "host_secret.json" not in touched
