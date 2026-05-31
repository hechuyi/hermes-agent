import time
from types import SimpleNamespace

from hermes_state import SessionDB


def _make_scoped_session(
    db: SessionDB,
    session_id: str,
    *,
    parent_session_id: str | None = None,
    conversation_scope_id: str = "cs_a",
    route_partition_key: str = "route_a",
) -> None:
    db.create_session(
        session_id,
        "feishu",
        parent_session_id=parent_session_id,
        conversation_scope_id=conversation_scope_id,
        scope_assignment_status="scoped",
        route_session_key_snapshot=route_partition_key,
        route_partition_key=route_partition_key,
    )


def test_ensure_db_session_scope_uses_current_gateway_scope_metadata(tmp_path):
    from gateway.session_context import set_session_vars
    from run_agent import AIAgent

    db = SessionDB(db_path=tmp_path / "state.db")
    agent = AIAgent.__new__(AIAgent)
    agent._session_db = db
    agent._session_db_created = False
    agent.session_id = "agent-created"
    agent.platform = "feishu"
    agent.model = "test-model"
    agent._session_init_model_config = {}
    agent._cached_system_prompt = "system"
    agent._parent_session_id = None

    set_session_vars(
        conversation_scope_id="cs_agent",
        platform_account_id="feishu_app:agent",
        route_partition_key="route-agent",
    )

    agent._ensure_db_session()

    row = db.get_session("agent-created")
    assert row["scope_assignment_status"] == "scoped"
    assert row["conversation_scope_id"] == "cs_agent"
    assert row["route_partition_key"] == "route-agent"


def test_ensure_db_session_scope_requires_current_gateway_platform_account(tmp_path):
    from gateway.session_context import set_session_vars
    from run_agent import AIAgent

    db = SessionDB(db_path=tmp_path / "state.db")
    agent = AIAgent.__new__(AIAgent)
    agent._session_db = db
    agent._session_db_created = False
    agent.session_id = "agent-created-without-account"
    agent.platform = "feishu"
    agent.model = "test-model"
    agent._session_init_model_config = {}
    agent._cached_system_prompt = "system"
    agent._parent_session_id = None

    set_session_vars(
        conversation_scope_id="cs_agent",
        platform_account_id=None,
        route_partition_key="route-agent",
    )

    agent._ensure_db_session()

    row = db.get_session("agent-created-without-account")
    assert row["scope_assignment_status"] != "scoped"
    assert row["conversation_scope_id"] is None
    assert row["route_partition_key"] is None


def test_hygiene_compression_preserves_scope(tmp_path, monkeypatch):
    from agent.conversation_compression import compress_context

    db = SessionDB(db_path=tmp_path / "state.db")
    _make_scoped_session(db, "root", conversation_scope_id="cs_compress", route_partition_key="route-compress")

    agent = SimpleNamespace()
    agent.compression_enabled = True
    agent.session_id = "root"
    agent.platform = "feishu"
    agent.model = "test-model"
    agent._session_db = db
    agent._session_db_created = True
    agent._session_init_model_config = {}
    agent._cached_system_prompt = "old system"
    agent._last_flushed_db_idx = 0
    agent._todo_store = SimpleNamespace(format_for_injection=lambda: "")
    agent.context_compressor = SimpleNamespace(
        compress=lambda messages, **kwargs: [{"role": "user", "content": "compressed"}],
        compression_count=0,
        last_prompt_tokens=0,
        last_completion_tokens=0,
        _last_compress_aborted=False,
        _last_summary_error=None,
        _last_summary_fallback_used=False,
        _last_aux_model_failure_model=None,
        _last_aux_model_failure_error=None,
    )
    agent._memory_manager = None
    agent.log_prefix = ""
    agent._last_compression_summary_warning = None
    agent._last_aux_fallback_warning_key = None
    agent._compression_feasibility_checked = True
    agent.tools = []
    agent._invalidate_system_prompt = lambda: None
    agent._build_system_prompt = lambda system_message=None: "new system"
    agent.commit_memory_session = lambda messages: None
    agent._emit_warning = lambda message: None
    agent._emit_status = lambda message: None
    agent._vprint = lambda *args, **kwargs: None
    monkeypatch.setattr(
        "agent.conversation_compression.estimate_request_tokens_rough",
        lambda *args, **kwargs: 1,
    )

    compress_context(
        agent,
        messages=[{"role": "user", "content": "hello"}],
        system_message="system",
        approx_tokens=42,
    )

    assert agent.session_id != "root"
    child = db.get_session(agent.session_id)
    assert child["parent_session_id"] == "root"
    assert child["scope_assignment_status"] == "scoped"
    assert child["conversation_scope_id"] == "cs_compress"
    assert child["route_partition_key"] == "route-compress"


def test_hygiene_compression_does_not_launder_legacy_parent_with_gateway_scope(tmp_path, monkeypatch):
    from agent.conversation_compression import compress_context
    from gateway.session_context import set_session_vars

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("legacy-root", "feishu")

    agent = SimpleNamespace()
    agent.compression_enabled = True
    agent.session_id = "legacy-root"
    agent.platform = "feishu"
    agent.model = "test-model"
    agent._session_db = db
    agent._session_db_created = True
    agent._session_init_model_config = {}
    agent._cached_system_prompt = "old system"
    agent._last_flushed_db_idx = 0
    agent._todo_store = SimpleNamespace(format_for_injection=lambda: "")
    agent.context_compressor = SimpleNamespace(
        compress=lambda messages, **kwargs: [{"role": "user", "content": "compressed"}],
        compression_count=0,
        last_prompt_tokens=0,
        last_completion_tokens=0,
        _last_compress_aborted=False,
        _last_summary_error=None,
        _last_summary_fallback_used=False,
        _last_aux_model_failure_model=None,
        _last_aux_model_failure_error=None,
    )
    agent._memory_manager = None
    agent.log_prefix = ""
    agent._last_compression_summary_warning = None
    agent._last_aux_fallback_warning_key = None
    agent._compression_feasibility_checked = True
    agent.tools = []
    agent._gateway_session_key = "route-from-agent"
    agent._gateway_conversation_scope_id = "cs_agent"
    agent._gateway_platform_account_id = "feishu_app:agent"
    agent._gateway_route_partition_key = "route-agent"
    agent._invalidate_system_prompt = lambda: None
    agent._build_system_prompt = lambda system_message=None: "new system"
    agent.commit_memory_session = lambda messages: None
    agent._emit_warning = lambda message: None
    agent._emit_status = lambda message: None
    agent._vprint = lambda *args, **kwargs: None
    monkeypatch.setattr(
        "agent.conversation_compression.estimate_request_tokens_rough",
        lambda *args, **kwargs: 1,
    )

    set_session_vars(
        conversation_scope_id="cs_ctx",
        platform_account_id="feishu_app:ctx",
        route_partition_key="route-ctx",
        session_key="route-session-key",
    )

    compress_context(
        agent,
        messages=[{"role": "user", "content": "hello"}],
        system_message="system",
        approx_tokens=42,
    )

    child = db.get_session(agent.session_id)
    assert child["parent_session_id"] == "legacy-root"
    assert child["scope_assignment_status"] in {None, "legacy_unscoped"}
    assert child["conversation_scope_id"] is None
    assert child["route_session_key_snapshot"] is None
    assert child["route_partition_key"] is None


def test_hygiene_compression_does_not_create_scoped_child_from_legacy_parent_without_account(tmp_path, monkeypatch):
    from agent.conversation_compression import compress_context
    from gateway.session_context import set_session_vars

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("legacy-root", "feishu")

    agent = SimpleNamespace()
    agent.compression_enabled = True
    agent.session_id = "legacy-root"
    agent.platform = "feishu"
    agent.model = "test-model"
    agent._session_db = db
    agent._session_db_created = True
    agent._session_init_model_config = {}
    agent._cached_system_prompt = "old system"
    agent._last_flushed_db_idx = 0
    agent._todo_store = SimpleNamespace(format_for_injection=lambda: "")
    agent.context_compressor = SimpleNamespace(
        compress=lambda messages, **kwargs: [{"role": "user", "content": "compressed"}],
        compression_count=0,
        last_prompt_tokens=0,
        last_completion_tokens=0,
        _last_compress_aborted=False,
        _last_summary_error=None,
        _last_summary_fallback_used=False,
        _last_aux_model_failure_model=None,
        _last_aux_model_failure_error=None,
    )
    agent._memory_manager = None
    agent.log_prefix = ""
    agent._last_compression_summary_warning = None
    agent._last_aux_fallback_warning_key = None
    agent._compression_feasibility_checked = True
    agent.tools = []
    agent._gateway_session_key = "route-from-agent"
    agent._gateway_conversation_scope_id = "cs_agent"
    agent._gateway_platform_account_id = None
    agent._gateway_route_partition_key = "route-agent"
    agent._invalidate_system_prompt = lambda: None
    agent._build_system_prompt = lambda system_message=None: "new system"
    agent.commit_memory_session = lambda messages: None
    agent._emit_warning = lambda message: None
    agent._emit_status = lambda message: None
    agent._vprint = lambda *args, **kwargs: None
    monkeypatch.setattr(
        "agent.conversation_compression.estimate_request_tokens_rough",
        lambda *args, **kwargs: 1,
    )

    set_session_vars(
        conversation_scope_id="cs_ctx",
        platform_account_id="",
        route_partition_key="route-ctx",
        session_key="route-session-key",
    )

    compress_context(
        agent,
        messages=[{"role": "user", "content": "hello"}],
        system_message="system",
        approx_tokens=42,
    )

    child = db.get_session(agent.session_id)
    assert child["parent_session_id"] == "legacy-root"
    assert child["scope_assignment_status"] in {None, "legacy_unscoped"}
    assert child["conversation_scope_id"] is None
    assert child["route_session_key_snapshot"] is None
    assert child["route_partition_key"] is None


def test_resolve_resume_cross_scope_child_does_not_redirect(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    base = time.time() - 1000
    _make_scoped_session(db, "empty-root", conversation_scope_id="cs_a", route_partition_key="route-a")
    db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (base, "empty-root"))
    _make_scoped_session(
        db,
        "wrong-scope-child",
        parent_session_id="empty-root",
        conversation_scope_id="cs_b",
        route_partition_key="route-b",
    )
    db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (base + 1, "wrong-scope-child"))
    db.append_message("wrong-scope-child", "user", "must not resume")
    db._conn.commit()

    assert db.resolve_resume_session_id("empty-root") == "empty-root"


def test_resolve_resume_legacy_null_parent_does_not_redirect_to_scoped_child(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    base = time.time() - 1000
    db.create_session("empty-root", "feishu")
    db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (base, "empty-root"))
    _make_scoped_session(
        db,
        "scoped-child",
        parent_session_id="empty-root",
        conversation_scope_id="cs_child",
        route_partition_key="route-child",
    )
    db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (base + 1, "scoped-child"))
    db.append_message("scoped-child", "user", "resume migrated child")
    db._conn.commit()

    assert db.resolve_resume_session_id("empty-root") == "empty-root"
