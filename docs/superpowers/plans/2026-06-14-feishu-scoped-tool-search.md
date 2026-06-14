# Feishu Scoped Tool Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Absorb the useful part of upstream progressive tool disclosure while preserving this fork's Feishu/live-gateway session toolset boundary.

**Architecture:** Add the upstream `tool_search` bridge as a session-scoped catalog, not a process-global registry browser. Core Hermes and all Feishu-family toolsets remain direct or hidden by their existing readiness gates; only non-Feishu deferrable plugin/MCP tools already granted by the current scoped tool definitions may be searched, described, or invoked through `tool_call`.

**Tech Stack:** Python, existing `tools.registry`, `toolsets`, `model_tools.get_tool_definitions`, `agent.tool_executor`, pytest, ruff.

---

## File Map

- Create: `tools/tool_search.py`
  - Pure catalog, BM25 search, bridge schemas, config parsing, and scoped dispatch helpers.
- Create: `tests/tools/test_tool_search.py`
  - Contract tests for classification, activation, scoped catalog, bridge dispatch, and Feishu-visible tool preservation.
- Modify: `model_tools.py`
  - Add `skip_tool_search_assembly`, optional `context_length`, bridge assembly as the final schema step, bridge dispatch branch, scoped toolset args on `handle_function_call`, and a shared scope fingerprint helper for bridge cache/dispatch.
- Modify: `agent/agent_runtime_helpers.py`
  - Pass `agent.enabled_toolsets` / `agent.disabled_toolsets` into `handle_function_call`.
- Modify: `agent/tool_executor.py`
  - Unwrap `tool_call` to the underlying tool before guardrails/hooks/progress, but only when the underlying tool is in the session-scoped deferrable set.
- Modify: `hermes_cli/config.py`
  - Add default `tools.tool_search` config with `enabled: auto`.
- Modify: `docs/plans/2026-06-06-main-safe-fixes-integration.md`
  - Record local disposition for upstream commits `369075dc95b`, `7427b9d5812`, `17097761207`, `18c9e891068`, `a87f0a82a52`.
- Do not import the upstream live harness scripts by default.
  - `17097761207` and `a87f0a82a` are test-harness-only; keep them deferred unless there is a later explicit need for live A/B tooling.

## Non-Negotiable Runtime Contract

The bridge has no independent authority. It may only expose the deferrable subset of a scoped tool definition list that was already produced for the active session. A bridge call without explicit scoped definitions or explicit `enabled_toolsets` must fail closed; it must not interpret `enabled_toolsets=None` as permission to build a global catalog.

All bridge scope decisions use one shared `ToolSearchScopeKey` concept:

- registry generation;
- enabled toolsets;
- disabled toolsets;
- config file fingerprint;
- `HERMES_KANBAN_TASK` presence;
- Feishu broker context fingerprint;
- tool-search config mode and threshold inputs;
- context length used for assembly.

The key is not a security boundary by itself; it is the invalidation contract for assembly caches and executor unwrap caches. Dispatch authorization must still be checked against the current scoped deferrable names at call time.

Feishu-family toolsets are never deferred in this port. That includes `hermes-feishu`, `feishu_doc`, `feishu_drive`, and future toolsets whose names start with `feishu` or `hermes-feishu`. `hermes-feishu` Package B model-visible tools remain direct. Adapter-only, denied, or legacy Feishu tools must not become searchable/describeable through the bridge.

Transcript/API pairing semantics are strict: the assistant message remains the bridge call emitted by the model, and the tool result keeps the original bridge `tool_call_id`. Downstream policy and observability hooks that represent execution intent use the underlying tool name after successful unwrap; if unwrap/scope validation fails, no underlying hook or registry dispatch occurs and the result is an error for the original bridge call.

## To Do

### Task 1: Document The Scoped Contract

**Files:**
- Modify: `docs/superpowers/plans/2026-06-14-feishu-scoped-tool-search.md`

- [x] **Step 1: State the non-negotiable contract**

The bridge catalog is built only from the already-filtered tool definitions for the current session. It must not call unscoped `get_tool_definitions()` from bridge dispatch or unwrap code.

- [x] **Step 2: Define visible vs deferrable**

Core Hermes tools and Feishu-family tools stay visible or hidden by their existing direct readiness gates. Plugin/MCP tools outside `_HERMES_CORE_TOOLS` and outside Feishu-family toolsets may be deferred only if present in the session's filtered tool definitions.

- [x] **Step 3: Define explicit deferrals**

Live A/B harness scripts are deferred. Website docs are not required for this fork-local backend behavior.

- [x] **Step 4: Commit**

Run:

```bash
git add docs/superpowers/plans/2026-06-14-feishu-scoped-tool-search.md
git commit -m "docs: plan feishu-scoped tool search"
```

### Task 2: Add RED Tests For Session-Scoped Tool Search

**Files:**
- Create: `tests/tools/test_tool_search.py`

- [x] **Step 1: Write catalog and activation tests**

Cover:

- bridge disabled by config leaves schemas unchanged;
- forced-on mode replaces deferrable plugin/MCP tools with `tool_search`, `tool_describe`, `tool_call`;
- core tools remain visible and are refused by `tool_call`;
- bridge schemas are not themselves deferrable.

- [x] **Step 2: Write scoped regression tests**

Cover:

- `get_tool_definitions(enabled_toolsets=["allowed-plugin"], disabled_toolsets=None)` exposes only allowed deferrable names in `tool_search`;
- `tool_call` rejects a registered out-of-scope plugin/MCP tool;
- `tool_describe` rejects a registered out-of-scope plugin/MCP tool;
- `tool_search`, `tool_describe`, and `tool_call` fail closed when called without explicit scope;
- bridge assembly does not update `_last_resolved_tool_names` with out-of-scope tools;
- `scoped_deferrable_names()` returns only names from the supplied scoped definitions.

- [x] **Step 3: Write Feishu boundary tests**

Cover:

- `enabled_toolsets=["hermes-feishu"]` keeps model-visible Feishu Package B tools direct, not deferred;
- non-model-visible `hermes-feishu` package tools stay hidden by existing Package B filtering and cannot appear through `tool_search`.
- `feishu_doc` and `feishu_drive` tools are not deferrable bridge catalog entries, even when enabled directly.
- `tool_describe` rejects hidden, adapter-only, denied, and legacy Feishu identifiers.

- [x] **Step 4: Verify RED**

Run:

```bash
uv run --extra dev pytest tests/tools/test_tool_search.py -q -rs
```

Expected: fail because `tools.tool_search` and bridge plumbing do not exist yet.

### Task 3: Implement Scoped Bridge Assembly And Dispatch

**Files:**
- Create: `tools/tool_search.py`
- Modify: `model_tools.py`
- Modify: `hermes_cli/config.py`

- [x] **Step 1: Implement pure helpers in `tools/tool_search.py`**

Include config parsing, `classify_tools`, token estimate, activation gate, catalog entry construction, BM25 search, bridge schema builders, `assemble_tool_definitions`, `resolve_underlying_call`, `scoped_deferrable_names`, and `handle_bridge_call`.

- [x] **Step 2: Wire `model_tools.get_tool_definitions`**

Add keyword args:

```python
context_length: Optional[int] = None
skip_tool_search_assembly: bool = False
```

After dynamic schema rebuild and schema sanitization, call `tool_search.assemble_tool_definitions(...)` unless skipped. Cache keys must include these new args.

Expose one helper that returns the same scope fingerprint used by the schema cache. Executor unwrap cache must use this helper rather than inventing a shorter cache key.

- [x] **Step 3: Wire bridge dispatch**

In `handle_function_call`, accept `enabled_toolsets`, `disabled_toolsets`, and an optional already-scoped tool definition list. For bridge names, fail closed unless either an explicit scoped definitions list is supplied or `enabled_toolsets` is non-`None`. Rebuild the catalog using scoped `get_tool_definitions(..., skip_tool_search_assembly=True)` and reject any target not in that scoped deferrable catalog.

- [x] **Step 4: Add default config**

Add:

```yaml
tools:
  tool_search:
    enabled: auto
    threshold_pct: 10
    search_default_limit: 5
    max_search_limit: 20
```

- [x] **Step 5: Verify GREEN**

Run:

```bash
uv run --extra dev pytest tests/tools/test_tool_search.py -q -rs
```

- [x] **Step 6: Commit**

```bash
git add tools/tool_search.py model_tools.py hermes_cli/config.py tests/tools/test_tool_search.py
git commit -m "feat(tools): add session-scoped tool search bridge"
```

### Task 4: Unwrap Bridge Calls Under The Same Scope

**Files:**
- Modify: `agent/tool_executor.py`
- Modify: `agent/agent_runtime_helpers.py`
- Modify: `tests/run_agent/test_run_agent.py` or focused agent executor tests

- [x] **Step 1: Write RED tests for unwrap scoping**

Cover:

- sequential path unwraps `tool_call` so progress/hooks see the underlying tool name;
- concurrent path unwraps similarly;
- unwrap refuses out-of-scope targets and returns a tool error instead of invoking the underlying registry handler;
- `_invoke_tool` passes `enabled_toolsets` and `disabled_toolsets` into `handle_function_call`.
- original assistant/tool result pairing still uses the bridge tool call id, while execution callbacks use the underlying tool name only after scope validation succeeds.

- [x] **Step 2: Implement `_tool_search_scoped_names(agent)`**

Cache by the shared `ToolSearchScopeKey`, not just registry generation plus `agent.enabled_toolsets` and `agent.disabled_toolsets`. Build names from scoped `model_tools.get_tool_definitions(..., skip_tool_search_assembly=True)`.

- [x] **Step 3: Unwrap before pre-tool hooks and guardrails**

Use `tools.tool_search.resolve_underlying_call`. Preserve the original tool call id and transcript shape, but downstream execution, guardrails, callbacks, result storage, and activity feed should use the underlying tool name.

- [x] **Step 4: Pass toolset scope from agent runtime helper**

Add `enabled_toolsets=getattr(agent, "enabled_toolsets", None)` and `disabled_toolsets=getattr(agent, "disabled_toolsets", None)` to `handle_function_call`.

- [x] **Step 5: Verify GREEN**

Run:

```bash
uv run --extra dev pytest tests/tools/test_tool_search.py tests/run_agent/test_run_agent.py tests/agent/test_tool_dispatch_helpers.py -q -rs
```

- [x] **Step 6: Commit**

```bash
git add agent/tool_executor.py agent/agent_runtime_helpers.py tests/run_agent/test_run_agent.py tests/agent/test_tool_dispatch_helpers.py tests/tools/test_tool_search.py
git commit -m "fix(tool-search): enforce session scope during bridge unwrap"
```

### Task 5: Integration Record And Verification

**Files:**
- Modify: `docs/plans/2026-06-06-main-safe-fixes-integration.md`

- [ ] **Step 1: Update upstream disposition**

Record:

- `369075dc95b` manually absorbed as session-scoped bridge;
- `7427b9d5812` absorbed as mandatory scoping contract, not follow-up;
- `18c9e8910` absorbed if `_invoke_tool` dispatch assertion is updated;
- `17097761207` and `a87f0a82a` remain deferred as live harness scripts.

- [ ] **Step 2: Run final verification**

Run:

```bash
uv run --extra dev pytest tests/tools/test_tool_search.py tests/tools/test_delegate_toolset_scope.py tests/test_get_tool_definitions_cache_isolation.py tests/run_agent/test_run_agent.py -q -rs
uv run --extra dev pytest tests/gateway/test_feishu_package_b_scope.py tests/gateway/test_feishu_package_c_scope.py tests/hermes_cli/test_tools_config.py tests/agent/test_tool_dispatch_helpers.py -q -rs
uv run --extra dev ruff check tools/tool_search.py model_tools.py agent/tool_executor.py agent/agent_runtime_helpers.py tests/tools/test_tool_search.py
python -m py_compile tools/tool_search.py model_tools.py agent/tool_executor.py agent/agent_runtime_helpers.py tests/tools/test_tool_search.py
git diff --check
```

- [ ] **Step 3: Commit**

```bash
git add docs/plans/2026-06-06-main-safe-fixes-integration.md
git commit -m "docs: record scoped tool-search absorption"
```
