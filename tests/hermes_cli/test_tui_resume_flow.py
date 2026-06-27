import pytest


def test_oneshot_fails_closed_on_agent_exception(monkeypatch, capsys):
    import hermes_cli.oneshot as oneshot_mod

    def _boom(*_args, **_kwargs):
        raise OSError("not a TTY")

    monkeypatch.setattr(oneshot_mod, "_run_agent", _boom)

    assert oneshot_mod.run_oneshot("hello") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "agent failed" in captured.err
    assert "not a TTY" in captured.err


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_oneshot_reraises_control_flow_exceptions(monkeypatch, exc):
    import hermes_cli.oneshot as oneshot_mod

    def _raise(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(oneshot_mod, "_run_agent", _raise)

    with pytest.raises(exc):
        oneshot_mod.run_oneshot("hello")


def test_oneshot_fails_closed_on_empty_final_response(monkeypatch, capsys):
    import hermes_cli.oneshot as oneshot_mod

    monkeypatch.setattr(oneshot_mod, "_run_agent", lambda *_args, **_kwargs: " \n\t")

    assert oneshot_mod.run_oneshot("hello") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no final response was produced" in captured.err
