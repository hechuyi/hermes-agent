# Feishu-native Package A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Package A from `docs/plans/2026-06-08-feishu-native-hermes-design.md` and `docs/plans/2026-06-08-feishu-native-hermes-master-plan.md`: Feishu-native contract foundations, audit/preflight readiness primitives, and fail-closed gates for every legacy Feishu bypass path, without enabling any Package B-G live Feishu behavior.

**Architecture:** Keep Package A mostly pure and fail-closed. Add focused contract modules under `gateway/`, extend the existing ledger with sanitized audit events, add an out-of-band broker context that ordinary model/tool JSON cannot forge, and route legacy Feishu doc/comment/action paths through a shared guard that denies by default.

**Tech Stack:** Python dataclasses, contextvars, deterministic JSON hashing, existing gateway event ledger/contract modules, pytest, existing `tools.registry` registration patterns.

---

## File Structure

- Create `gateway/feishu_contracts.py`: pure dataclasses, canonical hash helpers, authorization evidence, object grant checks, contract population helpers, stable failure classes.
- Create `gateway/feishu_action_plan.py`: pure `RenderPlan`, `RenderPlanPart`, `FeishuActionContract`, and validation helpers; no network calls.
- Create `gateway/feishu_legacy_guard.py`: out-of-band broker context and fail-closed checks for legacy Feishu tools/actions.
- Create `gateway/feishu_smoke.py`: pure smoke evidence classifier and TTL/invalidation helpers.
- Modify `gateway/conversation_scope.py`: add helper(s) needed to build `ConversationContract` without changing existing route semantics.
- Modify `gateway/platforms/feishu.py`: populate conversation contracts on inbound metadata where feasible; gate descriptor/status-card/generic-card/reaction paths; gate gateway exec approval-card/update prompt callbacks before side effects unless existing path can provide broker evidence.
- Modify `gateway/platforms/feishu_comment.py`: require broker context before injecting Feishu doc/drive tools or calling any direct comment-side Feishu API from the handler, including reaction, meta/comment query, list, wiki resolution, reply, add-comment, and cleanup operations.
- Modify `gateway/gateway_event_contract.py`: add contract/auth/action audit event names and validators.
- Modify `gateway/gateway_event_ledger.py`: persist new audit event families and preflight them without changing delivery semantics.
- Modify `tools/feishu_doc_tool.py` and `tools/feishu_drive_tool.py`: keep registration but make handlers/checks fail closed unless out-of-band broker context is present.
- Add tests:
  - `tests/gateway/test_feishu_contracts.py`
  - `tests/gateway/test_feishu_action_plan.py`
  - `tests/gateway/test_feishu_legacy_guard.py`
  - `tests/gateway/test_feishu_smoke.py`
  - Extend `tests/gateway/test_gateway_event_ledger.py`
  - Extend targeted Feishu adapter/comment tests where legacy paths are gated.

## Non-negotiable Boundaries

- No Package B-G live behavior expansion: no new Feishu send UX, no docs/comment read/write enablement, no authorization-provider-backed business tools, no calendar/task/contact/Feishu approval instance tools, no Base/Sheets data-object behavior, no Drive/wiki search, no cross-chat/admin behavior, no Minutes/VC/Mail/Slides/Whiteboard/Apps/Miaoda behavior.
- No user/model-forgeable broker grants: `_feishu_broker_grant` or similar user JSON keys must not bypass guards.
- Every legacy Feishu API path either has out-of-band broker context or fails before SDK call, synthetic command injection, or success ledger write.
- Denial audit write failure is not a successful denial. The denied path must return a typed failed/unknown/not-ready result and must not continue to the side effect.
- Logs have the same redaction standard as ledger events: no raw token, raw Feishu ID, raw document token, raw message/document/Feishu approval content, raw file path, or raw API response body.
- Existing positive tests for legacy Feishu side effects must become broker-context-only positives; skipped/deferred tests must not hide a legacy bypass.
- Every new behavior follows RED -> GREEN -> commit.

## Task 1: Canonical Hashes and Core Contract Types

**Files:**
- Create: `gateway/feishu_contracts.py`
- Test: `tests/gateway/test_feishu_contracts.py`

- [ ] **Step 1: Write failing hash tests**

Cover sorted mapping keys, missing vs explicit null, NFC Unicode normalization, schema version/hash version/domain separation, and SHA-256 output format.

Run: `uv run --extra dev pytest tests/gateway/test_feishu_contracts.py -q -k "hash"`

Expected: import failures.

- [ ] **Step 2: Implement canonical hash helpers**

Implement:

- `FeishuContractError`
- `canonical_contract_json(value)`
- `feishu_contract_hash(value, *, domain, version, schema_version=1, algorithm="sha256")`

Reject sensitive raw fields whose normalized key contains `token`, `secret`, `private_key`, `raw_message`, `document_content`, `file_path`, `open_id`, `user_id`, `union_id`, unless the key ends in `_hash` or is explicitly allowlisted metadata such as `token_class`.

- [ ] **Step 3: Run GREEN and commit**

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_contracts.py -q -k "hash"
git add gateway/feishu_contracts.py tests/gateway/test_feishu_contracts.py
git commit -m "feat(feishu): add contract hash primitives"
```

- [ ] **Step 4: Write failing dataclass and grant-denial tests**

Cover:

- `ConversationContract`
- `AuthorizationEvidence`
- `ObjectCapabilityGrant`
- `scope_assignment_status != scoped` denies
- route snapshot mismatch denies
- app-token-only evidence denies
- discovery-only evidence denies
- explicit confirmation alone cannot authorize P3 object actions
- shared context does not imply shared authority subject

Run: `uv run --extra dev pytest tests/gateway/test_feishu_contracts.py -q -k "grant or scope or evidence"`

Expected: missing symbols or failing decisions.

- [ ] **Step 5: Implement contract dataclasses and grant decision helper**

Implement:

- `HashedRef`
- `ConversationContract`
- `AuthorizationEvidence`
- `ObjectCapabilityGrant`
- `can_issue_object_grant(contract, evidence, *, object_type, action) -> tuple[bool, str | None]`

Keep real Feishu ACL/OAuth checks out of scope; only encode evidence kinds and fail-closed rules.

- [ ] **Step 6: Run GREEN and commit**

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_contracts.py -q
git add gateway/feishu_contracts.py tests/gateway/test_feishu_contracts.py
git commit -m "feat(feishu): add authorization evidence contracts"
```

## Task 2: Feishu Inbound Contract Population and Legacy Session Denial

**Files:**
- Modify: `gateway/conversation_scope.py`
- Modify: `gateway/platforms/feishu.py`
- Test: `tests/gateway/test_feishu_contracts.py` or focused adapter test

- [ ] **Step 1: Write failing population tests**

Build a fake Feishu session source/message event and assert:

- contract includes `platform_account_id`, `conversation_scope_id`, `route_partition_key`, `route_session_key_snapshot`, `scope_assignment_status`, `authority_subject_ref`, `identity_evidence_set`, `contract_hash`
- `ambiguous`, `legacy_unscoped`, `detached`, `backfilled`, `resume_pending`, `cli_handoff`, and `implicit_switch` states cannot produce authorizable contracts

Run: `uv run --extra dev pytest tests/gateway/test_feishu_contracts.py -q -k "populate or legacy_state"`

Expected: failures because population helper does not exist.

- [ ] **Step 2: Implement population helpers**

Add helper(s) that build `ConversationContract` from existing `ConversationScopeIdentity`, route key, session key snapshot, actor evidence, and session status. Prefer a pure helper in `gateway/feishu_contracts.py`; only add small call-site plumbing to `feishu.py` if needed.

- [ ] **Step 3: Run GREEN and commit**

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_contracts.py -q -k "populate or legacy_state"
git add gateway/feishu_contracts.py gateway/conversation_scope.py gateway/platforms/feishu.py tests/gateway/test_feishu_contracts.py
git commit -m "feat(feishu): populate conversation contracts"
```

## Task 3: Offline RenderPlan and ActionIntent Contracts

**Files:**
- Create: `gateway/feishu_action_plan.py`
- Test: `tests/gateway/test_feishu_action_plan.py`

- [ ] **Step 1: Write failing render/action tests**

Cover:

- post, markdown, plain text, table, code block, link, chunked message, image, file, card, and button plan snapshots
- card/button plans require opaque action digest, same-operator scope, expiry, route, payload hash
- raw tool arguments/method/path/body are rejected
- fallback matrix declares `preserve`, `replace`, or `drop` actions
- chunk group/index/count must be coherent
- attachment parts require source class and provenance hash

Run: `uv run --extra dev pytest tests/gateway/test_feishu_action_plan.py -q`

Expected: import failures.

- [ ] **Step 2: Implement pure planning dataclasses**

Implement:

- `RenderPlanPart`
- `RenderPlan`
- `FeishuActionContract`
- `validate_render_part`
- `validate_action_contract`

Return stable failure class strings; perform no network calls.

- [ ] **Step 3: Run GREEN and commit**

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_action_plan.py -q
git add gateway/feishu_action_plan.py tests/gateway/test_feishu_action_plan.py
git commit -m "feat(feishu): add action planning contracts"
```

## Task 4: Gateway Event Audit Contract Extensions

**Files:**
- Modify: `gateway/gateway_event_contract.py`
- Modify: `gateway/gateway_event_ledger.py`
- Test: `tests/gateway/test_gateway_event_ledger.py`

- [ ] **Step 1: Write failing audit-event matrix tests**

Test all Package A audit event families:

- `feishu_contract_observed`
- `feishu_authorization_evidence_observed`
- `feishu_authorization_evidence_denied`
- `feishu_auth_decision`
- `feishu_object_scope_resolved`
- `feishu_capability_granted`
- `feishu_capability_denied`
- `feishu_action_requested`
- `feishu_action_authorized`
- `feishu_action_denied`
- `feishu_action_executed`
- `feishu_tool_result_redacted`
- `feishu_api_failure`
- `feishu_legacy_tool_denied`
- `feishu_legacy_descriptor_denied`

Every event requires timestamp, correlation ID, sanitized hash fields, and failure class where applicable. Add negative tests for raw token/content/path fields.

Run: `uv run --extra dev pytest tests/gateway/test_gateway_event_ledger.py -q -k "feishu_audit or legacy_tool or authorization_evidence"`

Expected: unsupported event type.

- [ ] **Step 2: Extend event validators**

Add event requirements and validation branches in `gateway/gateway_event_contract.py`. Use safe strings/hashes only. Do not allow raw tokens, raw body content, raw document content, or raw file paths.

- [ ] **Step 3: Extend ledger storage and schema validation**

Add `feishu_audit_events` to state, persist append-only sanitized event records, cap list size if needed, and validate persisted records. Old v1 ledgers without this section should either be migrated deterministically with an empty section or fail closed if schema semantics require a version bump; document the chosen behavior in code comments.

- [ ] **Step 4: Run GREEN and commit**

Run:

```bash
uv run --extra dev pytest tests/gateway/test_gateway_event_ledger.py -q -k "feishu_audit or legacy_tool or authorization_evidence"
git add gateway/gateway_event_contract.py gateway/gateway_event_ledger.py tests/gateway/test_gateway_event_ledger.py
git commit -m "feat(feishu): audit native gateway events"
```

## Task 5: Package A Readiness and Preflight Primitives

**Files:**
- Create: `gateway/feishu_readiness.py`
- Test: `tests/gateway/test_feishu_readiness.py`
- May modify: `gateway/gateway_event_ledger.py`

- [ ] **Step 1: Write failing readiness tests**

Cover Package A readiness/preflight classifications:

- missing contract -> not ready with `feishu_contract_missing`
- route mismatch -> not ready with `feishu_route_snapshot_mismatch`
- missing grant when object action is requested -> not ready with `feishu_capability_missing`
- unknown delivery evidence -> not ready/degraded with `unknown_delivery_state`
- redaction failure -> not ready with `feishu_redaction_failed`
- legacy-tool bypass denial -> not ready/degraded with `feishu_legacy_tool_requires_broker`
- unchecked legacy surface -> not ready with `feishu_legacy_surface_unchecked`
- denial audit write failure -> not ready with `feishu_denial_audit_unavailable`

Run: `uv run --extra dev pytest tests/gateway/test_feishu_readiness.py -q`

Expected: import failures.

- [ ] **Step 2: Implement pure readiness classifier**

Implement `FeishuReadinessEvidence` and `classify_feishu_package_a_readiness`. This should be a pure classifier used by tests and future preflight integration; do not make startup depend on live Feishu API.

- [ ] **Step 3: Add preflight audit hook tests**

Add tests that audit events from Task 4 can be summarized into the readiness classifier without writing fake business events into the real ledger. The preflight/readiness summary must include legacy doc/drive/comment/descriptor/status-card/gateway-exec-approval/update-prompt/generic-card/reaction surfaces; a preflight that excludes a known legacy surface must fail closed instead of passing by omission.

Run: `uv run --extra dev pytest tests/gateway/test_feishu_readiness.py tests/gateway/test_gateway_event_ledger.py -q -k "readiness or preflight or feishu_audit"`

Expected: fail until summary helpers exist.

- [ ] **Step 4: Implement minimal summary helpers and commit**

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_readiness.py tests/gateway/test_gateway_event_ledger.py -q -k "readiness or preflight or feishu_audit"
git add gateway/feishu_readiness.py gateway/gateway_event_ledger.py tests/gateway/test_feishu_readiness.py tests/gateway/test_gateway_event_ledger.py
git commit -m "feat(feishu): classify package readiness"
```

## Task 6: Smoke Evidence Classification

**Files:**
- Create: `gateway/feishu_smoke.py`
- Test: `tests/gateway/test_feishu_smoke.py`

- [ ] **Step 1: Write failing smoke-state tests**

Cover:

- `acked` passes
- `sent_ack_not_supported` passes only when config declares ack unsupported
- `sent_ack_pending` is degraded/not deploy-pass
- `sent_ack_timeout` is degraded/not deploy-pass with `feishu_ack_timeout`
- `failed` fails
- `unknown` fails
- stale TTL returns `smoke_evidence_stale`
- commit/config/policy/schema/app/route mismatch invalidates evidence

Run: `uv run --extra dev pytest tests/gateway/test_feishu_smoke.py -q`

Expected: import failures.

- [ ] **Step 2: Implement pure smoke classifier**

Implement:

- `SmokeEvidence`
- `classify_smoke_evidence(evidence, current_identity, *, now) -> SmokeReadiness`

No network calls. This is a pure readiness primitive for later deploy gates.

- [ ] **Step 3: Run GREEN and commit**

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_smoke.py -q
git add gateway/feishu_smoke.py tests/gateway/test_feishu_smoke.py
git commit -m "feat(feishu): classify smoke evidence"
```

## Task 7: Out-of-band Legacy Feishu Guard

**Files:**
- Create: `gateway/feishu_legacy_guard.py`
- Test: `tests/gateway/test_feishu_legacy_guard.py`

- [ ] **Step 1: Write failing broker-context tests**

Cover:

- no context denies
- JSON args containing `_feishu_broker_grant` still deny
- only contextvar-created broker context allows
- context exits restore deny
- denial reason is `feishu_legacy_tool_requires_broker`

Run: `uv run --extra dev pytest tests/gateway/test_feishu_legacy_guard.py -q -k "context"`

Expected: import failures.

- [ ] **Step 2: Implement guard context**

Use `contextvars.ContextVar`, not kwargs or env vars. Implement:

- `FeishuBrokerContext`
- `feishu_broker_context(grant_handle, *, action_id, contract_hash, route_partition_key)`
- `current_feishu_broker_context()`
- `require_feishu_broker_context(kind, name, args=None) -> tuple[bool, str]`

The guard must ignore user/model-supplied `_feishu_broker_grant` keys.

- [ ] **Step 3: Run GREEN and commit**

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_legacy_guard.py -q -k "context"
git add gateway/feishu_legacy_guard.py tests/gateway/test_feishu_legacy_guard.py
git commit -m "feat(feishu): add legacy broker guard"
```

## Task 8: Gate Legacy Feishu Doc/Drive Tools and Comment Handler

**Files:**
- Modify: `tools/feishu_doc_tool.py`
- Modify: `tools/feishu_drive_tool.py`
- Modify: `gateway/platforms/feishu_comment.py`
- Test: `tests/gateway/test_feishu_legacy_guard.py`

- [ ] **Step 1: Write failing legacy tool tests**

Cover:

- `feishu_doc_read` check function returns false without context
- drive comment tool checks return false without context
- handlers do not call injected client when context is missing
- args with `_feishu_broker_grant` do not bypass
- with out-of-band broker context, old behavior can proceed to existing client checks
- `tools.registry` entries for legacy Feishu tool names are unavailable through ordinary discovery when broker context is absent
- the `hermes-feishu` composite toolset cannot make `feishu_doc` or `feishu_drive` available without broker context
- toolset check aliases cannot make `feishu_doc` or `feishu_drive` available without broker context
- if a legacy Feishu check is evaluated inside broker context, registry `check_fn` TTL/cache and `model_tools.get_tool_definitions(..., quiet_mode=True)` cache must not keep the tool discoverable after the broker context exits
- direct `tools.registry.dispatch(...)` and `model_tools.handle_function_call(...)` calls to legacy Feishu tools do not call the injected client without broker context

Run: `uv run --extra dev pytest tests/gateway/test_feishu_legacy_guard.py -q -k "doc or drive"`

Expected: tools are currently available when `lark_oapi` exists or handlers bypass the new guard.

- [ ] **Step 2: Apply guard to doc/drive tools**

Update check functions and handlers to call `require_feishu_broker_context`. Default fail closed.

- [ ] **Step 3: Write failing comment-handler tests**

Add focused tests proving `feishu_comment` denies before any direct Feishu client call without broker context. This includes add/delete reaction, meta query, comment batch query, list comments, list replies, wiki-token resolution that calls Feishu, agent/tool injection, reply, add whole comment, and delivery cleanup.

Run: `uv run --extra dev pytest tests/gateway/test_feishu_legacy_guard.py -q -k "comment"`

Expected: current handler has no guard.

- [ ] **Step 4: Apply guard to comment handler**

Gate comment-side doc/drive tool client injection and every direct comment-handler Feishu API action before the first client call. Do not break non-agent event admission tests unless they rely on formerly unsafe behavior; update expectations to typed fail-closed.

- [ ] **Step 5: Add audit-write tests for legacy denials**

Assert denied doc/drive/comment paths append `feishu_legacy_tool_denied` audit events through the gateway ledger before returning, and that no success ledger event is written on denial. Returning data sufficient for a caller to write later is not enough.

Also assert denial audit write failure returns a typed failed/unknown/not-ready result rather than a successful denial.

Run: `uv run --extra dev pytest tests/gateway/test_feishu_legacy_guard.py -q -k "audit or denied"`

Expected: fail until denied call sites append audit events through the gateway ledger before returning.

- [ ] **Step 6: Implement denial audit payloads and run GREEN**

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_legacy_guard.py -q
git add gateway/platforms/feishu_comment.py tools/feishu_doc_tool.py tools/feishu_drive_tool.py tests/gateway/test_feishu_legacy_guard.py
git commit -m "fix(feishu): gate legacy document tools"
```

## Task 9: Gate Legacy Descriptor, Status-card, Gateway Exec Approval-card, Update Prompt, Generic Card, and Reaction Paths

**Files:**
- Modify: `gateway/platforms/feishu.py`
- Extend: `tests/gateway/test_feishu_gateway_descriptor_send.py`
- Extend: `tests/gateway/test_feishu_approval_buttons.py`
- May extend: `tests/gateway/test_feishu_gateway_event_apply.py`

- [ ] **Step 1: Write failing descriptor/status-card tests**

Cover:

- `execute_feishu_request_descriptor` without broker context fails before SDK call
- `execute_status_card_action` without broker context fails before descriptor execution
- no `delivery_sent` is written on denied descriptor/status-card paths
- denied descriptor/status-card paths append `feishu_legacy_descriptor_denied` audit events before returning; exposing data for a caller to write later is not enough
- existing positive descriptor/status-card tests are converted to broker-context-only positive tests; skipped/deferred success cases do not hide a no-broker bypass

Run: `uv run --extra dev pytest tests/gateway/test_feishu_gateway_descriptor_send.py -q -k "requires_broker or status_card"`

Expected: current descriptor/status-card paths execute.

- [ ] **Step 2: Gate descriptor/status-card paths**

Call `require_feishu_broker_context` before descriptor SDK builders or status-card descriptor extraction can produce side effects. Return stable `SendResult(success=False, error=...)`.

- [ ] **Step 3: Write failing gateway exec approval-card/update prompt tests**

Cover:

- gateway exec approval-card callback without broker evidence cannot resolve Hermes gateway approval side effect
- update-prompt callback without broker evidence cannot resolve prompt side effect
- denial happens before `resolve_gateway_approval`, before audited prompt-card update, and before success ledger write
- denial appends `feishu_action_denied` audit events before returning; exposing data for a caller to write later is not enough
- existing unauthorized-user tests still pass

Run: `uv run --extra dev pytest tests/gateway/test_feishu_approval_buttons.py -q -k "requires_broker or approval or update_prompt"`

Expected: current callbacks can execute legacy side effects.

- [ ] **Step 4: Gate gateway exec approval-card/update prompt callbacks**

Gate before side effects. If existing synchronous UX must return a callback card, it may return a denial/noop card, but it must not resolve approval/update state or write success ledger without broker context.

- [ ] **Step 5: Write failing generic `/card` tests**

Cover:

- generic card action without broker context does not call message callback with `/card ...`
- denial reason is `feishu_legacy_card_action_requires_broker`
- denial appends `feishu_legacy_descriptor_denied` audit events before returning; exposing data for a caller to write later is not enough
- duplicate-token handling still prevents replay

Run: `uv run --extra dev pytest tests/gateway/test_feishu_approval_buttons.py -q -k "generic_card"`

Expected: current code routes synthetic command.

- [ ] **Step 6: Gate generic card path**

In `_handle_card_action_event`, deny before `synthetic_text = f"/card {action_tag}"` unless broker context is active.

- [ ] **Step 7: Write failing reaction callback tests**

Cover:

- reaction callback without broker context does not route a synthetic `reaction:*` message event
- reaction callback cannot issue object grants or object authorization evidence
- denial happens before synthetic event submission and before success ledger write
- denial appends `feishu_legacy_descriptor_denied` or a dedicated `feishu_reaction_denied` audit event before returning
- reaction replay/duplicate handling still prevents duplicate side effects

Run: `uv run --extra dev pytest tests/gateway/test_feishu_gateway_event_apply.py tests/gateway/test_feishu_approval_buttons.py -q -k "reaction or requires_broker"`

Expected: current code routes synthetic reaction text.

- [ ] **Step 8: Gate reaction path**

Represent reactions as typed event/action intent with route/session/operator binding or deny before synthetic text event submission. Reactions must not grant object authority.

- [ ] **Step 9: Run GREEN and commit**

Run:

```bash
uv run --extra dev pytest \
  tests/gateway/test_feishu_gateway_descriptor_send.py \
  tests/gateway/test_feishu_gateway_event_apply.py \
  tests/gateway/test_feishu_approval_buttons.py \
  -q -k "requires_broker or status_card or approval or update_prompt or generic_card or reaction"
git add gateway/platforms/feishu.py tests/gateway/test_feishu_gateway_descriptor_send.py tests/gateway/test_feishu_gateway_event_apply.py tests/gateway/test_feishu_approval_buttons.py
git commit -m "fix(feishu): gate legacy card actions"
```

## Task 10: Package A Verification and Scope Audit

**Files:**
- No production changes unless verification exposes a defect.

- [ ] **Step 1: Run Package A tests**

Run:

```bash
uv run --extra dev pytest \
  tests/gateway/test_feishu_contracts.py \
  tests/gateway/test_feishu_action_plan.py \
  tests/gateway/test_feishu_readiness.py \
  tests/gateway/test_feishu_smoke.py \
  tests/gateway/test_feishu_legacy_guard.py \
  tests/gateway/test_gateway_event_ledger.py \
  tests/gateway/test_feishu_gateway_descriptor_send.py \
  tests/gateway/test_feishu_gateway_event_apply.py \
  tests/gateway/test_feishu_approval_buttons.py \
  -q
```

- [ ] **Step 2: Run gateway preflight regression subset**

Run:

```bash
uv run --extra dev pytest tests/gateway/test_hermes_tools_gateway_event.py tests/gateway/test_gateway_event_ledger.py -q -k "preflight or feishu"
```

- [ ] **Step 3: Run scope-creep audit commands**

Run:

```bash
BASE=$(git merge-base HEAD origin/fix/live-gateway-hermes-tools)
git diff --name-only "$BASE"..HEAD
rg -n "calendar|task|contact|feishu_approval_instance|Base|Sheets|Drive search|wiki search|create event|approve/reject|directory search|cross-chat|group admin|Minutes|VC|Mail|Slides|Whiteboard|Apps|Miaoda|batch|export|room booking" gateway tools tests/gateway
```

Expected: no new Package B-G live enablement. Existing references may remain only where they are being gated, represented as offline contracts, classified for readiness/smoke, or tested for denial. If `git merge-base` cannot identify an upstream base, stop and require an explicit base commit instead of falling back to the last local commit.

- [ ] **Step 4: Commit verification fixes if needed**

If fixes are needed:

```bash
git add <files>
git commit -m "test(feishu): verify package a gates"
```

- [ ] **Step 5: Final status**

Run:

```bash
git status --short --branch
git log --oneline -10
```

Report commits and test commands.
