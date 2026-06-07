"""Pin compaction handoff semantics so stale Active Task cannot override users."""

from agent.context_compressor import SUMMARY_PREFIX


def test_no_resume_exactly_directive():
    """The prefix must not tell the model to resume Active Task verbatim."""
    assert "resume exactly" not in SUMMARY_PREFIX.lower()


def test_latest_message_wins_on_conflict():
    """The latest user message must explicitly win over stale summary work."""
    lower = SUMMARY_PREFIX.lower()
    assert "latest user message" in lower
    assert "wins" in lower or "supersede" in lower or "discard" in lower


def test_reverse_signals_called_out():
    """Stop/undo/topic-change signals must be named as cancellation triggers."""
    lower = SUMMARY_PREFIX.lower()
    reverse_terms = ["stop", "undo", "roll back", "never mind", "just verify"]
    hits = sum(1 for term in reverse_terms if term in lower)
    assert hits >= 3
