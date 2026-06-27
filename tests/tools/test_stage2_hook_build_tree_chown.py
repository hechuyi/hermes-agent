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


def _build_tree_block(text: str) -> str:
    match = re.search(r"(venv_owner=\$\(stat[^\n]*\n(?:.*\n)*?fi)", text)
    assert match, "stage2 hook must probe .venv ownership before chowning build trees"
    return match.group(1)


def test_build_tree_chown_is_gated_independently_of_hermes_home(stage2_text: str) -> None:
    block = _build_tree_block(stage2_text)
    assert "$INSTALL_DIR/.venv" in block
    assert "$INSTALL_DIR/node_modules" in block
    assert "$INSTALL_DIR/ui-tui" not in block


def _run_build_tree_block(stage2_text: str, *, venv_owner: int, hermes_uid: int) -> bool:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log = tmp_path / "chown.log"
        script = (
            "set -eu\n"
            'INSTALL_DIR="/opt/hermes"\n'
            f"actual_hermes_uid={hermes_uid}\n"
            f"stat() {{ echo {venv_owner}; }}\n"
            f'chown() {{ echo fired >> "{log}"; }}\n'
            + _build_tree_block(stage2_text)
        )
        harness = tmp_path / "harness.sh"
        harness.write_text(script, encoding="utf-8")
        proc = subprocess.run([bash, str(harness)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return log.exists() and "fired" in log.read_text(encoding="utf-8")


def test_build_tree_chown_fires_after_uid_remap_even_if_home_already_matches(stage2_text: str) -> None:
    assert _run_build_tree_block(stage2_text, venv_owner=10000, hermes_uid=4242)


def test_build_tree_chown_skips_when_venv_already_owned(stage2_text: str) -> None:
    assert not _run_build_tree_block(stage2_text, venv_owner=4242, hermes_uid=4242)
