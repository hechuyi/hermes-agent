"""Tests for Hermes sandbox/container mirror write classification."""

from __future__ import annotations

import pytest


@pytest.fixture
def fake_hermes_root(tmp_path):
    root = tmp_path / "host-hermes"
    root.mkdir()
    return root


def test_classifies_sandbox_hermes_root_file(fake_hermes_root, monkeypatch):
    import agent.file_safety as fs

    host_home = fake_hermes_root / "profiles" / "feishu"
    host_home.mkdir(parents=True)
    monkeypatch.setattr(fs, "_hermes_home_path", lambda: host_home)
    monkeypatch.setattr(fs, "_hermes_root_path", lambda: fake_hermes_root)

    info = fs.classify_sandbox_mirror_target("/workspace/.hermes/SOUL.md")

    assert info is not None
    assert info["area"] == "SOUL.md"
    assert info["mirror_root"] == "/workspace/.hermes"


def test_classifies_sandbox_hermes_profile_area(fake_hermes_root, monkeypatch):
    import agent.file_safety as fs

    host_home = fake_hermes_root / "profiles" / "feishu"
    host_home.mkdir(parents=True)
    monkeypatch.setattr(fs, "_hermes_home_path", lambda: host_home)
    monkeypatch.setattr(fs, "_hermes_root_path", lambda: fake_hermes_root)

    info = fs.classify_sandbox_mirror_target(
        "/workspace/project/.hermes/memories/MEMORY.md"
    )

    assert info is not None
    assert info["area"] == "memories"
    assert info["relative_path"] == "memories/MEMORY.md"


def test_real_host_hermes_home_is_not_a_sandbox_mirror(fake_hermes_root, monkeypatch):
    import agent.file_safety as fs

    host_home = fake_hermes_root / "profiles" / "feishu"
    host_home.mkdir(parents=True)
    monkeypatch.setattr(fs, "_hermes_home_path", lambda: host_home)
    monkeypatch.setattr(fs, "_hermes_root_path", lambda: fake_hermes_root)

    assert fs.classify_sandbox_mirror_target(str(host_home / "SOUL.md")) is None
    assert (
        fs.classify_sandbox_mirror_target(str(fake_hermes_root / "memories" / "MEMORY.md"))
        is None
    )


def test_unrelated_dot_hermes_cache_is_not_classified(fake_hermes_root, monkeypatch):
    import agent.file_safety as fs

    host_home = fake_hermes_root / "profiles" / "feishu"
    host_home.mkdir(parents=True)
    monkeypatch.setattr(fs, "_hermes_home_path", lambda: host_home)
    monkeypatch.setattr(fs, "_hermes_root_path", lambda: fake_hermes_root)

    assert fs.classify_sandbox_mirror_target("/workspace/.hermes/cache/tmp.json") is None


def test_container_mirror_warning_requires_nonlocal_backend(fake_hermes_root, monkeypatch):
    import agent.file_safety as fs

    host_home = fake_hermes_root / "profiles" / "feishu"
    host_home.mkdir(parents=True)
    monkeypatch.setattr(fs, "_hermes_home_path", lambda: host_home)
    monkeypatch.setattr(fs, "_hermes_root_path", lambda: fake_hermes_root)

    assert fs.get_container_mirror_warning("/workspace/.hermes/SOUL.md", "local") is None
    warning = fs.get_container_mirror_warning("/workspace/.hermes/SOUL.md", "docker")
    assert warning is not None
    assert "docker" in warning
    assert "sandbox-local .hermes mirror" in warning
