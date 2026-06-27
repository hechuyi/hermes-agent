"""Tests for gateway/channel_directory.py — channel resolution and display."""

import asyncio
import json
import os
from unittest.mock import patch

from gateway.channel_directory import (
    build_channel_directory,
    lookup_channel_type,
    resolve_channel_name,
    format_directory_for_display,
    load_directory,
    _build_from_sessions,
)


def _write_directory(tmp_path, platforms):
    """Helper to write a fake channel directory."""
    data = {"updated_at": "2026-01-01T00:00:00", "platforms": platforms}
    cache_file = tmp_path / "channel_directory.json"
    cache_file.write_text(json.dumps(data))
    return cache_file


class TestLoadDirectory:
    def test_missing_file(self, tmp_path):
        with patch("gateway.channel_directory.DIRECTORY_PATH", tmp_path / "nope.json"):
            result = load_directory()
        assert result["updated_at"] is None
        assert result["platforms"] == {}

    def test_valid_file(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "feishu": [{"id": "oc_123", "name": "Ops", "type": "group"}]
        })
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            result = load_directory()
        assert result["platforms"]["feishu"][0]["name"] == "Ops"

    def test_corrupt_file(self, tmp_path):
        cache_file = tmp_path / "channel_directory.json"
        cache_file.write_text("{bad json")
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            result = load_directory()
        assert result["updated_at"] is None


class TestBuildChannelDirectoryWrites:
    def test_failed_write_preserves_previous_cache(self, tmp_path, monkeypatch):
        cache_file = _write_directory(tmp_path, {
            "feishu": [{"id": "oc_123", "name": "Ops", "type": "group"}]
        })
        previous = json.loads(cache_file.read_text())

        def broken_dump(data, fp, *args, **kwargs):
            fp.write('{"updated_at":')
            fp.flush()
            raise OSError("disk full")

        monkeypatch.setattr(json, "dump", broken_dump)

        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            asyncio.run(build_channel_directory({}))
            result = load_directory()

        assert result == previous


class TestResolveChannelName:
    def _setup(self, tmp_path, platforms):
        cache_file = _write_directory(tmp_path, platforms)
        return patch("gateway.channel_directory.DIRECTORY_PATH", cache_file)

    def test_exact_match(self, tmp_path):
        platforms = {
            "feishu": [
                {"id": "oc_ops", "name": "Ops", "type": "group"},
                {"id": "oc_general", "name": "General", "type": "group"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("feishu", "Ops") == "oc_ops"
            assert resolve_channel_name("feishu", "#Ops") == "oc_ops"

    def test_case_insensitive(self, tmp_path):
        platforms = {
            "feishu": [{"id": "oc_eng", "name": "Engineering", "type": "group"}]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("feishu", "engineering") == "oc_eng"
            assert resolve_channel_name("feishu", "ENGINEERING") == "oc_eng"

    def test_prefix_match_unambiguous(self, tmp_path):
        platforms = {
            "feishu": [
                {"id": "oc_backend", "name": "engineering-backend", "type": "group"},
                {"id": "oc_design", "name": "design-team", "type": "group"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("feishu", "engineering") == "oc_backend"

    def test_prefix_match_ambiguous_returns_none(self, tmp_path):
        platforms = {
            "feishu": [
                {"id": "oc_backend", "name": "eng-backend", "type": "group"},
                {"id": "oc_frontend", "name": "eng-frontend", "type": "group"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("feishu", "eng") is None

    def test_no_channels_returns_none(self, tmp_path):
        with self._setup(tmp_path, {}):
            assert resolve_channel_name("feishu", "someone") is None

    def test_no_match_returns_none(self, tmp_path):
        platforms = {
            "feishu": [{"id": "ou_123", "name": "John", "type": "dm"}]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("feishu", "nonexistent") is None

    def test_topic_name_resolves_to_composite_id(self, tmp_path):
        platforms = {
            "feishu": [{"id": "oc_chat:om_thread", "name": "Coaching Chat / thread om_thread", "type": "group"}]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("feishu", "Coaching Chat / thread om_thread") == "oc_chat:om_thread"

    def test_id_match_takes_precedence_over_name(self, tmp_path):
        """A raw channel ID resolves to itself, even when a different
        channel happens to be named the same string."""
        platforms = {
            "feishu": [
                {"id": "oc_exact", "name": "engineering", "type": "group"},
                {"id": "oc_named", "name": "oc_exact", "type": "group"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("feishu", "oc_exact") == "oc_exact"

    def test_display_label_with_type_suffix_resolves(self, tmp_path):
        platforms = {
            "feishu": [
                {"id": "ou_alice", "name": "Alice", "type": "dm"},
                {"id": "oc_dev", "name": "Dev Group", "type": "group"},
                {"id": "oc_chat:om_thread", "name": "Coaching Chat / thread om_thread", "type": "group"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("feishu", "Alice (dm)") == "ou_alice"
            assert resolve_channel_name("feishu", "Dev Group (group)") == "oc_dev"
            assert resolve_channel_name("feishu", "Coaching Chat / thread om_thread (group)") == "oc_chat:om_thread"

    def test_ignores_non_feishu_directory_entries(self, tmp_path):
        platforms = {
            "telegram": [{"id": "123", "name": "Alice", "type": "dm"}],
            "discord": [{"id": "456", "name": "ops", "type": "channel"}],
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("telegram", "Alice") is None
            assert resolve_channel_name("discord", "ops") is None


class TestBuildFromSessions:
    def _write_sessions(self, tmp_path, sessions_data):
        """Write sessions.json at the path _build_from_sessions expects."""
        sessions_path = tmp_path / "sessions" / "sessions.json"
        sessions_path.parent.mkdir(parents=True)
        sessions_path.write_text(json.dumps(sessions_data))

    def test_builds_from_sessions_json(self, tmp_path):
        self._write_sessions(tmp_path, {
            "session_1": {
                "origin": {
                    "platform": "feishu",
                    "chat_id": "ou_alice",
                    "chat_name": "Alice",
                },
                "chat_type": "dm",
            },
            "session_2": {
                "origin": {
                    "platform": "feishu",
                    "chat_id": "oc_ops",
                    "user_name": "Bob",
                },
                "chat_type": "group",
            },
            "session_3": {
                "origin": {
                    "platform": "discord",
                    "chat_id": "99999",
                },
            },
        })

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = _build_from_sessions("feishu")

        assert len(entries) == 2
        names = {e["name"] for e in entries}
        assert "Alice" in names
        assert "Bob" in names

    def test_missing_sessions_file(self, tmp_path):
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = _build_from_sessions("feishu")
        assert entries == []

    def test_deduplication_by_chat_id(self, tmp_path):
        self._write_sessions(tmp_path, {
            "s1": {"origin": {"platform": "feishu", "chat_id": "oc_same", "chat_name": "X"}},
            "s2": {"origin": {"platform": "feishu", "chat_id": "oc_same", "chat_name": "X"}},
        })

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = _build_from_sessions("feishu")

        assert len(entries) == 1

    def test_keeps_distinct_topics_with_same_chat_id(self, tmp_path):
        self._write_sessions(tmp_path, {
            "group_root": {
                "origin": {"platform": "feishu", "chat_id": "oc_chat", "chat_name": "Coaching Chat"},
                "chat_type": "group",
            },
            "topic_a": {
                "origin": {
                    "platform": "feishu",
                    "chat_id": "oc_chat",
                    "chat_name": "Coaching Chat",
                    "thread_id": "om_a",
                },
                "chat_type": "group",
            },
            "topic_b": {
                "origin": {
                    "platform": "feishu",
                    "chat_id": "oc_chat",
                    "chat_name": "Coaching Chat",
                    "thread_id": "om_b",
                },
                "chat_type": "group",
            },
        })

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = _build_from_sessions("feishu")

        ids = {entry["id"] for entry in entries}
        names = {entry["name"] for entry in entries}
        assert ids == {"oc_chat", "oc_chat:om_a", "oc_chat:om_b"}
        assert "Coaching Chat" in names
        assert "Coaching Chat / topic om_a" in names
        assert "Coaching Chat / topic om_b" in names


class TestFormatDirectoryForDisplay:
    def test_empty_directory(self, tmp_path):
        with patch("gateway.channel_directory.DIRECTORY_PATH", tmp_path / "nope.json"):
            result = format_directory_for_display()
        assert "No messaging platforms" in result

    def test_feishu_display(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "feishu": [
                {"id": "ou_alice", "name": "Alice", "type": "dm"},
                {"id": "oc_dev", "name": "Dev Group", "type": "group"},
                {"id": "oc_chat:om_thread", "name": "Coaching Chat / topic om_thread", "type": "group"},
            ]
        })
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            result = format_directory_for_display()

        assert "Feishu:" in result
        assert "feishu:Alice" in result
        assert "feishu:Dev Group" in result
        assert "feishu:Coaching Chat / topic om_thread" in result

    def test_display_filters_non_feishu_cache_entries(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "telegram": [{"id": "123", "name": "Alice", "type": "dm"}],
            "discord": [{"id": "1", "name": "general", "guild": "Server1", "type": "channel"}],
        })
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            result = format_directory_for_display()

        assert "No messaging platforms" in result
        assert "telegram:" not in result
        assert "discord:" not in result


class TestLookupChannelType:
    def _setup(self, tmp_path, platforms):
        cache_file = _write_directory(tmp_path, platforms)
        return patch("gateway.channel_directory.DIRECTORY_PATH", cache_file)

    def test_forum_channel(self, tmp_path):
        platforms = {
            "feishu": [
                {"id": "oc_100", "name": "Ideas", "type": "group"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert lookup_channel_type("feishu", "oc_100") == "group"

    def test_regular_channel(self, tmp_path):
        platforms = {
            "feishu": [
                {"id": "ou_200", "name": "Alice", "type": "dm"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert lookup_channel_type("feishu", "ou_200") == "dm"

    def test_unknown_chat_id_returns_none(self, tmp_path):
        platforms = {
            "feishu": [
                {"id": "oc_200", "name": "General", "type": "group"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert lookup_channel_type("feishu", "oc_missing") is None

    def test_unknown_platform_returns_none(self, tmp_path):
        with self._setup(tmp_path, {}):
            assert lookup_channel_type("discord", "100") is None

    def test_channel_without_type_key_returns_none(self, tmp_path):
        platforms = {
            "feishu": [
                {"id": "oc_300", "name": "General"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert lookup_channel_type("feishu", "oc_300") is None
