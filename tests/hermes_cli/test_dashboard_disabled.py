"""Dashboard command behavior in the Feishu runtime fork."""

from __future__ import annotations

import argparse

import pytest

from hermes_cli.main import cmd_dashboard


def _ns(**kw):
    defaults = dict(
        port=9119,
        host="127.0.0.1",
        no_open=False,
        insecure=False,
        tui=False,
        stop=False,
        status=False,
        skip_build=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


@pytest.mark.parametrize(
    "args",
    [
        _ns(status=True),
        _ns(stop=True),
    ],
)
def test_dashboard_command_is_disabled(args, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_dashboard(args)

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "dashboard is not available in this Feishu runtime fork" in err


def test_dashboard_command_does_not_import_web_runtime(monkeypatch, capsys):
    def fail_import(name, *args, **kwargs):
        if name in {"fastapi", "uvicorn", "hermes_cli.web_server"}:
            raise AssertionError(f"dashboard attempted to import {name}")
        return real_import(name, *args, **kwargs)

    real_import = __import__
    monkeypatch.setattr("builtins.__import__", fail_import)

    with pytest.raises(SystemExit) as exc:
        cmd_dashboard(_ns())

    assert exc.value.code == 2
    assert "Feishu runtime fork" in capsys.readouterr().err
