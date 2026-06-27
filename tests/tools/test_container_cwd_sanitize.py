"""Regression tests for container cwd sanitization across tool entrypoints."""

import json
import threading
from unittest.mock import MagicMock, patch

import tools.code_execution_tool as code_execution_tool
import tools.file_tools as file_tools
import tools.terminal_tool as terminal_tool


def _container_config(env_type="docker", cwd="/root"):
    return {
        "env_type": env_type,
        "docker_image": "python:latest",
        "singularity_image": "docker://python:latest",
        "modal_image": "python:latest",
        "daytona_image": "python:latest",
        "cwd": cwd,
        "host_cwd": None,
        "timeout": 60,
        "lifetime_seconds": 3600,
        "container_cpu": 1,
        "container_memory": 5120,
        "container_disk": 51200,
        "container_persistent": True,
        "docker_volumes": [],
        "docker_env": {},
        "docker_extra_args": [],
        "docker_mount_cwd_to_workspace": False,
        "docker_run_as_host_user": False,
        "docker_forward_env": [],
        "docker_persist_across_processes": False,
        "docker_orphan_reaper": True,
        "modal_mode": "auto",
    }


def _local_config(cwd="/host/project"):
    config = _container_config(env_type="local", cwd=cwd)
    config["local_persistent"] = False
    return config


def _capture_terminal_created_cwd(monkeypatch, override_cwd, config_cwd="/root"):
    captured = {}

    class FakeEnv:
        cwd = config_cwd

        def execute(self, command, **kwargs):
            return {"output": "ok", "returncode": 0}

    def fake_create_environment(**kwargs):
        captured.update(kwargs)
        return FakeEnv()

    task_id = "terminal-cwd-sanitize"
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _container_config(cwd=config_cwd))
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks_lock", threading.Lock())
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda *a, **k: {"approved": True})
    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)

    terminal_tool.register_task_env_overrides(task_id, {"cwd": override_cwd})
    try:
        result = json.loads(terminal_tool.terminal_tool("pwd", task_id=task_id))
    finally:
        terminal_tool.clear_task_env_overrides(task_id)

    assert result["exit_code"] == 0
    return captured["cwd"]


class TestContainerCwdSanitizer:
    def test_host_prefixes_include_windows_and_posix(self):
        assert "/Users/" in terminal_tool._HOST_CWD_PREFIXES
        assert "/home/" in terminal_tool._HOST_CWD_PREFIXES
        assert "C:\\" in terminal_tool._HOST_CWD_PREFIXES
        assert "C:/" in terminal_tool._HOST_CWD_PREFIXES

    def test_container_backends_set(self):
        assert terminal_tool._CONTAINER_BACKENDS == frozenset(
            {"docker", "singularity", "modal", "daytona"}
        )

    def test_sanitize_rejects_host_and_relative_container_cwd(self):
        default = "/root"
        unusable = [
            r"C:\Users\someuser",
            "C:/Users/someuser",
            "/home/someuser/project",
            "/Users/someuser/project",
            ".",
            "src/app",
        ]

        for cwd in unusable:
            assert terminal_tool.sanitize_container_cwd(cwd, default, "docker") == default

    def test_sanitize_preserves_in_container_absolute_paths(self):
        for cwd in ("/workspace", "/root", "/app", "/opt/project"):
            assert terminal_tool.sanitize_container_cwd(cwd, "/root", "docker") == cwd

    def test_sanitize_leaves_non_container_backends_untouched(self):
        for env_type in ("local", "ssh"):
            assert (
                terminal_tool.sanitize_container_cwd("relative/path", "/root", env_type)
                == "relative/path"
            )

    def test_empty_cwd_uses_default_for_container_backend(self):
        assert terminal_tool.sanitize_container_cwd("", "/root", "docker") == "/root"
        assert terminal_tool.sanitize_container_cwd(None, "/root", "docker") == "/root"


class TestTerminalContainerCwdSanitize:
    def test_terminal_creation_sanitizes_windows_backslash_override(self, monkeypatch):
        assert _capture_terminal_created_cwd(monkeypatch, r"C:\Users\someuser") == "/root"

    def test_terminal_creation_sanitizes_windows_forwardslash_override(self, monkeypatch):
        assert _capture_terminal_created_cwd(monkeypatch, "C:/Users/someuser") == "/root"

    def test_terminal_creation_sanitizes_posix_host_override(self, monkeypatch):
        assert _capture_terminal_created_cwd(monkeypatch, "/home/someuser/project") == "/root"

    def test_terminal_creation_sanitizes_macos_host_override(self, monkeypatch):
        assert _capture_terminal_created_cwd(monkeypatch, "/Users/someuser/project") == "/root"

    def test_terminal_creation_sanitizes_relative_override(self, monkeypatch):
        assert _capture_terminal_created_cwd(monkeypatch, "src/app") == "/root"

    def test_terminal_creation_preserves_container_override(self, monkeypatch):
        assert _capture_terminal_created_cwd(monkeypatch, "/workspace/task") == "/workspace/task"


class TestLiveEnvSyncContainerCwdSanitize:
    def test_live_container_env_does_not_receive_host_cwd_override(self, monkeypatch):
        class FakeDockerEnv:
            cwd = "/workspace/live"

        fake_env = FakeDockerEnv()
        task_id = "live-container-cwd"

        monkeypatch.setattr(terminal_tool, "_active_environments", {task_id: fake_env})
        monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
        monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _container_config(cwd="/root"))

        terminal_tool.register_task_env_overrides(task_id, {"cwd": r"C:\Users\someuser"})

        assert fake_env.cwd == "/root"

    def test_live_container_env_preserves_container_cwd_override(self, monkeypatch):
        class FakeDockerEnv:
            cwd = "/workspace/live"

        fake_env = FakeDockerEnv()
        task_id = "live-container-cwd-valid"

        monkeypatch.setattr(terminal_tool, "_active_environments", {task_id: fake_env})
        monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
        monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _container_config(cwd="/root"))

        terminal_tool.register_task_env_overrides(task_id, {"cwd": "/opt/project"})

        assert fake_env.cwd == "/opt/project"

    def test_live_local_env_still_receives_host_or_relative_override(self, monkeypatch):
        class FakeLocalEnv:
            cwd = "/old/local"

        fake_env = FakeLocalEnv()
        task_id = "live-local-cwd"

        monkeypatch.setattr(terminal_tool, "_active_environments", {task_id: fake_env})
        monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
        monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _local_config(cwd="/old/local"))

        terminal_tool.register_task_env_overrides(task_id, {"cwd": "relative/project"})

        assert fake_env.cwd == "relative/project"


class TestFileToolsContainerCwdSanitize:
    def test_file_tools_sanitizes_task_override_before_container_creation(self):
        captured = {}
        task_id = "file-tools-cwd"

        def fake_create_env(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("tools.terminal_tool._get_env_config", return_value=_container_config(cwd="/root")), \
             patch("tools.terminal_tool._task_env_overrides", {task_id: {"cwd": "/Users/someuser/project"}}), \
             patch("tools.terminal_tool._active_environments", {}), \
             patch("tools.terminal_tool._creation_locks", {}), \
             patch("tools.terminal_tool._creation_locks_lock", threading.Lock()), \
             patch("tools.terminal_tool._create_environment", side_effect=fake_create_env), \
             patch("tools.terminal_tool._start_cleanup_thread"), \
             patch("tools.file_tools._file_ops_cache", {}), \
             patch("tools.file_tools._file_ops_lock", threading.Lock()):
            file_tools._get_file_ops(task_id)

        assert captured["cwd"] == "/root"

    def test_file_tools_preserves_container_task_override(self):
        captured = {}
        task_id = "file-tools-cwd-valid"

        def fake_create_env(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("tools.terminal_tool._get_env_config", return_value=_container_config(cwd="/root")), \
             patch("tools.terminal_tool._task_env_overrides", {task_id: {"cwd": "/app"}}), \
             patch("tools.terminal_tool._active_environments", {}), \
             patch("tools.terminal_tool._creation_locks", {}), \
             patch("tools.terminal_tool._creation_locks_lock", threading.Lock()), \
             patch("tools.terminal_tool._create_environment", side_effect=fake_create_env), \
             patch("tools.terminal_tool._start_cleanup_thread"), \
             patch("tools.file_tools._file_ops_cache", {}), \
             patch("tools.file_tools._file_ops_lock", threading.Lock()):
            file_tools._get_file_ops(task_id)

        assert captured["cwd"] == "/app"


class TestCodeExecutionContainerCwdSanitize:
    def test_code_execution_sanitizes_task_override_before_container_creation(self):
        captured = {}
        task_id = "code-exec-cwd"

        def fake_create_env(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("tools.terminal_tool._get_env_config", return_value=_container_config(cwd="/root")), \
             patch("tools.terminal_tool._task_env_overrides", {task_id: {"docker_image": "custom:latest", "cwd": "relative/project"}}), \
             patch("tools.terminal_tool._active_environments", {}), \
             patch("tools.terminal_tool._creation_locks", {}), \
             patch("tools.terminal_tool._creation_locks_lock", threading.Lock()), \
             patch("tools.terminal_tool._create_environment", side_effect=fake_create_env), \
             patch("tools.terminal_tool._start_cleanup_thread"):
            code_execution_tool._get_or_create_env(task_id)

        assert captured["cwd"] == "/root"

    def test_code_execution_preserves_container_task_override(self):
        captured = {}
        task_id = "code-exec-cwd-valid"

        def fake_create_env(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("tools.terminal_tool._get_env_config", return_value=_container_config(cwd="/root")), \
             patch("tools.terminal_tool._task_env_overrides", {task_id: {"docker_image": "custom:latest", "cwd": "/opt/project"}}), \
             patch("tools.terminal_tool._active_environments", {}), \
             patch("tools.terminal_tool._creation_locks", {}), \
             patch("tools.terminal_tool._creation_locks_lock", threading.Lock()), \
             patch("tools.terminal_tool._create_environment", side_effect=fake_create_env), \
             patch("tools.terminal_tool._start_cleanup_thread"):
            code_execution_tool._get_or_create_env(task_id)

        assert captured["cwd"] == "/opt/project"

    def test_code_execution_reads_raw_task_cwd_only_override(self):
        captured = {}
        task_id = "code-exec-cwd-only-valid"

        def fake_create_env(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("tools.terminal_tool._get_env_config", return_value=_container_config(cwd="/root")), \
             patch("tools.terminal_tool._task_env_overrides", {task_id: {"cwd": "/opt/project"}}), \
             patch("tools.terminal_tool._active_environments", {}), \
             patch("tools.terminal_tool._creation_locks", {}), \
             patch("tools.terminal_tool._creation_locks_lock", threading.Lock()), \
             patch("tools.terminal_tool._create_environment", side_effect=fake_create_env), \
             patch("tools.terminal_tool._start_cleanup_thread"):
            code_execution_tool._get_or_create_env(task_id)

        assert captured["cwd"] == "/opt/project"
