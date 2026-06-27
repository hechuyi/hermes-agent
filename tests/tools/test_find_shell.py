"""Tests for shell selection used by background process spawning."""

import os
from unittest.mock import patch

import pytest

from tools.environments import local


def _fake_executable(tmp_path, name, mode=0o755):
    shell = tmp_path / name
    shell.write_text("#!/bin/sh\n")
    shell.chmod(mode)
    return str(shell)


@pytest.mark.parametrize("name", ["bash", "zsh", "dash", "sh", "ksh"])
def test_find_shell_prefers_compatible_posix_shell_env(tmp_path, name):
    shell = _fake_executable(tmp_path, name)

    with patch.object(local, "_IS_WINDOWS", False), \
        patch.dict(os.environ, {"SHELL": shell}), \
        patch.object(local, "_find_bash", return_value="/fallback/bash"):
        assert local._find_shell() == shell


@pytest.mark.parametrize(
    "name",
    ["fish", "csh", "tcsh", "nu", "nushell", "pwsh", "powershell"],
)
def test_find_shell_falls_back_for_incompatible_shell_env(tmp_path, name):
    shell = _fake_executable(tmp_path, name)

    with patch.object(local, "_IS_WINDOWS", False), \
        patch.dict(os.environ, {"SHELL": shell}), \
        patch.object(local, "_find_bash", return_value="/fallback/bash"):
        assert local._find_shell() == "/fallback/bash"


def test_find_shell_falls_back_when_shell_env_is_missing():
    with patch.object(local, "_IS_WINDOWS", False), \
        patch.dict(os.environ, {}, clear=True), \
        patch.object(local, "_find_bash", return_value="/fallback/bash"):
        assert local._find_shell() == "/fallback/bash"


def test_find_shell_falls_back_when_shell_env_is_empty():
    with patch.object(local, "_IS_WINDOWS", False), \
        patch.dict(os.environ, {"SHELL": ""}), \
        patch.object(local, "_find_bash", return_value="/fallback/bash"):
        assert local._find_shell() == "/fallback/bash"


def test_find_shell_falls_back_when_shell_env_is_not_a_file(tmp_path):
    missing_shell = str(tmp_path / "zsh")

    with patch.object(local, "_IS_WINDOWS", False), \
        patch.dict(os.environ, {"SHELL": missing_shell}), \
        patch.object(local, "_find_bash", return_value="/fallback/bash"):
        assert local._find_shell() == "/fallback/bash"


def test_find_shell_falls_back_when_shell_env_is_not_executable(tmp_path):
    shell = _fake_executable(tmp_path, "zsh", mode=0o644)

    with patch.object(local, "_IS_WINDOWS", False), \
        patch.dict(os.environ, {"SHELL": shell}), \
        patch.object(local, "_find_bash", return_value="/fallback/bash"):
        assert local._find_shell() == "/fallback/bash"


def test_find_shell_delegates_to_find_bash_on_windows(tmp_path):
    shell = _fake_executable(tmp_path, "zsh")

    with patch.object(local, "_IS_WINDOWS", True), \
        patch.dict(os.environ, {"SHELL": shell}), \
        patch.object(local, "_find_bash", return_value=r"C:\Git\bin\bash.exe"):
        assert local._find_shell() == r"C:\Git\bin\bash.exe"


def test_find_shell_does_not_return_incompatible_shell_when_bash_missing(tmp_path):
    shell = _fake_executable(tmp_path, "fish")

    with patch.object(local, "_IS_WINDOWS", False), \
        patch.dict(os.environ, {"SHELL": shell}), \
        patch.object(local, "_find_bash", return_value="/bin/sh"):
        assert local._find_shell() == "/bin/sh"
        assert local._find_shell() != shell


def test_find_bash_semantics_are_unchanged_for_posix_shell_env(tmp_path):
    shell = _fake_executable(tmp_path, "zsh")

    with patch.object(local, "_IS_WINDOWS", False), \
        patch.dict(os.environ, {"SHELL": shell}), \
        patch("tools.environments.local.shutil.which", return_value="/usr/bin/bash"):
        assert local._find_bash() == "/usr/bin/bash"
