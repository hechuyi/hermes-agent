"""Contract test: the s6-overlay stage2 hook accepts PUID/PGID as aliases for
HERMES_UID/HERMES_GID.

Regression guard for #15290.  NAS platforms (UGOS, Synology, unRAID) bind-mount
/opt/data from a host directory owned by the user's own UID and expect the
LinuxServer.io PUID/PGID convention.  Without the alias those vars are silently
ignored, the s6-setuidgid drop lands on UID 10000, and the runtime cannot read
the volume.  HERMES_UID/HERMES_GID must still take precedence when both are
set.

The s6-overlay rework moved bootstrap from docker/entrypoint.sh (now a shim)
to docker/stage2-hook.sh, which is installed as /etc/cont-init.d/01-hermes-setup
by the Dockerfile.  This test targets the post-rework location.
"""
from __future__ import annotations

import os
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
    if not STAGE2_HOOK.exists():
        pytest.skip("docker/stage2-hook.sh not present in this checkout")
    return STAGE2_HOOK.read_text()


def _alias_lines(text: str) -> list[str]:
    """The stage2 hook lines that resolve HERMES_UID/HERMES_GID from aliases."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith(("HERMES_UID=", "HERMES_GID="))
    ]


def _uid_gid_validation_block(text: str) -> str:
    start_marker = "invalid_uid_gid() {"
    end_marker = 'HERMES_GID="${HERMES_GID:-${PGID:-}}"\n'
    start = text.find(start_marker)
    end = text.find(end_marker, start)
    assert start != -1 and end != -1, (
        "expected alias resolution followed by explicit UID/GID validation"
    )
    block = text[start : end + len(end_marker)]
    for expected in (
        'validate_uid_gid HERMES_UID "$HERMES_UID"',
        'validate_uid_gid HERMES_GID "$HERMES_GID"',
        'validate_uid_gid PUID "$PUID"',
        'validate_uid_gid PGID "$PGID"',
    ):
        assert expected in block
    return block


def test_stage2_hook_resolves_puid_pgid_aliases(stage2_text: str) -> None:
    alias_lines = _alias_lines(stage2_text)
    assert any("PUID" in line for line in alias_lines), (
        "docker/stage2-hook.sh must resolve HERMES_UID from a PUID alias; see #15290"
    )
    assert any("PGID" in line for line in alias_lines), (
        "docker/stage2-hook.sh must resolve HERMES_GID from a PGID alias; see #15290"
    )


def _resolve(stage2_text: str, env: dict[str, str]) -> str:
    """Run the stage2 hook's alias-resolution lines in isolation and report the
    resolved ``HERMES_UID:HERMES_GID`` pair."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    script = "\n".join(_alias_lines(stage2_text))
    script += '\necho "${HERMES_UID:-}:${HERMES_GID:-}"\n'
    proc = subprocess.run(
        [bash, "-ec", script],
        env={"PATH": os.environ.get("PATH", "")} | env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_puid_pgid_populate_hermes_uid_gid(stage2_text: str) -> None:
    assert _resolve(stage2_text, {"PUID": "1000", "PGID": "10"}) == "1000:10"


def test_hermes_uid_gid_take_precedence_over_aliases(stage2_text: str) -> None:
    resolved = _resolve(
        stage2_text,
        {"HERMES_UID": "2000", "HERMES_GID": "2001", "PUID": "1000", "PGID": "10"},
    )
    assert resolved == "2000:2001"


def test_no_uid_vars_leaves_values_empty(stage2_text: str) -> None:
    # An empty resolution means the stage2 hook keeps the default hermes user.
    assert _resolve(stage2_text, {}) == ":"


def _validate(stage2_text: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    script = _uid_gid_validation_block(stage2_text)
    script += '\nprintf "VALID:%s:%s\\n" "${HERMES_UID:-}" "${HERMES_GID:-}"\n'
    return subprocess.run(
        [bash, "-ec", script],
        env={"PATH": os.environ.get("PATH", "")} | env,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"HERMES_UID": "1", "HERMES_GID": "65534"}, "VALID:1:65534"),
        ({"PUID": "4242", "PGID": "4243"}, "VALID:4242:4243"),
        ({"HERMES_UID": "2000", "PUID": "3000"}, "VALID:2000:"),
    ],
)
def test_uid_gid_validation_accepts_integer_range(
    stage2_text: str, env: dict[str, str], expected: str
) -> None:
    proc = _validate(stage2_text, env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


@pytest.mark.parametrize(
    ("env", "var_name", "value"),
    [
        ({"HERMES_UID": "0"}, "HERMES_UID", "0"),
        ({"HERMES_GID": "0"}, "HERMES_GID", "0"),
        ({"PUID": "65535"}, "PUID", "65535"),
        ({"PGID": "65535"}, "PGID", "65535"),
        ({"HERMES_UID": "abc"}, "HERMES_UID", "abc"),
        ({"HERMES_GID": "12x"}, "HERMES_GID", "12x"),
        ({"PUID": "-1"}, "PUID", "-1"),
        ({"PGID": ""}, "PGID", ""),
    ],
)
def test_uid_gid_validation_rejects_invalid_values(
    stage2_text: str, env: dict[str, str], var_name: str, value: str
) -> None:
    proc = _validate(stage2_text, env)
    assert proc.returncode == 1
    assert "[stage2] ERROR: invalid" in proc.stderr
    assert var_name in proc.stderr
    assert f"'{value}'" in proc.stderr
    assert "expected integer in range 1-65534 and not 0/root" in proc.stderr


def test_stage2_hook_runs_config_migration_as_hermes(stage2_text: str) -> None:
    assert "scripts/docker_config_migrate.py" in stage2_text
    assert 'as_hermes "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/docker_config_migrate.py"' in stage2_text
    assert "docker_config_migration_failed" in stage2_text
    assert "exit \"$migration_status\"" in stage2_text


def test_stage2_hook_documents_config_migration_opt_out(stage2_text: str) -> None:
    assert "HERMES_SKIP_CONFIG_MIGRATION" in stage2_text


def test_stage2_hook_does_not_warning_continue_config_migration_failure(stage2_text: str) -> None:
    migration_block_start = stage2_text.index("# --- Migrate persisted config schema ---")
    migration_block_end = stage2_text.index("# auth.json:", migration_block_start)
    migration_block = stage2_text[migration_block_start:migration_block_end]
    assert "Warning: docker_config_migrate.py failed; continuing" not in migration_block
    assert "|| echo" not in migration_block


def _migration_block(stage2_text: str) -> str:
    start = stage2_text.index("# --- Migrate persisted config schema ---")
    end = stage2_text.index("# auth.json:", start)
    block = stage2_text[start:end]
    match = re.search(r"(if \[ -f \"\$HERMES_HOME/config\.yaml\" \]; then\n(?:.*\n)*?fi)", block)
    assert match, "expected stage2 config migration if-block"
    return match.group(1)


def test_stage2_hook_propagates_config_migration_failure_status(stage2_text: str) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.yaml").write_text("_config_version: 0\n", encoding="utf-8")
        script = (
            "set -eu\n"
            f'HERMES_HOME="{home}"\n'
            'INSTALL_DIR="/opt/hermes"\n'
            'as_hermes() { return 37; }\n'
            + _migration_block(stage2_text)
            + "\n"
        )
        proc = subprocess.run([bash, "-c", script], capture_output=True, text=True)
    assert proc.returncode == 37
    assert "failure_class=docker_config_migration_failed" in proc.stderr
    assert "stage=config_migration" in proc.stderr
    assert "action=abort" in proc.stderr
    assert "exit_status=37" in proc.stderr
