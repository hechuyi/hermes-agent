"""Characterization tests for the fork's gateway-level access boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.session import Platform, SessionSource


@pytest.fixture(autouse=True)
def _clear_access_env(monkeypatch):
    for key in (
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
        "FEISHU_ALLOWED_USERS",
        "FEISHU_ALLOW_ALL_USERS",
        "WEIXIN_ALLOWED_USERS",
        "WEIXIN_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _runner_with_adapter(platform: Platform, *, adapter_enforces: bool):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {
        platform: SimpleNamespace(enforces_own_access_policy=adapter_enforces)
    }
    runner.pairing_store = SimpleNamespace(
        is_approved=lambda *_args, **_kwargs: False
    )
    return runner


def _source(platform: Platform, user_id: str = "ou_stranger") -> SessionSource:
    return SessionSource(
        platform=platform,
        chat_id="chat-1",
        chat_type="dm",
        user_id=user_id,
        user_name="Stranger",
    )


def test_adapter_owned_policy_flag_does_not_bypass_gateway_default_deny():
    """Current fork contract: gateway authorization remains the final guard.

    Upstream later teaches the gateway to trust adapter-owned access-policy
    flags. This fork has not adopted that boundary change; with no allowlist,
    no pairing approval, and no allow-all flag, an adapter attribute alone must
    not authorize the sender.
    """
    runner = _runner_with_adapter(Platform.WEIXIN, adapter_enforces=True)

    assert runner._is_user_authorized(_source(Platform.WEIXIN)) is False


def test_feishu_stays_fail_closed_even_if_adapter_claimed_own_policy():
    """Feishu authorization must remain gateway/fork controlled.

    This guards against accidentally importing a generic adapter-trust helper
    that would authorize live Feishu traffic merely because an adapter object
    advertises local intake policy.
    """
    runner = _runner_with_adapter(Platform.FEISHU, adapter_enforces=True)

    assert runner._is_user_authorized(_source(Platform.FEISHU)) is False


def test_gateway_allow_all_is_still_the_explicit_escape_hatch(monkeypatch):
    """The characterization above is about implicit adapter trust only."""
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    runner = _runner_with_adapter(Platform.FEISHU, adapter_enforces=False)

    assert runner._is_user_authorized(_source(Platform.FEISHU)) is True
