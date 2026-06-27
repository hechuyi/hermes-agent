"""Contract tests for the Docker --user guard in stage2-hook and main-wrapper."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE2_HOOK = REPO_ROOT / "docker" / "stage2-hook.sh"
MAIN_WRAPPER = REPO_ROOT / "docker" / "main-wrapper.sh"


def _read(path: Path) -> str:
    if not path.exists():
        pytest.skip(f"{path} not present in this checkout")
    return path.read_text()


def _guard_block(text: str) -> str:
    m = re.search(
        r"(reject_arbitrary_user\(\) \{.*?\n\}\n\nreject_arbitrary_user)",
        text,
        re.S,
    )
    assert m, "expected shared --user guard function and invocation"
    return m.group(1)


@pytest.mark.parametrize("path", [STAGE2_HOOK, MAIN_WRAPPER])
def test_guard_present_and_mentions_remediation(path: Path) -> None:
    block = _guard_block(_read(path))
    assert '"$cur_uid" = 0' in block
    assert '"$cur_uid" = "$hermes_uid"' in block
    assert '"$cur_gid" = "$hermes_gid"' in block
    assert "exit 1" in block
    assert "HERMES_UID" in block and "HERMES_GID" in block
    assert "PUID" in block and "PGID" in block


def _run_guard(
    text: str,
    *,
    cur_uid: int,
    cur_gid: int = 10000,
    hermes_uid: int = 10000,
    hermes_gid: int = 10000,
) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    script = (
        "set -e\n"
        "id() {\n"
        f'  if [ "${{1:-}}" = -u ] && [ "${{2:-}}" = hermes ]; then echo {hermes_uid};\n'
        f'  elif [ "${{1:-}}" = -g ] && [ "${{2:-}}" = hermes ]; then echo {hermes_gid};\n'
        f'  elif [ "${{1:-}}" = -u ]; then echo {cur_uid};\n'
        f'  elif [ "${{1:-}}" = -g ]; then echo {cur_gid};\n'
        "  else return 64; fi\n"
        "}\n"
        + _guard_block(text)
        + "\necho GUARD_PASSED\n"
    )
    with tempfile.TemporaryDirectory() as d:
        script_path = Path(d) / "guard.sh"
        script_path.write_text(script)
        return subprocess.run([bash, str(script_path)], capture_output=True, text=True)


def test_arbitrary_user_uid_is_rejected() -> None:
    for text in (_read(STAGE2_HOOK), _read(MAIN_WRAPPER)):
        proc = _run_guard(text, cur_uid=1000, hermes_uid=10000)
        assert proc.returncode == 1, proc.stderr
        assert "arbitrary, non-hermes UID" in proc.stderr
        assert "not supported" in proc.stderr
        assert "GUARD_PASSED" not in proc.stdout


def test_root_start_passes() -> None:
    for text in (_read(STAGE2_HOOK), _read(MAIN_WRAPPER)):
        proc = _run_guard(text, cur_uid=0, hermes_uid=10000)
        assert proc.returncode == 0, proc.stderr
        assert "GUARD_PASSED" in proc.stdout


def test_user_pinned_to_hermes_uid_passes() -> None:
    for text in (_read(STAGE2_HOOK), _read(MAIN_WRAPPER)):
        proc = _run_guard(text, cur_uid=10000, hermes_uid=10000)
        assert proc.returncode == 0, proc.stderr
        assert "GUARD_PASSED" in proc.stdout


def test_user_pinned_to_remapped_hermes_uid_passes() -> None:
    for text in (_read(STAGE2_HOOK), _read(MAIN_WRAPPER)):
        proc = _run_guard(text, cur_uid=4242, cur_gid=4243, hermes_uid=4242, hermes_gid=4243)
        assert proc.returncode == 0, proc.stderr
        assert "GUARD_PASSED" in proc.stdout


def test_user_pinned_to_hermes_uid_with_arbitrary_gid_is_rejected() -> None:
    for text in (_read(STAGE2_HOOK), _read(MAIN_WRAPPER)):
        proc = _run_guard(text, cur_uid=10000, cur_gid=1234, hermes_uid=10000, hermes_gid=10000)
        assert proc.returncode == 1
        assert "arbitrary, non-hermes UID/GID" in proc.stderr
        assert "GUARD_PASSED" not in proc.stdout


def _run_function(text: str, function_name: str, *, cur_uid: int, command: str) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    m = re.search(rf"({function_name}\(\) \{{.*?\n\}})", text, re.S)
    assert m, f"expected {function_name} helper"
    with tempfile.TemporaryDirectory() as d:
        stub_dir = Path(d) / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "s6-setuidgid"
        stub.write_text('#!/bin/sh\nprintf "S6:%s\\n" "$*"\n')
        stub.chmod(0o755)
        script = (
            "set -e\n"
            f"PATH={stub_dir}:$PATH\n"
            f'id() {{ if [ "${{1:-}}" = -u ]; then echo {cur_uid}; else return 64; fi; }}\n'
            + m.group(1)
            + f"\n{command}\n"
        )
        script_path = Path(d) / "helper.sh"
        script_path.write_text(script)
        return subprocess.run([bash, str(script_path)], capture_output=True, text=True)


def test_stage2_as_hermes_skips_s6_setuidgid_when_already_non_root() -> None:
    proc = _run_function(
        _read(STAGE2_HOOK),
        "as_hermes",
        cur_uid=10000,
        command='as_hermes printf "DIRECT:%s\\n" ok',
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "DIRECT:ok\n"


def test_stage2_as_hermes_uses_s6_setuidgid_when_root() -> None:
    proc = _run_function(
        _read(STAGE2_HOOK),
        "as_hermes",
        cur_uid=0,
        command='as_hermes printf "DIRECT:%s\\n" ok',
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "S6:hermes printf DIRECT:%s\\n ok\n"


def test_main_wrapper_drop_skips_s6_setuidgid_when_already_non_root() -> None:
    proc = _run_function(
        _read(MAIN_WRAPPER),
        "drop",
        cur_uid=10000,
        command='drop printf "DIRECT:%s\\n" ok',
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "DIRECT:ok\n"


def test_main_wrapper_drop_uses_s6_setuidgid_when_root() -> None:
    proc = _run_function(
        _read(MAIN_WRAPPER),
        "drop",
        cur_uid=0,
        command='drop printf "DIRECT:%s\\n" ok',
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "S6:hermes printf DIRECT:%s\\n ok\n"
