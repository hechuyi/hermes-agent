"""Pin compaction handoff semantics so stale Active Task cannot override users."""

from agent.context_compressor import (
    HISTORICAL_IN_PROGRESS_HEADING,
    HISTORICAL_PENDING_ASKS_HEADING,
    HISTORICAL_REMAINING_WORK_HEADING,
    HISTORICAL_TASK_HEADING,
    SUMMARY_PREFIX,
)


def test_no_resume_exactly_directive():
    """The prefix must not tell the model to resume Active Task verbatim."""
    assert "resume exactly" not in SUMMARY_PREFIX.lower()


def test_latest_message_wins_on_conflict():
    """The latest user message must explicitly win over stale summary work."""
    lower = SUMMARY_PREFIX.lower()
    assert "latest user message" in lower
    assert "wins" in lower or "supersede" in lower or "discard" in lower


def test_handoff_sections_are_framed_as_historical():
    """Referenced summary headings must not read as current-turn tasks."""
    lower = SUMMARY_PREFIX.lower()
    assert "## active task" not in lower
    assert "## pending user asks" not in lower
    assert "## remaining work" not in lower
    assert HISTORICAL_TASK_HEADING.lower() in lower
    assert HISTORICAL_IN_PROGRESS_HEADING.lower() in lower
    assert HISTORICAL_PENDING_ASKS_HEADING.lower() in lower
    assert HISTORICAL_REMAINING_WORK_HEADING.lower() in lower


def test_no_background_consistency_carveout():
    """Topic overlap must not license stale-task resumption."""
    lower = SUMMARY_PREFIX.lower()
    assert "you may use the summary as background" not in lower
    assert "topic overlap" in lower


def test_reverse_signals_called_out():
    """Stop/undo/topic-change signals must be named as cancellation triggers."""
    lower = SUMMARY_PREFIX.lower()
    reverse_terms = ["stop", "undo", "roll back", "never mind", "just verify"]
    hits = sum(1 for term in reverse_terms if term in lower)
    assert hits >= 3


def test_replaced_prefixes_are_frozen_for_renormalization():
    """Retired SUMMARY_PREFIX values must remain detectable after upgrade."""
    from agent.context_compressor import (
        _HISTORICAL_SUMMARY_PREFIXES,
        ContextCompressor,
    )

    carveout_era = [
        p for p in _HISTORICAL_SUMMARY_PREFIXES
        if "you may use the summary as background" in p
    ]
    assert carveout_era, "carveout-era prefix missing from frozen tuple"
    assert SUMMARY_PREFIX not in _HISTORICAL_SUMMARY_PREFIXES
    for old_prefix in _HISTORICAL_SUMMARY_PREFIXES:
        content = old_prefix + "\n## Summary body"
        assert ContextCompressor._is_context_summary_content(content)
        stripped = ContextCompressor._strip_summary_prefix(content)
        assert not stripped.startswith(old_prefix)
