"""Tests for the local Python toolchain prompt probe."""

import pytest

from tools import env_probe


@pytest.fixture(autouse=True)
def reset_probe_cache():
    env_probe._reset_cache_for_tests()
    yield
    env_probe._reset_cache_for_tests()


def test_clean_local_environment_returns_empty(monkeypatch):
    monkeypatch.setattr(env_probe, "_python_version_of", lambda binary: "3.13.3" if binary == "python3" else None)
    monkeypatch.setattr(env_probe, "_has_pip_module", lambda _binary: True)
    monkeypatch.setattr(env_probe, "_detect_pep668", lambda _binary: False)
    monkeypatch.setattr(env_probe, "_pip_python_version", lambda: "3.13")
    monkeypatch.setattr(env_probe.shutil, "which", lambda name: None)

    assert env_probe.get_environment_probe_line() == ""


def test_pep668_with_uv_returns_empty(monkeypatch):
    monkeypatch.setattr(env_probe, "_python_version_of", lambda binary: "3.12.4" if binary == "python3" else None)
    monkeypatch.setattr(env_probe, "_has_pip_module", lambda _binary: True)
    monkeypatch.setattr(env_probe, "_detect_pep668", lambda _binary: True)
    monkeypatch.setattr(env_probe, "_pip_python_version", lambda: "3.12")
    monkeypatch.setattr(env_probe.shutil, "which", lambda name: "/usr/local/bin/uv" if name == "uv" else None)

    assert env_probe.get_environment_probe_line() == ""


def test_problem_environment_emits_single_line(monkeypatch):
    monkeypatch.setattr(env_probe, "_python_version_of", lambda binary: {"python3": "3.11.15"}.get(binary))
    monkeypatch.setattr(env_probe, "_has_pip_module", lambda _binary: False)
    monkeypatch.setattr(env_probe, "_detect_pep668", lambda _binary: True)
    monkeypatch.setattr(env_probe, "_pip_python_version", lambda: "3.12")
    monkeypatch.setattr(env_probe.shutil, "which", lambda name: None if name == "uv" else f"/usr/bin/{name}")

    line = env_probe.get_environment_probe_line()

    assert line.startswith("Python toolchain: ")
    assert "\n" not in line
    assert "python3=3.11.15" in line
    assert "no pip module" in line
    assert "pip->python3.12 (mismatch)" in line
    assert "PEP 668" in line


def test_missing_python3_is_reported(monkeypatch):
    monkeypatch.setattr(env_probe, "_python_version_of", lambda _binary: None)
    monkeypatch.setattr(env_probe, "_has_pip_module", lambda _binary: False)
    monkeypatch.setattr(env_probe, "_detect_pep668", lambda _binary: False)
    monkeypatch.setattr(env_probe, "_pip_python_version", lambda: None)
    monkeypatch.setattr(env_probe.shutil, "which", lambda _name: None)

    line = env_probe.get_environment_probe_line()

    assert "python3=missing" in line
    assert "pip=missing" in line


@pytest.mark.parametrize("backend", ["docker", "modal", "ssh", "managed_modal"])
def test_remote_backends_stay_silent(monkeypatch, backend):
    monkeypatch.setenv("TERMINAL_ENV", backend)
    monkeypatch.setattr(env_probe, "_python_version_of", lambda _binary: None)
    monkeypatch.setattr(env_probe, "_has_pip_module", lambda _binary: False)

    assert env_probe.get_environment_probe_line() == ""


def test_result_is_cached(monkeypatch):
    calls = []

    def fake_python_version(binary):
        calls.append(binary)
        return "3.12.4" if binary == "python3" else None

    monkeypatch.setattr(env_probe, "_python_version_of", fake_python_version)
    monkeypatch.setattr(env_probe, "_has_pip_module", lambda _binary: True)
    monkeypatch.setattr(env_probe, "_detect_pep668", lambda _binary: False)
    monkeypatch.setattr(env_probe, "_pip_python_version", lambda: "3.12")
    monkeypatch.setattr(env_probe.shutil, "which", lambda _name: None)

    env_probe.get_environment_probe_line()
    env_probe.get_environment_probe_line()

    assert calls == ["python3", "python"]


def test_probe_failure_returns_empty(monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("simulated")

    monkeypatch.setattr(env_probe.subprocess, "run", boom)

    assert isinstance(env_probe.get_environment_probe_line(), str)
