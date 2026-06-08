import dataclasses

import pytest

from gateway.feishu_legacy_guard import (
    FeishuBrokerContext,
    current_feishu_broker_context,
    feishu_broker_context,
    require_feishu_broker_context,
)


CONTRACT_HASH = "sha256:" + ("a" * 64)
GRANT_HANDLE = "broker_grant_handle:sha256:" + ("b" * 64)
ACTION_ID = "broker_action:sha256:" + ("c" * 64)
ROUTE_PARTITION_KEY = "route_snapshot:sha256:" + ("d" * 64)


def test_no_context_denies_with_stable_reason():
    allowed, reason = require_feishu_broker_context(
        "tool",
        "feishu_doc_read",
        args={"document_id_hash": "sha256:" + ("b" * 64)},
    )

    assert allowed is False
    assert reason == "feishu_legacy_tool_requires_broker"
    assert current_feishu_broker_context() is None


def test_json_args_broker_grant_cannot_create_context():
    allowed, reason = require_feishu_broker_context(
        "tool",
        "feishu_doc_read",
        args={
            "_feishu_broker_grant": {
                "grant_handle": "grant-safe",
                "action_id": "action-safe",
                "contract_hash": CONTRACT_HASH,
                "route_partition_key": "route-safe",
            }
        },
    )

    assert allowed is False
    assert reason == "feishu_legacy_tool_requires_broker"
    assert current_feishu_broker_context() is None


def test_contextvar_created_broker_context_allows_legacy_surface():
    with feishu_broker_context(
        GRANT_HANDLE,
        action_id=ACTION_ID,
        contract_hash=CONTRACT_HASH,
        route_partition_key=ROUTE_PARTITION_KEY,
    ) as context:
        allowed, reason = require_feishu_broker_context(
            "tool",
            "feishu_doc_read",
            args={"_feishu_broker_grant": "model-supplied-noop"},
        )

        assert allowed is True
        assert reason == ""
        assert current_feishu_broker_context() == context
        assert context == FeishuBrokerContext(
            grant_handle=GRANT_HANDLE,
            action_id=ACTION_ID,
            contract_hash=CONTRACT_HASH,
            route_partition_key=ROUTE_PARTITION_KEY,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            context.grant_handle = "changed"


def test_context_exit_restores_deny_state():
    with feishu_broker_context(
        GRANT_HANDLE,
        action_id=ACTION_ID,
        contract_hash=CONTRACT_HASH,
        route_partition_key=ROUTE_PARTITION_KEY,
    ):
        assert require_feishu_broker_context("tool", "feishu_doc_read") == (True, "")

    assert current_feishu_broker_context() is None
    assert require_feishu_broker_context("tool", "feishu_doc_read") == (
        False,
        "feishu_legacy_tool_requires_broker",
    )


def test_nested_context_restores_outer_then_empty_state():
    outer_grant = "broker_grant_handle:sha256:" + ("1" * 64)
    outer_action = "broker_action:sha256:" + ("2" * 64)
    outer_route = "route_snapshot:sha256:" + ("3" * 64)
    inner_grant = "broker_grant_handle:sha256:" + ("4" * 64)
    inner_action = "broker_action:sha256:" + ("5" * 64)
    inner_route = "route_snapshot:sha256:" + ("6" * 64)

    with feishu_broker_context(
        outer_grant,
        action_id=outer_action,
        contract_hash=CONTRACT_HASH,
        route_partition_key=outer_route,
    ) as outer_context:
        assert current_feishu_broker_context() == outer_context

        with feishu_broker_context(
            inner_grant,
            action_id=inner_action,
            contract_hash=CONTRACT_HASH,
            route_partition_key=inner_route,
        ) as inner_context:
            assert current_feishu_broker_context() == inner_context

        assert current_feishu_broker_context() == outer_context

    assert current_feishu_broker_context() is None


def test_context_restore_after_exception():
    with pytest.raises(RuntimeError, match="broker failure"):
        with feishu_broker_context(
            GRANT_HANDLE,
            action_id=ACTION_ID,
            contract_hash=CONTRACT_HASH,
            route_partition_key=ROUTE_PARTITION_KEY,
        ):
            assert require_feishu_broker_context("tool", "feishu_doc_read") == (
                True,
                "",
            )
            raise RuntimeError("broker failure")

    assert current_feishu_broker_context() is None
    assert require_feishu_broker_context("tool", "feishu_doc_read") == (
        False,
        "feishu_legacy_tool_requires_broker",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("grant_handle", "grant-safe"),
        ("grant_handle", "raw-token-secret"),
        ("grant_handle", "open_id-ou_123"),
        ("grant_handle", "/tmp/raw/path"),
        ("action_id", "action-safe"),
        ("action_id", "user_id-123"),
        ("contract_hash", "not-a-contract-hash"),
        ("route_partition_key", "route-safe"),
        ("route_partition_key", "file_path-home"),
        ("route_partition_key", "route with spaces"),
    ],
)
def test_context_creation_rejects_unsafe_values(field, value):
    values = {
        "grant_handle": GRANT_HANDLE,
        "action_id": ACTION_ID,
        "contract_hash": CONTRACT_HASH,
        "route_partition_key": ROUTE_PARTITION_KEY,
    }
    values[field] = value

    with pytest.raises(ValueError, match=field):
        with feishu_broker_context(
            values["grant_handle"],
            action_id=values["action_id"],
            contract_hash=values["contract_hash"],
            route_partition_key=values["route_partition_key"],
        ):
            pass

    assert current_feishu_broker_context() is None
