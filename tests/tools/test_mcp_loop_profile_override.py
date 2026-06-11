"""Regression tests for HERMES_HOME override propagation onto the MCP loop."""

import os
import threading

import pytest


@pytest.fixture
def mcp_loop():
    import tools.mcp_tool as mcp_tool

    mcp_tool._ensure_mcp_loop()
    yield mcp_tool
    mcp_tool._stop_mcp_loop()


def test_override_propagates_to_mcp_loop(tmp_path, monkeypatch, mcp_loop):
    from hermes_constants import (
        get_hermes_home,
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    process_home = tmp_path / "proc-home"
    profile_home = tmp_path / "profile-home"
    process_home.mkdir()
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(process_home))

    async def read_home():
        return str(get_hermes_home())

    assert mcp_loop._run_on_mcp_loop(read_home(), timeout=10) == str(process_home)

    token = set_hermes_home_override(str(profile_home))
    try:
        assert mcp_loop._run_on_mcp_loop(read_home(), timeout=10) == str(profile_home)
        assert mcp_loop._run_on_mcp_loop(lambda: read_home(), timeout=10) == str(
            profile_home
        )
    finally:
        reset_hermes_home_override(token)

    assert mcp_loop._run_on_mcp_loop(read_home(), timeout=10) == str(process_home)


def test_oauth_token_paths_follow_override(tmp_path, monkeypatch, mcp_loop):
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    process_home = tmp_path / "proc-home"
    profile_home = tmp_path / "profile-home"
    process_home.mkdir()
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(process_home))

    async def token_path():
        from tools.mcp_oauth import HermesTokenStorage

        return str(HermesTokenStorage("probe-srv")._tokens_path())

    token = set_hermes_home_override(str(profile_home))
    try:
        path = mcp_loop._run_on_mcp_loop(token_path(), timeout=10)
    finally:
        reset_hermes_home_override(token)

    assert path.startswith(str(profile_home))
    assert os.path.join("mcp-tokens", "probe-srv.json") in path


def test_concurrent_scopes_do_not_interfere(tmp_path, monkeypatch, mcp_loop):
    from hermes_constants import (
        get_hermes_home,
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    process_home = tmp_path / "proc-home"
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    for home in (process_home, home_a, home_b):
        home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(process_home))

    async def read_home():
        return str(get_hermes_home())

    results = {}

    def scoped_call(key, home):
        token = set_hermes_home_override(str(home))
        try:
            results[key] = mcp_loop._run_on_mcp_loop(read_home(), timeout=10)
        finally:
            reset_hermes_home_override(token)

    threads = [
        threading.Thread(target=scoped_call, args=("a", home_a)),
        threading.Thread(target=scoped_call, args=("b", home_b)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert results == {"a": str(home_a), "b": str(home_b)}


def test_wrap_is_noop_without_override(mcp_loop):
    async def trivial():
        return 42

    coro = trivial()
    wrapped = mcp_loop._wrap_with_home_override(coro)
    assert wrapped is coro
    coro.close()
