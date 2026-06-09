from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import gateway_event_ledger
from gateway.config import PlatformConfig
from gateway.feishu_legacy_guard import current_feishu_broker_context
from gateway.gateway_event_ledger import LEDGER_FILENAME
from gateway.platforms.feishu import FeishuAdapter


def _sha(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _action_id(seed: str) -> str:
    return "broker_action:" + _sha(f"action:{seed}")


def _grant_handle(seed: str) -> str:
    return "broker_grant_handle:" + _sha(f"grant:{seed}")


def _idempotency(seed: str) -> str:
    return "broker_idempotency:" + _sha(f"idempotency:{seed}")


def _adapter(tmp_path, *, action_seed: str = "alpha") -> FeishuAdapter:
    adapter = FeishuAdapter(
        PlatformConfig(
            extra={
                "app_id": "app_broker_cards",
                "tenant_partition_key": "tenant:broker",
                "app_partition_key": "app:broker",
                "gateway_event_state_dir": str(tmp_path),
            }
        )
    )
    adapter.handle_message = AsyncMock()
    adapter._handle_message_with_guards = AsyncMock()
    adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace())))
    adapter._feishu_broker_action_id_factory = lambda _kind: _action_id(action_seed)
    adapter._feishu_broker_grant_factory = lambda _action_id_value: _grant_handle(action_seed)
    return adapter


def _binding(seed: str = "alpha", **overrides) -> dict[str, object]:
    values: dict[str, object] = {
        "route_partition_hash": _sha(f"route:{seed}"),
        "route_snapshot_hash": _sha(f"route-snapshot:{seed}"),
        "operator_hash": _sha(f"operator:{seed}"),
        "contract_hash": _sha(f"contract:{seed}"),
        "expires_at": 1_800_000_000,
        "idempotency_key": _idempotency(seed),
        "payload": {"payload_hash": _sha(f"payload:{seed}")},
    }
    values.update(overrides)
    return values


def _create(
    adapter: FeishuAdapter,
    *,
    kind: str = "clarification",
    seed: str = "alpha",
    **overrides,
) -> dict[str, object]:
    kwargs = _binding(seed, **overrides)
    if kind == "clarification":
        return adapter.create_brokered_clarification_card(**kwargs)
    if kind == "confirmation":
        return adapter.create_brokered_confirmation_card(**kwargs)
    raise AssertionError(kind)


def _state(tmp_path) -> dict:
    with (tmp_path / LEDGER_FILENAME).open(encoding="utf-8") as handle:
        return json.load(handle)


def _broker_events(tmp_path, event_type: str | None = None) -> list[dict]:
    events = _state(tmp_path).get("feishu_broker_action_lifecycle", [])
    if event_type is None:
        return events
    return [event for event in events if event["type"] == event_type]


def _assert_no_raw_material(tmp_path) -> None:
    rendered = (tmp_path / LEDGER_FILENAME).read_text(encoding="utf-8")
    for raw in (
        "oc_raw_chat",
        "ou_raw_operator",
        "om_raw_message",
        "/tmp/raw",
        "sdk_request",
        "_feishu_broker_grant",
        "raw message body",
    ):
        assert raw not in rendered


def _callback(result: dict[str, object], seed: str = "alpha", **overrides) -> dict[str, object]:
    value = {
        "action_id": result["action_id"],
        "payload_hash": _sha(f"payload:{seed}"),
        "choice_hash": _sha(f"choice:{seed}"),
    }
    value.update(overrides)
    return value


def _resolve_kwargs(seed: str = "alpha", **overrides) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "route_partition_hash": _sha(f"route:{seed}"),
        "route_snapshot_hash": _sha(f"route-snapshot:{seed}"),
        "operator_hash": _sha(f"operator:{seed}"),
        "contract_hash": _sha(f"contract:{seed}"),
        "now": 1_700_000_000,
    }
    kwargs.update(overrides)
    return kwargs


def _record(tmp_path, action_id: str) -> dict[str, object]:
    record = _state(tmp_path)["feishu_broker_actions"][action_id]
    assert isinstance(record, dict)
    return record


def _accept_event(
    adapter: FeishuAdapter,
    record: dict[str, object],
    seed: str = "alpha",
) -> dict[str, object]:
    return adapter._broker_action_resolution_event(
        "feishu_broker_action_accepted",
        record=record,
        callback_hash=_sha(f"callback:{seed}"),
        choice_hash=_sha(f"choice:{seed}"),
    )


@pytest.mark.parametrize("kind", ["clarification", "confirmation"])
def test_creating_brokered_card_persists_opaque_binding_and_exact_payload_hash(
    tmp_path,
    kind,
):
    adapter = _adapter(tmp_path)

    result = _create(adapter, kind=kind)

    assert result["ok"] is True
    assert result["action_id"] == _action_id("alpha")
    assert result["grant_handle"] == _grant_handle("alpha")
    assert result["payload_hash"] == _sha("payload:alpha")
    assert result["card"]["value"] == {
        "action_id": _action_id("alpha"),
        "payload_hash": _sha("payload:alpha"),
    }
    assert "_feishu_broker_grant" not in json.dumps(result["card"], sort_keys=True)

    created = _broker_events(tmp_path, "feishu_broker_action_created")
    assert len(created) == 1
    record = created[0]
    assert record["action_id"] == _action_id("alpha")
    assert record["grant_handle"] == _grant_handle("alpha")
    assert record["action_kind"] == kind
    assert record["route_partition_hash"] == _sha("route:alpha")
    assert record["route_snapshot_hash"] == _sha("route-snapshot:alpha")
    assert record["operator_hash"] == _sha("operator:alpha")
    assert record["contract_hash"] == _sha("contract:alpha")
    assert record["payload_hash"] == _sha("payload:alpha")
    assert record["expires_at"] == 1_800_000_000
    assert record["idempotency_key_hash"] == _sha(_idempotency("alpha"))
    _assert_no_raw_material(tmp_path)


@pytest.mark.asyncio
async def test_successful_callback_records_accepted_and_resolved_once_with_broker_context(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    created = _create(adapter, kind="confirmation")
    side_effects: list[object] = []

    async def on_resolved(record):
        context = current_feishu_broker_context()
        assert context is not None
        assert context.action_id == _action_id("alpha")
        assert context.grant_handle == _grant_handle("alpha")
        assert context.contract_hash == _sha("contract:alpha")
        assert context.route_partition_key == "route_snapshot:" + _sha("route:alpha")
        side_effects.append(record)

    result = await adapter.resolve_brokered_card_callback(
        action_value=_callback(created),
        route_partition_hash=_sha("route:alpha"),
        route_snapshot_hash=_sha("route-snapshot:alpha"),
        operator_hash=_sha("operator:alpha"),
        contract_hash=_sha("contract:alpha"),
        now=1_700_000_000,
        on_resolved=on_resolved,
    )

    assert result == {
        "ok": True,
        "action_id": _action_id("alpha"),
        "action_kind": "confirmation",
        "payload_hash": _sha("payload:alpha"),
    }
    assert len(side_effects) == 1
    assert [event["type"] for event in _broker_events(tmp_path)] == [
        "feishu_broker_action_created",
        "feishu_broker_action_accepted",
        "feishu_broker_action_resolved",
    ]
    assert len(_broker_events(tmp_path, "feishu_broker_action_accepted")) == 1
    assert len(_broker_events(tmp_path, "feishu_broker_action_resolved")) == 1
    assert current_feishu_broker_context() is None
    _assert_no_raw_material(tmp_path)


@pytest.mark.parametrize(
    ("label", "callback_overrides", "resolve_overrides", "expected_failure"),
    [
        (
            "wrong_operator",
            {},
            {"operator_hash": _sha("operator:other")},
            "feishu_broker_action_operator_mismatch",
        ),
        (
            "wrong_route",
            {},
            {"route_partition_hash": _sha("route:other")},
            "feishu_broker_action_route_mismatch",
        ),
        (
            "expired",
            {},
            {"now": 1_900_000_000},
            "feishu_broker_action_expired",
        ),
        (
            "payload_mismatch",
            {"payload_hash": _sha("payload:other")},
            {},
            "feishu_broker_action_payload_mismatch",
        ),
        (
            "unknown_id",
            {"action_id": _action_id("unknown")},
            {},
            "feishu_broker_action_unknown",
        ),
    ],
)
@pytest.mark.asyncio
async def test_callback_denials_fail_before_side_effects_and_preserve_binding(
    tmp_path,
    label,
    callback_overrides,
    resolve_overrides,
    expected_failure,
):
    del label
    adapter = _adapter(tmp_path)
    created = _create(adapter)
    before_created = list(_broker_events(tmp_path, "feishu_broker_action_created"))
    side_effect = AsyncMock()
    kwargs = {
        "route_partition_hash": _sha("route:alpha"),
        "route_snapshot_hash": _sha("route-snapshot:alpha"),
        "operator_hash": _sha("operator:alpha"),
        "contract_hash": _sha("contract:alpha"),
        "now": 1_700_000_000,
        "on_resolved": side_effect,
    }
    kwargs.update(resolve_overrides)

    result = await adapter.resolve_brokered_card_callback(
        action_value=_callback(created, **callback_overrides),
        **kwargs,
    )

    assert result["ok"] is False
    assert result["failure_class"] == expected_failure
    assert side_effect.await_count == 0
    assert adapter.handle_message.await_count == 0
    assert adapter._handle_message_with_guards.await_count == 0
    assert _broker_events(tmp_path, "feishu_broker_action_created") == before_created
    assert _broker_events(tmp_path, "feishu_broker_action_accepted") == []
    assert _broker_events(tmp_path, "feishu_broker_action_resolved") == []
    denied = _broker_events(tmp_path, "feishu_broker_action_denied")
    assert len(denied) == 1
    assert denied[0]["failure_class"] == expected_failure
    _assert_no_raw_material(tmp_path)


@pytest.mark.parametrize(
    "action_value",
    [
        {
            "action_id": _action_id("model"),
            "_feishu_broker_grant": _grant_handle("model"),
            "payload_hash": _sha("payload:alpha"),
        },
        {
            "action_id": _action_id("model"),
            "payload_hash": _sha("payload:alpha"),
            "raw_payload_path": "/tmp/raw/secret.txt",
        },
        {
            "action_id": _action_id("model"),
            "payload_hash": _sha("payload:alpha"),
            "sdk_request": {"path": "/open-apis/im/v1/messages", "body": "raw message body"},
        },
        {
            "hermes_action": "approve",
            "approval_id": 1,
            "payload_hash": _sha("payload:alpha"),
        },
    ],
)
@pytest.mark.asyncio
async def test_user_or_model_supplied_handles_and_raw_descriptors_do_not_authorize(
    tmp_path,
    action_value,
):
    adapter = _adapter(tmp_path)
    _create(adapter)
    side_effect = AsyncMock()

    result = await adapter.resolve_brokered_card_callback(
        action_value=action_value,
        route_partition_hash=_sha("route:alpha"),
        route_snapshot_hash=_sha("route-snapshot:alpha"),
        operator_hash=_sha("operator:alpha"),
        contract_hash=_sha("contract:alpha"),
        now=1_700_000_000,
        on_resolved=side_effect,
    )

    assert result["ok"] is False
    assert result["failure_class"] in {
        "feishu_broker_action_callback_material_invalid",
        "feishu_broker_action_unknown",
    }
    assert side_effect.await_count == 0
    assert _broker_events(tmp_path, "feishu_broker_action_accepted") == []
    assert _broker_events(tmp_path, "feishu_broker_action_resolved") == []
    assert len(_broker_events(tmp_path, "feishu_broker_action_denied")) == 1
    _assert_no_raw_material(tmp_path)


@pytest.mark.asyncio
async def test_successful_callback_replay_records_replayed_once_without_dispatch_or_delivery_mutation(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    created = _create(adapter)
    side_effect = AsyncMock()

    first = await adapter.resolve_brokered_card_callback(
        action_value=_callback(created),
        route_partition_hash=_sha("route:alpha"),
        route_snapshot_hash=_sha("route-snapshot:alpha"),
        operator_hash=_sha("operator:alpha"),
        contract_hash=_sha("contract:alpha"),
        now=1_700_000_000,
        on_resolved=side_effect,
    )
    replay = await adapter.resolve_brokered_card_callback(
        action_value=_callback(created),
        route_partition_hash=_sha("route:alpha"),
        route_snapshot_hash=_sha("route-snapshot:alpha"),
        operator_hash=_sha("operator:alpha"),
        contract_hash=_sha("contract:alpha"),
        now=1_700_000_001,
        on_resolved=side_effect,
    )
    replay_again = await adapter.resolve_brokered_card_callback(
        action_value=_callback(created),
        route_partition_hash=_sha("route:alpha"),
        route_snapshot_hash=_sha("route-snapshot:alpha"),
        operator_hash=_sha("operator:alpha"),
        contract_hash=_sha("contract:alpha"),
        now=1_700_000_002,
        on_resolved=side_effect,
    )

    assert first["ok"] is True
    assert replay == {
        "ok": False,
        "failure_class": "feishu_broker_action_duplicate",
        "replayed": True,
    }
    assert replay_again == replay
    assert side_effect.await_count == 1
    assert adapter.handle_message.await_count == 0
    assert adapter._handle_message_with_guards.await_count == 0
    assert _state(tmp_path).get("deliveries", {}) == {}
    assert _state(tmp_path).get("feishu_delivery_lifecycle", []) == []
    assert len(_broker_events(tmp_path, "feishu_broker_action_accepted")) == 1
    assert len(_broker_events(tmp_path, "feishu_broker_action_resolved")) == 1
    assert len(_broker_events(tmp_path, "feishu_broker_action_replayed")) == 1
    _assert_no_raw_material(tmp_path)


@pytest.mark.asyncio
async def test_in_flight_accepted_callback_replay_fails_before_side_effects_and_lifecycle_duplication(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    created = _create(adapter)
    record = _record(tmp_path, str(created["action_id"]))
    accepted = gateway_event_ledger.apply_gateway_event(
        _accept_event(adapter, record),
        tmp_path,
    )
    assert accepted.ok is True
    side_effect = AsyncMock()

    result = await adapter.resolve_brokered_card_callback(
        action_value=_callback(created),
        **_resolve_kwargs(on_resolved=side_effect),
    )

    assert result == {
        "ok": False,
        "failure_class": "feishu_broker_action_duplicate",
        "replayed": True,
    }
    assert side_effect.await_count == 0
    assert adapter.handle_message.await_count == 0
    assert adapter._handle_message_with_guards.await_count == 0
    assert len(_broker_events(tmp_path, "feishu_broker_action_accepted")) == 1
    assert _broker_events(tmp_path, "feishu_broker_action_resolved") == []
    assert len(_broker_events(tmp_path, "feishu_broker_action_replayed")) == 1
    _assert_no_raw_material(tmp_path)


@pytest.mark.asyncio
async def test_concurrent_callbacks_for_same_action_execute_side_effect_at_most_once(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    created = _create(adapter)
    entered = asyncio.Event()
    release = asyncio.Event()
    side_effect_calls = 0

    async def on_resolved(_record):
        nonlocal side_effect_calls
        side_effect_calls += 1
        entered.set()
        await release.wait()

    first = asyncio.create_task(
        adapter.resolve_brokered_card_callback(
            action_value=_callback(created),
            **_resolve_kwargs(on_resolved=on_resolved),
        )
    )
    await entered.wait()
    replay_task = asyncio.create_task(
        adapter.resolve_brokered_card_callback(
            action_value=_callback(created),
            **_resolve_kwargs(now=1_700_000_001, on_resolved=on_resolved),
        )
    )
    for _ in range(100):
        if replay_task.done() or side_effect_calls > 1:
            break
        await asyncio.sleep(0.01)
    release.set()
    first_result, replay = await asyncio.gather(first, replay_task)

    assert first_result["ok"] is True
    assert replay == {
        "ok": False,
        "failure_class": "feishu_broker_action_duplicate",
        "replayed": True,
    }
    assert side_effect_calls == 1
    assert adapter.handle_message.await_count == 0
    assert adapter._handle_message_with_guards.await_count == 0
    assert len(_broker_events(tmp_path, "feishu_broker_action_accepted")) == 1
    assert len(_broker_events(tmp_path, "feishu_broker_action_resolved")) == 1
    assert len(_broker_events(tmp_path, "feishu_broker_action_replayed")) == 1
    _assert_no_raw_material(tmp_path)


@pytest.mark.asyncio
async def test_resolved_write_failure_retry_fails_closed_without_second_side_effect(
    tmp_path,
    monkeypatch,
):
    adapter = _adapter(tmp_path)
    created = _create(adapter)
    side_effect = AsyncMock()
    original_apply = gateway_event_ledger.apply_gateway_event_async

    async def fail_resolved_once(event, state_dir):
        if event.get("type") == "feishu_broker_action_resolved":
            monkeypatch.setattr(
                gateway_event_ledger,
                "apply_gateway_event_async",
                original_apply,
            )
            return SimpleNamespace(ok=False, failure_class="gateway_event_state_io_failed")
        return await original_apply(event, state_dir)

    monkeypatch.setattr(
        gateway_event_ledger,
        "apply_gateway_event_async",
        fail_resolved_once,
    )

    failed = await adapter.resolve_brokered_card_callback(
        action_value=_callback(created),
        **_resolve_kwargs(on_resolved=side_effect),
    )
    retry = await adapter.resolve_brokered_card_callback(
        action_value=_callback(created),
        **_resolve_kwargs(now=1_700_000_001, on_resolved=side_effect),
    )

    assert failed == {
        "ok": False,
        "failure_class": "gateway_event_state_io_failed",
    }
    assert retry == {
        "ok": False,
        "failure_class": "feishu_broker_action_duplicate",
        "replayed": True,
    }
    assert side_effect.await_count == 1
    assert len(_broker_events(tmp_path, "feishu_broker_action_accepted")) == 1
    assert _broker_events(tmp_path, "feishu_broker_action_resolved") == []
    assert len(_broker_events(tmp_path, "feishu_broker_action_replayed")) == 1
    _assert_no_raw_material(tmp_path)


@pytest.mark.asyncio
async def test_denial_audit_write_failure_fails_closed_without_side_effects(
    tmp_path,
    monkeypatch,
):
    adapter = _adapter(tmp_path)
    _create(adapter)
    side_effect = AsyncMock()

    async def fail_denied(event, state_dir):
        if event.get("type") == "feishu_broker_action_denied":
            return SimpleNamespace(ok=False, failure_class="gateway_event_state_io_failed")
        return await original_apply(event, state_dir)

    from gateway import gateway_event_ledger

    original_apply = gateway_event_ledger.apply_gateway_event_async
    monkeypatch.setattr(gateway_event_ledger, "apply_gateway_event_async", fail_denied)

    result = await adapter.resolve_brokered_card_callback(
        action_value={"action_id": _action_id("unknown"), "payload_hash": _sha("payload:alpha")},
        route_partition_hash=_sha("route:alpha"),
        route_snapshot_hash=_sha("route-snapshot:alpha"),
        operator_hash=_sha("operator:alpha"),
        contract_hash=_sha("contract:alpha"),
        now=1_700_000_000,
        on_resolved=side_effect,
    )

    assert result == {
        "ok": False,
        "failure_class": "feishu_broker_action_denial_audit_failed",
    }
    assert side_effect.await_count == 0
    assert _broker_events(tmp_path, "feishu_broker_action_denied") == []
