def test_configured_platforms_detects_matrix_runtime_homeserver_env(monkeypatch):
    """Matrix dump detection follows the runtime env contract."""
    from hermes_cli import dump

    monkeypatch.delenv("MATRIX_HOMESERVER_URL", raising=False)
    monkeypatch.setenv("MATRIX_HOMESERVER", "https://matrix.example.org")

    assert "matrix" in dump._configured_platforms()


def test_configured_platforms_does_not_use_legacy_matrix_homeserver_url(monkeypatch):
    """The dump command should not report Matrix from a non-runtime env name."""
    from hermes_cli import dump

    monkeypatch.delenv("MATRIX_HOMESERVER", raising=False)
    monkeypatch.setenv("MATRIX_HOMESERVER_URL", "https://matrix.example.org")

    assert "matrix" not in dump._configured_platforms()
