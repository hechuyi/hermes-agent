import dataclasses

import pytest

from gateway.feishu_legacy_guard import (
    FeishuBrokerContext,
    current_feishu_broker_context,
    feishu_broker_context,
    require_feishu_broker_context,
)


CONTRACT_HASH = "sha256:" + ("a" * 64)


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
        "grant-safe",
        action_id="action-safe",
        contract_hash=CONTRACT_HASH,
        route_partition_key="route-safe",
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
            grant_handle="grant-safe",
            action_id="action-safe",
            contract_hash=CONTRACT_HASH,
            route_partition_key="route-safe",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            context.grant_handle = "changed"


def test_context_exit_restores_deny_state():
    with feishu_broker_context(
        "grant-safe",
        action_id="action-safe",
        contract_hash=CONTRACT_HASH,
        route_partition_key="route-safe",
    ):
        assert require_feishu_broker_context("tool", "feishu_doc_read") == (True, "")

    assert current_feishu_broker_context() is None
    assert require_feishu_broker_context("tool", "feishu_doc_read") == (
        False,
        "feishu_legacy_tool_requires_broker",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("grant_handle", "raw-token-secret"),
        ("grant_handle", "open_id-ou_123"),
        ("grant_handle", "/tmp/raw/path"),
        ("action_id", "user_id-123"),
        ("contract_hash", "not-a-contract-hash"),
        ("route_partition_key", "file_path-home"),
        ("route_partition_key", "route with spaces"),
    ],
)
def test_context_creation_rejects_unsafe_values(field, value):
    values = {
        "grant_handle": "grant-safe",
        "action_id": "action-safe",
        "contract_hash": CONTRACT_HASH,
        "route_partition_key": "route-safe",
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
