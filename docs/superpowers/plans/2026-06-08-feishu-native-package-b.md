# Feishu-native Package B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Package B from `docs/plans/2026-06-08-feishu-native-hermes-design.md` and `docs/plans/2026-06-08-feishu-native-hermes-master-plan.md`: current-conversation Feishu-native I/O with route-bound inbound admission, outbound RenderPlan delivery, brokered clarification/confirmation cards, lifecycle evidence, duplicate inbound idempotency, attachment provenance, and fail-closed denial of arbitrary local Feishu uploads.

**Architecture:** Build only the current-conversation IM surface on top of Package A contracts, guards, ledger, and readiness primitives. Inbound events must bind DM/group/thread/webhook/WebSocket route evidence to `ConversationContract`; outbound sends must execute typed `RenderPlan` parts through a Feishu current-conversation broker that writes lifecycle evidence before and after SDK calls. All interactive actions use opaque brokered action IDs, all attachment paths carry provenance, and all unsupported or out-of-scope requests fail before business side effects.

**Tech Stack:** Python dataclasses, existing Feishu platform adapter, Package A `ConversationContract`/`RenderPlan`/broker guard primitives, existing gateway event ledger, pytest, deterministic hashes, opaque fake IDs in tests.

---

## Goal and Scope

Package B delivers Feishu-native I/O only for the current conversation. The accepted route surfaces are DM, group chat, thread, webhook delivery route, and WebSocket inbound route binding when those surfaces can be tied to the same `ConversationContract` and current session route snapshot.

In scope:

- inbound current-conversation contract admission for DM/group/thread/webhook/WebSocket events
- duplicate inbound and replay idempotency before session dispatch
- delivery lifecycle for current replies and limited bot-owned edits
- `RenderPlan` outbound execution for text, post, markdown, code, table, link, chunk, and fallback parts
- opaque brokered clarification and confirmation cards whose action IDs are not model- or user-forgeable
- current-conversation delivery lifecycle evidence, including attempted, sent, ack/unknown/failed, and edit outcomes
- attachment provenance for inbound user attachments and generated Hermes attachments
- denial of arbitrary local file upload before any Feishu upload call
- tool surface and scope gates that expose only Package B current-conversation behavior

## Non-goals

Package B does not implement calendar, task, Feishu Approval business objects, Base, Sheets, Drive/wiki search, cross-chat or admin operations, generic OpenAPI passthrough, arbitrary file export, NixOS or remote-host changes, model/provider changes, or Package C-G live behavior. It also does not grant Feishu object authority, read arbitrary Drive objects, search organization content, manage chats, invite users, create events, mutate tasks, approve or reject approval instances, or export local workspace files.

## Preconditions

Package A commits, guards, ledger event families, readiness classifiers, and fail-closed legacy surface gates must already be merged on this branch. The worker must start from a clean worktree and must not revert unrelated user or peer changes.

Each Package B boundary must be developed TDD-first, committed independently, and reviewed before the next boundary expands behavior. A task may proceed only after its focused tests pass, `git diff --check` passes for its write-set, and a spec/quality review confirms no Package C-G live behavior was enabled.

## File Structure

Expected write-set for Package B implementation:

- Modify `gateway/platforms/feishu.py`: inbound route binding, current reply execution, limited bot-owned edit execution, Feishu card callbacks, duplicate inbound handling, webhook/WebSocket route admission, and upload denial call sites.
- Modify `gateway/feishu_contracts.py`: Package B admission helpers and current-conversation route predicates if Package A did not already expose them.
- Modify `gateway/feishu_action_plan.py`: Package B render-part validation refinements for current-conversation deliverability and fallback behavior.
- Modify `gateway/gateway_event_contract.py`: delivery lifecycle, inbound duplicate, attachment provenance, and action-card audit event validators.
- Modify `gateway/gateway_event_ledger.py`: append-only sanitized state for inbound idempotency, current delivery lifecycle, bot-owned edit evidence, and attachment provenance evidence.
- Modify `gateway/feishu_readiness.py`: Package B readiness summary, scope-audit findings, and stable failure classes.
- Add or extend focused tests under `tests/gateway/`, preferably:
  - `tests/gateway/test_feishu_current_conversation_admission.py`
  - `tests/gateway/test_feishu_inbound_idempotency.py`
  - `tests/gateway/test_feishu_current_delivery_lifecycle.py`
  - `tests/gateway/test_feishu_render_plan_outbound.py`
  - `tests/gateway/test_feishu_brokered_cards.py`
  - `tests/gateway/test_feishu_attachment_provenance.py`
  - `tests/gateway/test_feishu_upload_denial.py`
  - `tests/gateway/test_feishu_package_b_scope.py`

## Non-negotiable Boundaries

- Current conversation means same platform account, route partition, route session snapshot, authority subject, and conversation scope as the admitted inbound event.
- Webhook and WebSocket routes must be normalized into the same route-binding model; transport metadata alone is not authorization.
- Negative tests must assert stable failure class, zero business side effects, and persistent state unchanged or limited to allowed sanitized failure evidence.
- Duplicate inbound events and brokered card callback replays must not re-enter model/session dispatch, send a second reply, mutate delivery lifecycle as success, or create conflicting attachment provenance. B2 owns inbound event idempotency; B5 owns brokered card callback replay.
- RenderPlan execution must not accept raw method/path/body, raw OpenAPI descriptors, raw Feishu IDs beyond hashed/opaque refs, or local file paths as sendable attachments.
- Clarification and confirmation cards must use opaque broker-owned action IDs with route, operator, expiry, payload hash, and idempotency binding.
- Limited edits are allowed only for bot-owned messages created by Package B current-conversation delivery lifecycle and must fail closed for user messages, unknown ownership, unknown route, or stale lifecycle state.
- Logs and ledger events must not contain raw tokens, raw user IDs, raw open IDs, raw chat IDs, raw message bodies beyond sanitized content hashes where required, raw file paths, or raw SDK response bodies.

## Task B1: Inbound Current-conversation Contract Admission

**Purpose:** Admit only Feishu inbound events whose DM/group/thread/webhook/WebSocket metadata can be bound to a Package A `ConversationContract` for the current conversation.

**Write-set:**
- Modify: `gateway/platforms/feishu.py`
- May modify: `gateway/feishu_contracts.py`
- Test: `tests/gateway/test_feishu_current_conversation_admission.py`

**RED tests:**
- DM message admission creates a current-conversation admission record with contract hash, route partition, route session snapshot, actor/authority subject hash, transport kind, and reply anchor.
- Private DM continuity preserves the same contract/route/session/actor/reply-anchor binding across an inbound message and its current reply; mismatched DM actor, stale session snapshot, or missing reply anchor fails before dispatch.
- Group mention admission requires expected group route, actor evidence, mention evidence, reply anchor, route session snapshot, and contract hash; mismatched group route or absent mention evidence fails with `feishu_current_route_mismatch` or `feishu_current_route_evidence_missing`.
- Thread reply admission preserves thread/reply-to anchor binding and fails if a reply is detached from the active thread scope or the reply anchor is ambiguous.
- Webhook and WebSocket events normalize to equivalent contract/route/session/actor/reply-anchor evidence for the same fake event ID and route.
- Missing, ambiguous, stale, backfilled, or legacy-unscoped route, mention, actor, session, contract, or reply-anchor evidence fails before session dispatch.
- Negative cases assert zero session dispatch calls and only sanitized admission-denied evidence, if the ledger write is part of the call path.

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_current_conversation_admission.py -q
```

Expected RED: missing helpers, missing assertions, or current adapter admitting unbound events.

**GREEN implementation constraints:**
- Reuse Package A `ConversationContract` and route snapshot helpers; do not introduce a second route identity model.
- Normalize only current Feishu IM transports: `dm`, `group`, `thread`, `webhook`, `websocket`.
- Bind every admitted inbound surface, including DM, group mention, thread reply, webhook, and WebSocket, to the same stable tuple: contract hash, route partition, route session snapshot, actor hash, and reply anchor. A transport wrapper alone is never sufficient.
- Return typed failure results before model/session submission, tool dispatch, or delivery state mutation.
- Deny before dispatch when mention evidence or reply-anchor evidence is absent, ambiguous, stale, or mismatched.
- Use fake opaque placeholders in tests, such as `evt_fake_001`, `route_hash_fake_001`, and `actor_hash_fake_001`.

**Verification commands:**

```bash
uv run --extra dev pytest tests/gateway/test_feishu_current_conversation_admission.py -q
git diff --check
git diff --name-only
```

Before committing, manually or with a small script compare `git diff --name-only` against the B1 write-set and explain any extra file.

**Commit message suggestion:** `feat(feishu): admit current conversation inbound`

**Review focus:** Verify that every admitted event is route-bound and that every denied event exits before session dispatch or success ledger writes.

**Explicit don't-do list:** Do not add calendar/task/contact/doc search admission; do not infer authority from app token; do not use raw chat/user/message IDs in ledger; do not add cross-chat routing.

## Task B2: Duplicate Inbound and Replay Idempotency Hardening

**Purpose:** Make inbound Feishu event processing idempotent across duplicate delivery, replay, process restart, and concurrent webhook/WebSocket arrival.

**Write-set:**
- Modify: `gateway/platforms/feishu.py`
- Modify: `gateway/gateway_event_contract.py`
- Modify: `gateway/gateway_event_ledger.py`
- Test: `tests/gateway/test_feishu_inbound_idempotency.py`

**RED tests:**
- Processing the same inbound event ID twice dispatches to the session exactly once.
- A webhook duplicate after WebSocket admission is classified as `feishu_inbound_duplicate`.
- A WebSocket duplicate after webhook admission is classified as `feishu_inbound_duplicate`.
- Webhook and WebSocket delivery of the same canonical Feishu event identity, route partition, and contract hash use the same primary idempotency key, so transport fan-out cannot create two successful admissions.
- Restart replay from a populated ledger does not dispatch again.
- Concurrent duplicate admission records one winner and one duplicate without two business side effects.
- Legacy, non-brokered callback replay inputs fail closed if they reach this boundary, but B2 does not implement positive brokered card replay semantics; B5 owns that behavior.
- Negative cases assert stable failure class, zero additional business side effects, and unchanged persistent state except sanitized duplicate evidence.

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_inbound_idempotency.py -q
```

Expected RED: duplicate records are not durable or current route handling dispatches twice.

**GREEN implementation constraints:**
- Derive the primary idempotency key from canonical Feishu event identity, route partition, and contract hash; never from raw event bodies.
- Do not include physical transport kind in the primary key in a way that distinguishes webhook and WebSocket deliveries of the same logical Feishu event. Transport may appear only as sanitized audit evidence or as a normalized logical inbound class when it does not split one logical event into two keys.
- Persist enough sanitized evidence to survive restart.
- Treat replay as a first-class outcome, not a success path.
- If ledger append/check cannot prove uniqueness, fail closed with `feishu_inbound_idempotency_unknown`.

**Verification commands:**

```bash
uv run --extra dev pytest tests/gateway/test_feishu_inbound_idempotency.py -q
git diff --check
git diff --name-only
```

Before committing, manually or with a small script compare `git diff --name-only` against the B2 write-set and explain any extra file.

**Commit message suggestion:** `fix(feishu): harden inbound idempotency`

**Review focus:** Confirm atomicity of check-and-record behavior and absence of second dispatch, second delivery, or conflicting lifecycle state.

**Explicit don't-do list:** Do not rely on process-local sets as the only guard; do not mark duplicates as delivered; do not store raw event JSON; do not silently pass ledger failures.

## Task B3: Delivery Lifecycle for Current Replies and Limited Bot-owned Edits

**Purpose:** Track current-conversation reply delivery and allow only lifecycle-proven bot-owned message edits.

**Write-set:**
- Modify: `gateway/platforms/feishu.py`
- Modify: `gateway/gateway_event_contract.py`
- Modify: `gateway/gateway_event_ledger.py`
- Test: `tests/gateway/test_feishu_current_delivery_lifecycle.py`

**RED tests:**
- Current reply send writes attempted, sent, and ack/unknown/failed lifecycle evidence with sanitized route and message refs.
- SDK failure writes `feishu_delivery_failed` and no success event.
- Unknown ack support writes `feishu_delivery_ack_unknown`, not success.
- SDK success with no usable Feishu message ID writes `unknown_delivery_state`, no success lifecycle event, and only sanitized unknown-state evidence. This is distinct from `feishu_delivery_ack_unknown`, which is reserved for supported send responses whose message identity is usable but ack capability is unavailable or unobservable.
- Limited edit succeeds only when the message was created by the bot through Package B lifecycle and route/contract still match.
- Editing a user message, unknown message, stale message, cross-route message, or non-current conversation message fails before SDK call.
- Failed edit does not rewrite original delivery as success.
- Negative cases assert stable failure class, zero SDK calls where denial occurs before send/edit, and unchanged persistent state except sanitized failure evidence.

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_current_delivery_lifecycle.py -q
```

Expected RED: lifecycle evidence is incomplete or edit ownership is not enforced.

**GREEN implementation constraints:**
- Use Package A event validators for every lifecycle event; extend schemas only with sanitized Package B fields.
- Keep lifecycle append-only; if a projection is needed, compute it from events.
- Treat a send response without a usable Feishu message ID as `unknown_delivery_state`; it cannot create bot ownership proof, cannot unlock edits, and cannot be upgraded to success without a later route-bound lifecycle event that names the sanitized message ref.
- Treat edit as a new lifecycle action tied to the original delivery hash and bot ownership proof.
- Deny on missing ownership evidence or stale route snapshot.

**Verification commands:**

```bash
uv run --extra dev pytest tests/gateway/test_feishu_current_delivery_lifecycle.py -q
git diff --check
git diff --name-only
```

Before committing, manually or with a small script compare `git diff --name-only` against the B3 write-set and explain any extra file.

**Commit message suggestion:** `feat(feishu): track current delivery lifecycle`

**Review focus:** Check that delivery state is append-only, route-bound, and cannot be used to edit user-owned or unknown messages.

**Explicit don't-do list:** Do not add cross-chat sends; do not support arbitrary message edits; do not treat SDK response bodies as ledger payloads; do not hide failed ledger writes.

## Task B4: RenderPlan Integration for Feishu Outbound Text, Post, Markdown, Code, Table, Link, Chunk, and Fallback

**Purpose:** Execute Package A `RenderPlan` parts through Feishu current-conversation delivery without accepting raw descriptors or expanding scope.

**Write-set:**
- Modify: `gateway/platforms/feishu.py`
- Modify: `gateway/feishu_action_plan.py`
- Test: `tests/gateway/test_feishu_render_plan_outbound.py`

**RED tests:**
- Text, post, markdown, code block, table, and link parts render to Feishu-native outbound calls through the current route broker.
- Chunked plans preserve group ID, part index, count, and ordering; partial send failure reports the failed chunk and stops according to fallback policy.
- Fallback matrix applies `preserve`, `replace`, or `drop` deterministically when Feishu does not support a part.
- Oversized content is chunked or denied with `feishu_render_part_too_large`; it is not silently truncated.
- Image/file/attachment/local-path parts are denied with typed failure while the provenance subsystem from B6 is not implemented; B4 does not depend on a not-yet-existing positive attachment path.
- Raw method/path/body, raw OpenAPI descriptor, and raw SDK request fields are rejected before send.
- Negative cases assert stable failure class, zero SDK calls for validation denials, and no success lifecycle events.

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_render_plan_outbound.py -q
```

Expected RED: outbound send paths bypass RenderPlan or validation does not cover all part kinds.

**GREEN implementation constraints:**
- Keep renderer output pure; network calls belong only in the Feishu current-conversation broker/adapter layer.
- Use typed builders for text/post/markdown/code/table/link and a single lifecycle path for sends.
- Use deterministic fallback outcomes and record sanitized fallback evidence.
- Deny image, file, local-path, and generic attachment parts before upload with a stable unsupported/provenance-required failure class until B6/B7 provide provenance-positive tests and upload gates.
- Preserve existing non-Feishu adapters.

**Verification commands:**

```bash
uv run --extra dev pytest tests/gateway/test_feishu_render_plan_outbound.py -q
git diff --check
git diff --name-only
```

Before committing, manually or with a small script compare `git diff --name-only` against the B4 write-set and explain any extra file.

**Commit message suggestion:** `feat(feishu): deliver render plans to current chat`

**Review focus:** Ensure all outbound paths pass through RenderPlan validation, current route checks, lifecycle evidence, and Package B scope gates.

**Explicit don't-do list:** Do not send raw OpenAPI descriptors; do not upload arbitrary files; do not enable docs/comments/cards beyond B5 clarification/confirmation cards; do not change model output format.

## Task B5: Opaque Brokered Action IDs for Clarification and Confirmation Cards

**Purpose:** Support clarification and confirmation cards for the current conversation using broker-owned opaque action IDs that cannot be forged from user/model JSON.

**Write-set:**
- Modify: `gateway/platforms/feishu.py`
- Modify: `gateway/feishu_action_plan.py`
- Modify: `gateway/gateway_event_contract.py`
- Modify: `gateway/gateway_event_ledger.py`
- Test: `tests/gateway/test_feishu_brokered_cards.py`

**RED tests:**
- Creating a clarification card stores an opaque action ID bound to route, operator hash, contract hash, payload hash, expiry, and idempotency key.
- Creating a confirmation card stores the same binding and exact payload hash.
- Callback with wrong operator, wrong route, expired action, payload mismatch, duplicate action, or unknown ID fails before side effects.
- User/model-supplied action IDs, `_feishu_broker_grant`, raw payload paths, or raw SDK descriptors do not authorize a callback.
- Successful callback records action accepted/resolved lifecycle once and cannot be replayed.
- Brokered card callback replay does not trigger model/session dispatch, action execution, delivery success mutation, or a second resolved lifecycle event.
- Legacy callback inputs remain fail-closed, while positive replay semantics are implemented only for broker-owned clarification/confirmation action IDs.
- Negative cases assert stable failure class, zero business side effects, and unchanged persistent state except sanitized denied/replayed action evidence.

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_brokered_cards.py -q
```

Expected RED: action callbacks are not fully opaque or route/operator/expiry-bound.

**GREEN implementation constraints:**
- Generate action IDs as opaque broker handles; tests may use deterministic fake handles injected by a test clock/random provider.
- Store only hashes and opaque refs in ledger.
- Route callbacks through Package A broker context; do not pass grant evidence through user-visible JSON.
- Support only clarification and confirmation semantics needed for current conversation.
- Record callback replay as a one-time broker action lifecycle outcome owned by B5; inbound event idempotency in B2 must not be used as the positive card replay implementation.

**Verification commands:**

```bash
uv run --extra dev pytest tests/gateway/test_feishu_brokered_cards.py -q
uv run --extra dev pytest tests/gateway/test_feishu_legacy_guard.py tests/gateway/test_feishu_approval_buttons.py -q
git diff --check
git diff --name-only
```

Before committing, manually or with a small script compare `git diff --name-only` against the B5 write-set and explain any extra file. Include descriptor/status-card legacy tests too if the existing guard coverage is split across separate files in this branch.

**Commit message suggestion:** `feat(feishu): broker current conversation cards`

**Review focus:** Confirm that callback authority is unforgeable, one-time, route-bound, and side-effect-free on every denial.

**Explicit don't-do list:** Do not revive generic `/card` synthetic command routing; do not add Feishu Approval business actions; do not store raw card payloads or secrets; do not accept model-authored callback handles.

## Task B6: Inbound and Generated Attachment Provenance Closure

**Purpose:** Close attachment provenance for current-conversation inbound user attachments and Hermes-generated attachments before any attachment is rendered or uploaded.

**Write-set:**
- Modify: `gateway/platforms/feishu.py`
- Modify: `gateway/feishu_action_plan.py`
- Modify: `gateway/gateway_event_contract.py`
- Modify: `gateway/gateway_event_ledger.py`
- Test: `tests/gateway/test_feishu_attachment_provenance.py`

**RED tests:**
- Inbound user attachment provenance records source event hash, file key hash, MIME class, size class, retention class, route hash, contract hash, sensitivity classification, and redaction state.
- Renderer-generated attachment provenance records safe output root proof, producing tool/action ID, content hash, declared MIME class, size class, route hash, contract hash, delivery-plan hash, sensitivity classification, redaction state, retention policy, and source grant handles when generated from Feishu object data.
- Attachment render parts without provenance fail with `feishu_attachment_provenance_missing`.
- Provenance route mismatch, stale retention, MIME mismatch, size mismatch, missing or stale sensitivity, missing or stale safe output root proof, missing or mismatched producing tool/action ID, missing or mismatched content hash, missing redaction state, unknown redaction state, missing retention policy, missing required source grant handles, stale source grant handles, or unknown generator state fails before upload.
- Provenance evidence survives replay without duplicate upload side effects.
- Negative tests cover every required provenance field as missing, stale, mismatched, or unknown and assert stable denial rather than best-effort upload.
- Negative cases assert stable failure class, zero upload SDK calls, and unchanged persistent state except sanitized failure evidence.

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_attachment_provenance.py -q
```

Expected RED: attachments are not provenance-closed or upload checks are absent.

**GREEN implementation constraints:**
- Treat file keys, local paths, and raw URLs as sensitive; ledger stores only hashes/classes and opaque refs.
- Tie inbound attachments to the current inbound event; tie generated attachments to a Hermes-managed render/generation action.
- Inbound attachments must always record sensitivity classification. Renderer-generated attachments must prove all of: safe output root, producing tool/action ID, content hash, sensitivity classification, redaction state, retention policy, and source grant handles when generated from Feishu object data.
- Attachment provenance must be checked immediately before any upload call.
- Any required field that is missing, stale, mismatched, or unknown blocks upload with a stable denial class. Redaction failure, unknown sensitivity, unknown retention, unknown source grant state, or unknown safe-root proof never degrades to success.

**Verification commands:**

```bash
uv run --extra dev pytest tests/gateway/test_feishu_attachment_provenance.py -q
git diff --check
git diff --name-only
```

Before committing, manually or with a small script compare `git diff --name-only` against the B6 write-set and explain any extra file.

**Commit message suggestion:** `feat(feishu): close attachment provenance`

**Review focus:** Verify that every uploadable attachment has a complete current-conversation provenance chain and that denial occurs before upload.

**Explicit don't-do list:** Do not expose local paths; do not fetch arbitrary Drive/wiki objects; do not infer provenance from filename; do not upload on unknown MIME, size, retention, or redaction state.

## Task B7: Deny Arbitrary Local Feishu Upload Before Upload Call

**Purpose:** Fail closed for arbitrary local file upload/export attempts before any Feishu SDK upload call is constructed.

**Write-set:**
- Modify: `gateway/platforms/feishu.py`
- Modify: `gateway/feishu_action_plan.py`
- Test: `tests/gateway/test_feishu_upload_denial.py`

**RED tests:**
- RenderPlan part containing a local path without Package B generated-attachment provenance fails with `feishu_arbitrary_local_upload_denied`.
- Tool/model request to send `/tmp/fake-report.pdf`, workspace-relative paths, home-relative paths, symlink targets, or path traversal strings fails before upload.
- A valid generated attachment from Task B6 is allowed only when its provenance and content hash match.
- A valid inbound attachment echo is allowed only when current-event provenance permits that exact class of reply.
- Provenance-positive upload tests live here only after B6 has implemented complete provenance evidence; until then, B7 positive attachment cases must remain skipped or RED for the B6/B7 integration boundary rather than weakening B4 denial behavior.
- Upload denial writes sanitized failure evidence but no success lifecycle and no SDK upload call.
- Negative cases assert stable failure class, zero business side effects, and persistent state unchanged or limited to sanitized denial evidence.

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_upload_denial.py -q
```

Expected RED: local path handling reaches upload construction or lacks typed denial.

**GREEN implementation constraints:**
- Check arbitrary-local-file denial before path resolution, stat, MIME sniffing, or upload request creation where possible.
- Allow only opaque generated/inbound provenance handles from Task B6.
- Keep error messages sanitized; never echo full local paths.
- Preserve non-Feishu platforms unless their shared helper would otherwise leak through Feishu.

**Verification commands:**

```bash
uv run --extra dev pytest tests/gateway/test_feishu_upload_denial.py -q
git diff --check
git diff --name-only
```

Before committing, manually or with a small script compare `git diff --name-only` against the B7 write-set and explain any extra file.

**Commit message suggestion:** `fix(feishu): deny arbitrary local uploads`

**Review focus:** Confirm no Feishu upload call can be reached from an arbitrary local path, including workspace-relative and symlink-like inputs.

**Explicit don't-do list:** Do not add arbitrary file export; do not sanitize by basename and continue; do not probe local files after denial; do not create a generic upload allowlist.

## Task B8: Tool Surface and Package B Scope Gating

**Purpose:** Ensure user/model-visible Feishu tools expose only current-conversation Package B behavior and keep Package C-G behavior disabled.

**Write-set:**
- Modify: `gateway/platforms/feishu.py`
- Modify: `gateway/feishu_readiness.py`
- May modify: Feishu-related tool registration files only if they are already part of Package A gates
- Test: `tests/gateway/test_feishu_package_b_scope.py`

**RED tests:**
- Tool discovery exposes only the explicit model-visible Package B capability identifiers when readiness is satisfied: `feishu.current.reply.send`, `feishu.current.reply.edit_bot_owned`, `feishu.current.card.clarification.create`, `feishu.current.card.confirmation.create`, `feishu.current.attachment.echo_provenance`, and `feishu.current.attachment.send_generated`.
- Adapter-only identifiers are never model-visible and may be invoked only behind broker/readiness checks: `feishu.adapter.inbound.admit_current`, `feishu.adapter.delivery.record_lifecycle`, `feishu.adapter.card.resolve_callback`, `feishu.adapter.provenance.record_inbound_attachment`, `feishu.adapter.provenance.record_generated_attachment`, and `feishu.adapter.scope.audit`.
- Calendar, task, Feishu Approval business objects, Base, Sheets, Drive/wiki search, cross-chat/admin, generic OpenAPI, arbitrary file export, and legacy doc/comment surfaces remain unavailable.
- Package A legacy guards still deny doc/drive/comment/descriptor/status-card/generic-card/reaction bypasses without broker context.
- Readiness fails with `feishu_package_b_scope_creep` if a new live Feishu surface appears outside the Package B allowlist; any unknown/new Feishu tool name fails closed until deliberately classified as model-visible, adapter-only, or denied.
- Negative cases assert stable failure class, zero business side effects, and unchanged persistent state except sanitized scope-audit evidence.

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_package_b_scope.py tests/gateway/test_feishu_legacy_guard.py -q
```

Expected RED: scope audit helpers or tool visibility checks are incomplete.

**GREEN implementation constraints:**
- Express Package B allowlist as exact capability/tool identifiers split into model-visible and adapter-only sets; do not use broad labels such as "current-conversation primitives" as the test oracle.
- Keep generic OpenAPI, object-domain tools, and cross-chat/admin tools disabled unless Package A broker context is explicitly exercising a denial/legacy test.
- Scope readiness must fail closed on unknown Feishu tool names.
- Do not change NixOS, remote host, or model/provider configuration.

**Verification commands:**

```bash
uv run --extra dev pytest tests/gateway/test_feishu_package_b_scope.py tests/gateway/test_feishu_legacy_guard.py -q
uv run --extra dev pytest tests/gateway/test_feishu_approval_buttons.py -q
git diff --check
git diff --name-only
```

Before committing, manually or with a small script compare `git diff --name-only` against the B8 write-set and explain any extra file. Include descriptor/status-card existing tests if they are not already covered by `test_feishu_legacy_guard.py`.

**Commit message suggestion:** `feat(feishu): gate package b tool scope` or `fix(feishu): fail closed package b scope creep`

**Review focus:** Check the visible tool list and code diff for accidental Package C-G enablement.

**Explicit don't-do list:** Do not expose `lark-cli`/OpenAPI passthrough; do not register calendar/task/Base/Sheets/Drive search; do not modify model/provider selection; do not add remote/NixOS changes.

## Task B9: Package Verification and Scope Audit

**Purpose:** Verify Package B as a complete current-conversation I/O increment and audit that Package A+B did not enable out-of-scope Feishu behavior.

**Write-set:**
- No production changes unless verification exposes a defect.
- Test/docs changes only if a verification finding proves the plan or implementation is incomplete.

**RED tests:** Not applicable as a feature task. Verification failures become new focused RED tests in the relevant B1-B8 area before any fix.

**GREEN implementation constraints:**
- If verification exposes a defect, return to the smallest responsible task boundary, add a failing test, implement the fix, rerun the focused and package commands, then commit.
- Do not batch unrelated fixes into the verification commit.

**Verification commands:**

Package B focused run:

```bash
uv run --extra dev pytest \
  tests/gateway/test_feishu_current_conversation_admission.py \
  tests/gateway/test_feishu_inbound_idempotency.py \
  tests/gateway/test_feishu_current_delivery_lifecycle.py \
  tests/gateway/test_feishu_render_plan_outbound.py \
  tests/gateway/test_feishu_brokered_cards.py \
  tests/gateway/test_feishu_attachment_provenance.py \
  tests/gateway/test_feishu_upload_denial.py \
  tests/gateway/test_feishu_package_b_scope.py \
  -q
```

Package A+B regression subset:

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
  tests/gateway/test_feishu_current_conversation_admission.py \
  tests/gateway/test_feishu_inbound_idempotency.py \
  tests/gateway/test_feishu_current_delivery_lifecycle.py \
  tests/gateway/test_feishu_render_plan_outbound.py \
  tests/gateway/test_feishu_brokered_cards.py \
  tests/gateway/test_feishu_attachment_provenance.py \
  tests/gateway/test_feishu_upload_denial.py \
  tests/gateway/test_feishu_package_b_scope.py \
  -q
```

Scope audit commands:

```bash
BASE=$(git merge-base HEAD origin/fix/live-gateway-hermes-tools)
git diff --name-only "$BASE"..HEAD
git diff "$BASE"..HEAD -- gateway tools tests/gateway docs/superpowers/plans
rg -n "calendar|task|Feishu Approval|approval instance|Base|Sheets|Drive search|wiki search|cross-chat|admin|generic OpenAPI|lark-cli|arbitrary file export|NixOS|remote|model provider|room booking|contact search|directory search" gateway tools tests/gateway docs/superpowers/plans
rg -n "upload|create_file|file_path|local path|openapi|method|path|body" gateway/platforms/feishu.py gateway/feishu_action_plan.py tests/gateway
```

Expected: Package B files show only current-conversation I/O, RenderPlan delivery, opaque brokered cards, lifecycle/idempotency/provenance, upload denial, and scope gates. Out-of-scope strings may appear only in denial tests, scope audit assertions, Package A legacy guards, or non-goal documentation. If `git merge-base` cannot identify an upstream base, stop and require an explicit base commit.

B9 scope audit must classify every grep match as one of:

- allowed denial/docs matches: non-goal documentation, explicit denial tests, legacy guard assertions, readiness/scope-audit findings, or sanitized failure-class strings.
- unexpected production/test enablement matches: production registration, tool discovery, broker execution, SDK call construction, permissive fixture, or success-path test evidence for Package C-G behavior.

Any unexpected production/test enablement match is a verification failure and must be routed back to the smallest responsible B1-B8 task before Package B is marked complete.

**Commit message suggestion:** `test(feishu): verify package b scope`

**Review focus:** Confirm the package can be described as current-conversation Feishu-native I/O and nothing more.

**Explicit don't-do list:** Do not use verification as a reason to enable deferred business objects; do not rewrite Package A contracts without a focused regression; do not update deployment/remote/model configuration.
