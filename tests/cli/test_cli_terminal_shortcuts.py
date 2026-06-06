"""Regression tests for terminal navigation and focus escape sequences."""

from __future__ import annotations

import pytest

from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.keys import Keys

from hermes_cli.pt_input_extras import install_ignored_terminal_sequences


FOCUS_REPORT_SEQUENCES = ("\x1b[I", "\x1b[O")


@pytest.fixture
def restore_focus_sequences():
    original = {seq: ANSI_SEQUENCES.get(seq) for seq in FOCUS_REPORT_SEQUENCES}
    missing = {seq for seq in FOCUS_REPORT_SEQUENCES if seq not in ANSI_SEQUENCES}
    yield
    for seq in FOCUS_REPORT_SEQUENCES:
        if seq in missing:
            ANSI_SEQUENCES.pop(seq, None)
        else:
            ANSI_SEQUENCES[seq] = original[seq]


def _parse_keys(data: str):
    events = []
    parser = Vt100Parser(events.append)
    parser.feed_and_flush(data)
    return [(event.key, event.data) for event in events]


def test_focus_events_are_parser_level_ignored_before_prompt_buffer(
    restore_focus_sequences,
):
    install_ignored_terminal_sequences()

    assert _parse_keys("\x1b[O\x1b[Ihello") == [
        (Keys.Ignore, "\x1b[O"),
        (Keys.Ignore, "\x1b[I"),
        ("h", "h"),
        ("e", "e"),
        ("l", "l"),
        ("l", "l"),
        ("o", "o"),
    ]


def test_regular_escape_shortcuts_still_parse_normally(restore_focus_sequences):
    install_ignored_terminal_sequences()

    assert _parse_keys("\x1bg") == [(Keys.Escape, "\x1b"), ("g", "g")]


def test_install_is_idempotent(restore_focus_sequences):
    first = install_ignored_terminal_sequences()
    second = install_ignored_terminal_sequences()

    assert first in (0, 1, 2)
    assert second == 0


def test_install_preserves_existing_registrations(restore_focus_sequences):
    ANSI_SEQUENCES["\x1b[I"] = Keys.ControlA

    changed = install_ignored_terminal_sequences()

    assert ANSI_SEQUENCES["\x1b[I"] == Keys.ControlA
    assert ANSI_SEQUENCES["\x1b[O"] == Keys.Ignore
    assert changed == 1
