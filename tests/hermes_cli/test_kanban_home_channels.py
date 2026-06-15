from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_home_channels


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


class _Platform:
    def __init__(self, value: str):
        self.value = value


def _home_config():
    return SimpleNamespace(
        platforms={
            _Platform("feishu"): SimpleNamespace(
                home_channel=SimpleNamespace(
                    chat_id="oc_feishu_home",
                    thread_id="om_feishu_thread",
                    name="Feishu Home",
                ),
            ),
            _Platform("telegram"): SimpleNamespace(
                home_channel=SimpleNamespace(
                    chat_id="123",
                    thread_id=None,
                    name=None,
                ),
            ),
            _Platform("slack"): SimpleNamespace(home_channel=None),
        },
    )


def test_configured_home_channels_returns_gateway_homes_in_stable_shape():
    homes = kanban_home_channels.configured_home_channels(config_loader=_home_config)

    assert homes == [
        {
            "platform": "feishu",
            "chat_id": "oc_feishu_home",
            "thread_id": "om_feishu_thread",
            "name": "Feishu Home",
        },
        {
            "platform": "telegram",
            "chat_id": "123",
            "thread_id": "",
            "name": "Home",
        },
    ]


def test_home_channels_payload_marks_subscribed_gateway_home(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="notify me")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="feishu",
            chat_id="oc_feishu_home",
            thread_id="om_feishu_thread",
        )

        payload = kanban_home_channels.home_channels_payload(
            conn,
            task_id=task_id,
            config_loader=_home_config,
        )

        flags = {item["platform"]: item["subscribed"] for item in payload["home_channels"]}
        assert flags == {"feishu": True, "telegram": False}
    finally:
        conn.close()


def test_subscribe_home_channel_writes_notify_sub_with_active_profile(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="feishu task")

        payload = kanban_home_channels.subscribe_home_channel(
            conn,
            task_id,
            "feishu",
            config_loader=_home_config,
            active_profile_loader=lambda: "ops",
        )

        assert payload["ok"] is True
        assert payload["task_id"] == task_id
        assert payload["home_channel"]["platform"] == "feishu"
        subs = kb.list_notify_subs(conn, task_id)
        assert len(subs) == 1
        assert subs[0]["platform"] == "feishu"
        assert subs[0]["chat_id"] == "oc_feishu_home"
        assert subs[0]["thread_id"] == "om_feishu_thread"
        assert subs[0]["notifier_profile"] == "ops"
    finally:
        conn.close()


def test_subscribe_home_channel_reports_missing_platform_and_task(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="task")

        with pytest.raises(kanban_home_channels.HomeChannelError) as missing_platform:
            kanban_home_channels.subscribe_home_channel(
                conn,
                task_id,
                "discord",
                config_loader=_home_config,
            )
        assert missing_platform.value.status_code == 404
        assert "discord" in missing_platform.value.detail

        with pytest.raises(kanban_home_channels.HomeChannelError) as missing_task:
            kanban_home_channels.subscribe_home_channel(
                conn,
                "t_missing",
                "feishu",
                config_loader=_home_config,
            )
        assert missing_task.value.status_code == 404
        assert "t_missing" in missing_task.value.detail
    finally:
        conn.close()


def test_unsubscribe_home_channel_removes_only_matching_home(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="notify me")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="feishu",
            chat_id="oc_feishu_home",
            thread_id="om_feishu_thread",
        )
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="123",
        )

        payload = kanban_home_channels.unsubscribe_home_channel(
            conn,
            task_id,
            "feishu",
            config_loader=_home_config,
        )

        assert payload["ok"] is True
        subs = {sub["platform"]: sub for sub in kb.list_notify_subs(conn, task_id)}
        assert set(subs) == {"telegram"}
    finally:
        conn.close()
