"""Regression coverage for corrupted entries in the sessions index."""

import json
import logging
import threading

from gateway.session import SessionStore


class _FakeConfig:
    session_idle_ttl = 0
    session_daily_ttl = 0
    group_sessions_per_user = True
    thread_sessions_per_user = False
    multiplex_profiles = False

    def get_reset_policy(self, *args, **kwargs):
        return None


def _make_store(tmp_path, sessions_data):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "sessions.json").write_text(
        json.dumps(sessions_data),
        encoding="utf-8",
    )

    store = SessionStore.__new__(SessionStore)
    store.sessions_dir = sessions_dir
    store.config = _FakeConfig()
    store._entries = {}
    store._loaded = False
    store._lock = threading.RLock()
    store._has_active_processes_fn = None
    store._db = None
    return store


def _valid_entry(session_key="agent:main:telegram:dm:123456", session_id="sess-ok"):
    return {
        "session_key": session_key,
        "session_id": session_id,
        "created_at": "2026-01-01T12:00:00",
        "updated_at": "2026-01-01T12:30:00",
        "origin": {
            "platform": "telegram",
            "chat_id": "123456",
            "chat_type": "dm",
        },
    }


def test_session_index_skips_non_dict_entries_and_keeps_valid_entry(tmp_path, caplog):
    sessions_data = {
        "bad_bool": True,
        "bad_string": "not a session entry",
        "bad_list": ["not", "a", "session", "entry"],
        "bad_null": None,
        "valid_key": _valid_entry(),
    }
    store = _make_store(tmp_path, sessions_data)

    with caplog.at_level(logging.WARNING, logger="gateway.session"):
        store._ensure_loaded()

    assert set(store._entries) == {"valid_key"}
    assert store._entries["valid_key"].session_id == "sess-ok"

    warning_text = caplog.text
    assert warning_text.count("session_index_entry_invalid_type") == 4
    for actual_type in ("bool", "str", "list", "NoneType"):
        assert f"actual_type={actual_type}" in warning_text
    assert "stage=load_sessions_index" in warning_text
    assert "action=skip_session_entry" in warning_text
    assert "entry_key_hash=" in warning_text
    assert str(tmp_path) not in warning_text
    for raw_key in ("bad_bool", "bad_string", "bad_list", "bad_null"):
        assert raw_key not in warning_text


def test_session_index_ignores_non_dict_origin_without_dropping_entry(tmp_path):
    entry = _valid_entry(session_key="agent:main:telegram:dm:bad-origin")
    entry["origin"] = True
    store = _make_store(tmp_path, {"key_with_bad_origin": entry})

    store._ensure_loaded()

    assert set(store._entries) == {"key_with_bad_origin"}
    assert store._entries["key_with_bad_origin"].origin is None
