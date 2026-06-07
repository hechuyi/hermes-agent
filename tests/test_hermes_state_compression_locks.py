"""Tests for ``SessionDB`` compression-lock primitives."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    session_db = SessionDB(tmp_path / "state.db")
    try:
        yield session_db
    finally:
        session_db.close()


def test_acquire_succeeds_when_unlocked(db: SessionDB) -> None:
    assert db.try_acquire_compression_lock("sess1", "holder1") is True
    assert db.get_compression_lock_holder("sess1") == "holder1"


def test_acquire_blocks_second_holder(db: SessionDB) -> None:
    assert db.try_acquire_compression_lock("sess1", "holder1") is True
    assert db.try_acquire_compression_lock("sess1", "holder2") is False
    assert db.get_compression_lock_holder("sess1") == "holder1"


def test_release_allows_reacquire(db: SessionDB) -> None:
    assert db.try_acquire_compression_lock("sess1", "holder1") is True
    db.release_compression_lock("sess1", "holder1")
    assert db.get_compression_lock_holder("sess1") is None
    assert db.try_acquire_compression_lock("sess1", "holder2") is True


def test_release_with_wrong_holder_is_noop(db: SessionDB) -> None:
    assert db.try_acquire_compression_lock("sess1", "holder1") is True
    db.release_compression_lock("sess1", "holder_other")
    assert db.get_compression_lock_holder("sess1") == "holder1"


def test_release_when_unlocked_is_noop(db: SessionDB) -> None:
    db.release_compression_lock("never_locked", "holder1")
    assert db.get_compression_lock_holder("never_locked") is None


def test_locks_are_per_session(db: SessionDB) -> None:
    assert db.try_acquire_compression_lock("sess1", "holder1") is True
    assert db.try_acquire_compression_lock("sess2", "holder2") is True
    assert db.get_compression_lock_holder("sess1") == "holder1"
    assert db.get_compression_lock_holder("sess2") == "holder2"


def test_expired_lock_is_reclaimable(db: SessionDB) -> None:
    assert db.try_acquire_compression_lock(
        "sess1",
        "crashed_holder",
        ttl_seconds=0.05,
    ) is True
    time.sleep(0.1)
    assert db.get_compression_lock_holder("sess1") is None
    assert db.try_acquire_compression_lock("sess1", "fresh_holder") is True
    assert db.get_compression_lock_holder("sess1") == "fresh_holder"


def test_non_expired_lock_is_held(db: SessionDB) -> None:
    assert db.try_acquire_compression_lock("sess1", "holder1", ttl_seconds=60) is True
    assert db.try_acquire_compression_lock("sess1", "holder2") is False


def test_empty_session_ids_are_noops(db: SessionDB) -> None:
    assert db.try_acquire_compression_lock("", "holder1") is False
    db.release_compression_lock("", "holder1")
    assert db.get_compression_lock_holder("") is None


def test_concurrent_acquire_only_one_winner(db: SessionDB) -> None:
    results: list[bool] = []
    barrier = threading.Barrier(8)
    result_lock = threading.Lock()

    def try_acquire(idx: int) -> None:
        holder = f"thread_{idx}"
        barrier.wait()
        got = db.try_acquire_compression_lock("contended_session", holder)
        with result_lock:
            results.append(got)

    threads = [threading.Thread(target=try_acquire, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sum(1 for result in results if result is True) == 1
    assert sum(1 for result in results if result is False) == 7
    assert db.get_compression_lock_holder("contended_session") is not None
