"""Tests for xAI OAuth auth-error recovery in auxiliary_client."""

from __future__ import annotations

from unittest.mock import patch


class TestIsAuthErrorXaiOauth403:
    def test_xai_403_bad_credentials_is_auth_error(self):
        from agent.auxiliary_client import _is_auth_error

        exc = Exception(
            "Error code: 403 - The OAuth2 access token could not be validated. "
            "[WKE=unauthenticated:bad-credentials]"
        )
        exc.status_code = 403

        assert _is_auth_error(exc) is True

    def test_xai_bad_credentials_without_status_code_is_auth_error(self):
        from agent.auxiliary_client import _is_auth_error

        exc = Exception("Error code: 403 - unauthenticated:bad-credentials")

        assert _is_auth_error(exc) is True

    def test_generic_403_is_not_auth_error(self):
        from agent.auxiliary_client import _is_auth_error

        exc = Exception("Error code: 403 - permission denied")
        exc.status_code = 403

        assert _is_auth_error(exc) is False


class TestRecoverablePoolProviderXaiOauth:
    def test_explicit_xai_oauth_provider_passes_through(self):
        from agent.auxiliary_client import _recoverable_pool_provider

        assert _recoverable_pool_provider("xai-oauth", None) == "xai-oauth"

    def test_api_x_ai_base_url_maps_to_xai_oauth(self):
        from agent.auxiliary_client import _recoverable_pool_provider

        class Client:
            base_url = "https://api.x.ai/v1"

        assert _recoverable_pool_provider("auto", Client()) == "xai-oauth"


class TestRefreshProviderCredentialsXaiOauth:
    def test_refresh_uses_pool_entry_and_evicts_cache(self):
        from agent.auxiliary_client import _refresh_provider_credentials

        class Credential:
            runtime_api_key = "fresh-xai-token"

        class Pool:
            selected = False
            refreshed = False

            def has_credentials(self):
                return True

            def select(self):
                self.selected = True

            def try_refresh_current(self):
                self.refreshed = True
                return Credential()

        pool = Pool()
        with (
            patch("agent.auxiliary_client.load_pool", return_value=pool),
            patch("agent.auxiliary_client._evict_cached_clients") as evict,
        ):
            assert _refresh_provider_credentials("xai-oauth") is True

        assert pool.selected is True
        assert pool.refreshed is True
        evict.assert_called_once_with("xai-oauth")

    def test_refresh_falls_back_to_singleton_resolver(self):
        from agent.auxiliary_client import _refresh_provider_credentials

        with (
            patch("agent.auxiliary_client.load_pool", return_value=None),
            patch(
                "hermes_cli.auth.resolve_xai_oauth_runtime_credentials",
                return_value={"api_key": "fresh-singleton-token"},
            ),
            patch("agent.auxiliary_client._evict_cached_clients") as evict,
        ):
            assert _refresh_provider_credentials("xai-oauth") is True

        evict.assert_called_once_with("xai-oauth")

    def test_refresh_returns_false_without_pool_or_singleton_credentials(self):
        from agent.auxiliary_client import _refresh_provider_credentials

        with (
            patch("agent.auxiliary_client.load_pool", return_value=None),
            patch(
                "hermes_cli.auth.resolve_xai_oauth_runtime_credentials",
                return_value={"api_key": ""},
            ),
            patch("agent.auxiliary_client._evict_cached_clients") as evict,
        ):
            assert _refresh_provider_credentials("xai-oauth") is False

        evict.assert_not_called()
