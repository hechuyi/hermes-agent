import types

import hermes_cli.main as main_mod


def test_set_process_title_prefers_setproctitle(monkeypatch):
    calls = []
    fake_mod = types.SimpleNamespace(setproctitle=lambda value: calls.append(value))
    monkeypatch.setitem(__import__("sys").modules, "setproctitle", fake_mod)

    main_mod._set_process_title()

    assert calls == ["hermes"]


def test_set_process_title_linux_ctypes_fallback(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "setproctitle", None)
    monkeypatch.setattr("platform.system", lambda: "Linux")
    calls = []

    class FakeLibc:
        def prctl(self, *args):
            calls.append(args)

    monkeypatch.setattr("ctypes.CDLL", lambda *args, **kwargs: FakeLibc())

    main_mod._set_process_title()

    assert calls == [(15, b"hermes", 0, 0, 0)]


def test_main_invokes_process_title_before_fast_exit(monkeypatch):
    calls = []
    monkeypatch.setattr(main_mod, "_set_process_title", lambda: calls.append("title"))
    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: calls.append("cleanup"))
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: True)

    main_mod.main()

    assert calls[:2] == ["title", "cleanup"]
