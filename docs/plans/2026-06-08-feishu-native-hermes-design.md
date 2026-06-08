# Feishu-native Hermes design

Status: draft for user review.
Date: 2026-06-08.
Scope: Hermes live gateway specialization for Feishu/Lark-native operation.

## Context

This document records the design direction for specializing this Hermes fork into a Feishu-native assistant. It is intentionally a design and gating document, not an implementation plan. No implementation should start from this document until it has passed user review.

The current fork already has a functioning Feishu live gateway with durable inbound/delivery ledger behavior, default-deny gateway guard semantics, explicit failed/unknown delivery states, and deployed live service evidence. The remaining problem is not basic connectivity. The remaining problem is turning Feishu from a transport adapter into a first-class, auditable interaction surface without reintroducing external `lark-cli` runtime dependency, raw OpenAPI passthrough, task-card-driven state, or silent success paths.

Relevant existing surfaces:

- `gateway/platforms/feishu.py`: Feishu adapter, inbound normalization, delivery, interactive descriptors, media handling, and adapter-local renderer logic.
- `gateway/conversation_scope.py`: current conversation identity and route partition helpers.
- `gateway/gateway_event_contract.py`: internal gateway event contract and preflight checks.
- `gateway/gateway_event_ledger.py`: durable JSON ledger for inbound, delivery, ack, session route, and compression events.
- `tools/feishu_doc_tool.py`, `tools/feishu_drive_tool.py`, and `gateway/platforms/feishu_comment.py`: existing Feishu document/comment paths that can call Feishu with tenant-token backed clients. These must be treated as legacy bypass risk until they are isolated behind the new broker.
- `tests/gateway/test_feishu_gateway_descriptor_send.py`, `tests/gateway/test_feishu_approval_buttons.py`, `tests/gateway/test_gateway_event_ledger.py`: current regression coverage for delivery lifecycle, descriptor sends, buttons, and ledger behavior.

## Design Position

Hermes should become "Hermes inside Feishu", not "Hermes calling a Feishu CLI". The runtime capability must be internal to Hermes: typed contracts, policy decisions, object capability grants, audited action dispatch, renderer plans, and Feishu API execution behind the gateway guard. `lark-cli` may remain useful for development, manual diagnostics, or one-off ops, but it must not be the runtime authority model.

The right MVP is not a universal Feishu workspace assistant. The right MVP is a current-conversation Feishu-native shell: correct chat/thread/session identity, native replies, stable message rendering, controlled attachment handling, and complete delivery evidence. Feishu business objects such as docs, comments, calendar events, tasks, Feishu approval instances, Base, Sheets, Drive search, group administration, and organization contacts should be added only after object-level capability and audit are in place.

This design also avoids model catalog churn. It does not encode the user's current accessible model/channel state as a Hermes product constraint, and it does not add new model-selection behavior.

## Non-negotiable Invariants

1. App token is not user authorization. Feishu app credentials prove bot/app capability only. They do not authorize reading a user's calendar, task list, Feishu approval queue, document, or arbitrary Drive object.

2. Unknown is not success. Missing message IDs, unsupported descriptor kinds, unknown Feishu API responses, stale smoke evidence, ledger write failures, truncated outputs, route mismatches, and unverified delivery outcomes must produce typed `unknown`, `failed`, `blocked`, or `not_ready` states.

3. Gateway final guard remains authoritative, but it is not the only guard. Any action that can create side effects must be authorized before the side effect is attempted. Final guard then verifies the result transition and prevents false success.

4. Feishu object discovery is not Feishu object authorization. A doc link, file key, comment token, calendar ID, task ID, or Feishu approval instance ID found in an event is only a candidate object reference. It does not grant read or write permission.

5. Conversation context may be shared; authority is not shared. Group/thread semantic context can be shared by design, but tool authorization must remain bound to actor, object, route, action, expiry, and policy version.

6. No raw OpenAPI or `lark-cli` passthrough. The model must not be allowed to pass arbitrary method/path/body/token_type to Feishu. Every action must use an internal typed descriptor and an allowlisted broker.

7. No task-card-first architecture. Cards and buttons are allowed only as interaction surfaces for typed actions. They are not the source of truth for state, authorization, or delivery success.

8. User authorization evidence is mandatory for user-domain objects. Actor identity, app token, object discovery, SDK availability, tool registration, or model availability cannot independently issue a grant. Calendar, task, Feishu approval instance, document, Drive, Base, Sheets, and file-export operations require explicit `AuthorizationEvidence`.

9. Legacy Feishu tools cannot bypass the broker. Existing Feishu document, Drive, comment, descriptor, and status-card execution paths must either be routed through `FeishuActionContract` and `ObjectCapabilityGrant`, or fail closed before they can call Feishu APIs.

10. Persistent grants are not an early-phase capability. P0 through P3 may use one-time or short session grants only. Persistent grants move to a later separately approved design with revocation, reauthentication, use limits, policy invalidation, and token-rotation semantics.

11. Broker context is out-of-band authority. It must be created by Hermes policy code in process-local state and cannot be represented by kwargs, environment variables, user JSON, serialized card payloads, or model-visible fields such as `_feishu_broker_grant`.

12. Denial audit is part of the denial. If a denied Feishu side-effect path cannot append its sanitized denial event, it must return a typed failed/unknown/not-ready state and must not continue to the side effect.

13. Logs and ledgers share the same redaction standard. Neither may contain app secrets, tenant tokens, user tokens, private keys, raw Feishu IDs, raw document tokens, raw message bodies, raw document content, raw Feishu approval content, unredacted file paths, or raw API response bodies.

## Architecture

### 1. ConversationContract

`ConversationContract` is the first-class proof that an inbound event, session, route, and delivery target belong together. It should be created during Feishu inbound normalization and then carried through session routing, renderer planning, delivery, action callbacks, and ledger events.

Minimum fields:

- `contract_version`
- `platform = "feishu"`
- `platform_account_id`, derived from a sanitized app/account identity
- `tenant_ref_hash` or equivalent tenant/app partition evidence when available
- `conversation_scope_id`
- `conversation_canonical_key_hash`
- `shared_context_scope_id`, when semantic transcript context is shared
- `authority_subject_ref`, with stable kind and hashed value
- `chat_type`
- `conversation_id` or sanitized chat reference
- `thread_id` or `root_message_id`
- `participant_mode`
- `actor_ref`, with stable kind and hashed value
- `route_partition_key`
- `route_session_key_snapshot`
- `session_id` and session isolation policy
- `scope_assignment_status`: `scoped`, `ambiguous`, or `legacy_unscoped`
- `identity_evidence_set`: sanitized evidence that explains which Feishu IDs were present and consistent
- `policy_version`
- `event_id`, `message_id`, inbound transport, timestamp
- `admission_reason` and `evidence_state`
- `contract_hash_version`
- `contract_hash_domain`
- `contract_hash`

The hash must be authorization-relevant, not only a tracing tag. Its canonical input set must include contract version, platform account, tenant/app partition, shared context scope, authority subject, conversation scope, route partition, route session snapshot, session ID, thread/root anchor, actor evidence, policy version, and evidence state. The hash domain and version must be explicit so action-plan hashes, render-plan hashes, object-grant hashes, and smoke-evidence hashes cannot be confused.

Canonical hash rules:

- use one deterministic JSON representation for all contract hashes
- sort mapping keys lexicographically
- represent missing fields as absent and explicit nulls as `null`; do not silently coerce one into the other
- sort arrays only when the field schema declares order-insensitive semantics; otherwise preserve event order
- Unicode-normalize string values with NFC before hashing
- preserve case unless a field schema declares case-insensitive semantics
- hash only redacted or hashed identifier values, never raw secrets, raw tokens, raw user IDs, raw document tokens, or raw message bodies
- include `contract_hash_domain`, `contract_hash_version`, and schema version inside the hashed payload
- use a named algorithm such as SHA-256 and record it in the schema
- fail with `feishu_contract_hash_unstable` if canonicalization cannot be reproduced

`contract_hash` is not a substitute for field-level verification. The broker must re-check actor, authority subject, object, route, session, policy, expiry, and object capability fields. Hash mismatch produces a typed failure such as `feishu_contract_hash_mismatch`; route snapshot mismatch produces `feishu_route_snapshot_mismatch`; legacy or ambiguous scope produces `feishu_scope_not_authorizable`.

Identity inconsistency across `open_id`, `user_id`, and `union_id`, missing platform account identity, name-only fallback, or ambiguous route evidence must fail closed and must not generate object grants. Only `scope_assignment_status = scoped` with matching current route, route snapshot, ledger lock, and contract hash may issue a grant.

Shared context and authority must be separate. A group/thread transcript may use `shared_context_scope_id` for semantic continuity, but `authority_subject_ref` remains per actor or per approved authority subject. Shared context never implies shared object capability, shared Feishu approval-instance authority, or shared write permission.

Adapter-owned raw details such as original Feishu payloads, receive ID type, bot open ID, mention parsing, card payload parsing, upload tokens, and SDK-specific response shapes should remain adapter-owned. Core and ledger should consume only normalized, sanitized, contract-shaped evidence.

### 2. FeishuActionContract and RenderPlan

Feishu rendering and action execution need to be split.

Renderer input is a structured Hermes response: text blocks, code blocks, tables, links, media references, attachments, action intents, surface constraints, locale, and current `ConversationContract`.

Renderer output is a `RenderPlan`, not a network operation. Each plan part should include:

- `plan_version`
- `part_kind`: text, post, markdown, file, image, card, update, delete, or fallback
- `render_feature_set`: the Feishu capabilities this part expects
- `contract_hash`
- `route_partition_key`
- `platform_account_id`
- `target_kind` and target reference hash
- `reply_anchor`
- `msg_type`
- sanitized content or payload
- `chunk_group_id`
- `chunk_index`
- `chunk_count`
- `fallback_policy` and fallback reason
- `fallback_matrix`, including whether actions are preserved, dropped, or replaced
- `required_scope`
- `capability_required`
- `attachment_source_class`
- `attachment_provenance_hash`
- `action_digest`, for buttons or interactive cards
- `button_operator_scope`
- `post_click_update_policy`
- `idempotency_key`
- `correlation_id`
- expected delivery transition

Action callback parsing should produce `FeishuActionContract` or `ActionIntent`, not directly execute SDK calls. Minimum fields:

- `action_schema_version`
- `contract_hash`
- `operator_ref`
- `operator_class`
- `route_partition_key`
- `session_id`
- `object_ref_hash`
- `allowed_operation`
- `required_scopes`
- `expiry`
- `payload_hash`
- `idempotency_key`
- `redaction_policy`
- `correlation_id`

Execution must go through the gateway guard and Feishu action broker. The broker re-computes relevant contract evidence, checks route/operator/object/expiry/idempotency, validates descriptor allowlists, applies policy, writes audit events, and only then calls the adapter/SDK.

Interactive cards and buttons are part of P1 rendering primitives, but only as typed action surfaces. A card may display a confirmation, clarification, or post-click state, but it is not the source of truth for authorization, delivery, or task state. Button payloads must contain only opaque action IDs and digests; they must not carry raw object tokens, Feishu API request bodies, or free-form tool arguments.

Existing Feishu descriptor, gateway exec approval-card, status-card, and generic card callback paths must be closed over this broker. During P0 they should either emit `FeishuActionContract` and pass broker validation, or return a typed failure before any SDK call or synthetic command injection. A mixed world where new brokered actions and legacy descriptor-side effects coexist is not acceptable.

Legacy Feishu action migration table:

| Existing path | P0 requirement |
| --- | --- |
| Descriptor send/update execution | Emit `FeishuActionContract` and broker through descriptor allowlist, or fail closed before SDK call |
| Gateway exec approval-card callbacks | Emit `FeishuActionContract` with same operator, route, session, payload hash, and expiry, or fail closed |
| Status-card patch/update actions | Emit typed action/update intent; card state is not authoritative |
| Generic card action to synthetic `/card ...` command | Replace with `ActionIntent`; synthetic command injection must be disabled or fail closed |
| Comment/doc/Drive tool-side Feishu calls | Require `AuthorizationEvidence` and object grant through broker |
| Reaction callbacks routed as synthetic text | Represent as typed event/action intent or fail closed; reactions cannot issue object grants or bypass route/session/operator checks |

### 3. AuthorizationEvidence

`AuthorizationEvidence` explains why Hermes is allowed to issue an object grant. It is required for all user-domain and object-domain Feishu operations. It is separate from `ConversationContract`: the conversation contract proves where and by whom the request was made; authorization evidence proves that the actor has the requested authority over the requested object and action.

Minimum fields:

- `authorization_evidence_id`
- `evidence_kind`: explicit user confirmation, user OAuth/delegated credential, verified Feishu object ACL, admin policy grant, app-owned object, or system test object
- `token_class`: none, app token, user token, delegated user token, system test credential, or app-owned object credential
- `authorization_subject_hash`
- `authorization_subject_kind`
- `object_type`
- `object_ref_hash`
- `allowed_actions`
- `scope`
- `acl_check_result`
- `acl_checked_at`
- `issuer`
- `issued_at`
- `expires_at`
- `revocation_state`
- `policy_version`
- `evidence_hash`
- `redaction_policy`

App token, object discovery, actor identity, tool name, SDK availability, and model availability are invalid grant issuers on their own. They may appear in evidence records as context, but they cannot produce an allow decision without a valid evidence kind.

Authorization evidence issuer taxonomy:

- `explicit_user_confirmation`: a same-actor, same-route confirmation event with payload hash and expiry.
- `user_delegated_credential`: a user-scoped credential with enough Feishu scopes and a non-expired token state.
- `verified_object_acl`: a Feishu ACL check proving the actor can perform the action on the object.
- `admin_policy_grant`: an administrator policy naming the actor or group, object scope, action set, expiry, and revocation path.
- `app_owned_object`: an object created by or owned by the bot/app where Feishu API semantics prove app-owned authority.
- `system_test_object`: a dedicated smoke-test object, never reusable for production authorization.

Missing, stale, revoked, ambiguous, object-mismatched, actor-mismatched, route-mismatched, insufficient-scope, app-token-only, or discovery-only evidence must deny with a stable failure class.

### 4. ObjectCapabilityGrant

`ObjectCapabilityGrant` is required for Feishu object operations. It is not required for plain current-conversation IM replies, but it is required when the system touches docs, comments, attachments beyond current safe upload, calendar, tasks, Feishu approval instances, Drive, Base, Sheets, contacts, group membership, or any cross-chat operation.

Minimum grant fields:

- `grant_id`
- `authorization_evidence_id`
- `issuer_kind`
- `issuer_hash`
- `subject_hash`
- `app_id_hash`
- `conversation_scope`
- `route_partition_key`
- `session_id`
- `authority_subject_hash`
- `object_type`
- `object_ref_hash`
- `actions`
- `origin_event_id`
- `policy_version`
- `rule_source`
- `issued_at`
- `expires_at`
- `grant_kind`: one-time or short session in P0-P3
- `max_uses`
- `use_count`
- `redaction_policy`
- `correlation_id`
- `contract_hash`

The grant is issued by the gateway/policy layer, not by the tool implementation. Tools receive opaque grant handles and typed action descriptors. They do not decide authorization from tool name, SDK availability, model availability, app token presence, actor identity alone, or object discovery alone.

Persistent grants are explicitly out of P0-P3. A future persistent-grant design must define revocation, reauthentication, maximum use count, renewal, policy-version invalidation, token-rotation behavior, cross-session migration rules, and audit compaction before it can be enabled.

### 5. Ledger and Audit Events

The existing delivery ledger should be extended with Feishu authorization and object-scope evidence. New event families should include:

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

Records should keep stable, non-sensitive evidence: hashed app/account, hashed actor, hashed authority subject, hashed chat/thread/object references, action, scope, policy version, rule source, authorization evidence kind, grant kind, decision, failure class, correlation ID, descriptor hash, payload hash, redaction count, and classified Feishu API code. Records must not store app secrets, tenant tokens, user tokens, document tokens, private keys, raw message bodies, raw document content, raw Feishu approval content, or unredacted file paths.

## Phased Plan

### P0a: Contract, Legacy Isolation, and Guard Foundation

Add `ConversationContract`, `AuthorizationEvidence`, `FeishuActionContract`, `ObjectCapabilityGrant`, validators, hash semantics, redaction policy, legacy-tool isolation, and ledger/audit event schemas. This phase should be mostly contract, fixture, and behavior-gate work. It must not add broad Feishu tools.

Required outcomes:

- Feishu inbound normalization produces a complete `ConversationContract`.
- Missing app/chat/thread/actor/route evidence produces typed failure.
- `scope_assignment_status` is carried into contract evidence and only `scoped` can authorize grants.
- `route_session_key_snapshot`, current route, ledger lock, and contract hash must all match before grant issuance.
- `shared_context_scope_id` and `authority_subject_ref` are separate fields.
- Legacy, ambiguous, detached, backfilled, resume-pending, CLI-handoff, and implicit-switch sessions cannot issue object grants.
- `session_locked` binds scope, route, and session, not only a session key.
- Renderer/action plans carry authorization-relevant contract hashes.
- Authorization evidence defaults to deny; object grants default to deny.
- Existing `tools/feishu_doc_tool.py`, `tools/feishu_drive_tool.py`, Feishu comment handlers, descriptor sends, gateway exec approval-card callbacks, status-card paths, generic card-action-to-`/card` paths, and reaction callbacks are either broker-routed or fail closed before SDK calls, direct comment-handler client calls, synthetic command injection, local state mutation, or success ledger writes.
- Tool registry tests prove that legacy Feishu tool names cannot bypass the broker by being discovered as ordinary tools, included through `hermes-feishu`, cached by quiet-mode tool definition lookup, cached through registry check TTL, or invoked through direct dispatch.
- Denied legacy paths write sanitized denial audit before returning; audit-write failure is a typed failure/unknown/not-ready state, not a successful denial.
- Log redaction tests cover Feishu denial and API failure paths, including comment-handler raw response logging.
- Ledger preflight can prove schema compatibility and fail closed on malformed state.
- Ledger schema version migration defines how old records are read. Old v1 records lacking contract/scope evidence cannot authorize grants and must be marked `legacy_unscoped` or `ambiguous`.
- Tests cover route mismatch, route snapshot mismatch, operator mismatch, missing capability, identity ambiguity, legacy session, implicit compression switch, delivery unknown, redaction failure, unsupported descriptor, and legacy tool bypass denial.

### P0b: Renderer and Action Plan Offline Matrix

Before enabling new Feishu user-visible behavior, define and test the renderer/action plan surface offline.

Required outcomes:

- `RenderPlan` supports part kind, feature set, chunk group/order, fallback matrix, attachment source class, button action digest, operator scope, post-click update policy, and delivery expectation.
- Interactive card/button primitives exist as typed action surfaces, not task-state authorities.
- Existing adapter-local rendering paths can be mapped into `RenderPlan` or are explicitly scoped out.
- Descriptor, gateway exec approval-card, status-card, generic card, and reaction callback payloads are represented as `FeishuActionContract`/`ActionIntent` fixtures with opaque payloads, same-operator scope, same-route/session binding, expiry, idempotency, and no side effects before broker approval.
- Fallback behavior is deterministic: if post falls back to markdown or text, the plan declares whether buttons/actions are preserved, replaced, or dropped.
- Offline snapshots cover post, markdown, plain text, code blocks, tables, links, long chunking, image/file parts, and interactive buttons.

### P1: Current-conversation Feishu-native I/O

Build the first user-visible Feishu-native layer:

- group mention admission and private chat continuity
- reply-to and thread anchoring
- post/markdown/plain fallback renderer
- code block, table, link, and long-message chunk handling
- typed interactive card/button primitives for confirmation and clarification only
- current-conversation message send and limited bot-owned message edit
- renderer-generated file/image responses from safe Hermes outputs
- inbound user attachments with file key, source event, size, MIME, retention, and redaction evidence

Attachment handling must be explicitly split:

- inbound user attachments: allowed only when tied to the current event and safe cache policy
- renderer-generated attachments: allowed only from Hermes-managed outputs with delivery plan evidence
- arbitrary local file sending: default deny, treated as file exfiltration unless backed by an explicit object grant

Safe Hermes outputs must have provenance closure: output root allowlist, producing tool/action ID, source grant handles when source data came from Feishu objects, content hash, sensitivity classification, redaction status, and retention policy. A file produced from historical cache, local filesystem, downloaded document, or sensitive tool output is not safe merely because it is located under a Hermes directory.

P1 should not include calendar, task, Feishu approval instance, Drive search, Base, Sheets, organization contact search, group management, or generic OpenAPI execution.

### P2: Current-event Docs and Comments

Add narrow document/comment capability only for objects discovered from the current event or an explicit user-provided object reference. Discovery alone is not authorization.

Allowed at this phase:

- resolve a candidate document/comment object from the event
- deny discovery-only requests with `feishu_authorization_evidence_missing`
- generate local summary or reply drafts only after valid authorization evidence and object grant
- commit a comment reply only with one-time grant, actor match, object match, content hash, origin event, route key, expiry, and idempotency key

Draft and commit must be separate:

- draft does not call Feishu write APIs
- commit calls Feishu only after explicit confirmation
- content changes require re-confirmation
- duplicate, expired, different-user, different-thread, and route-mismatched confirmations fail with typed classes

Authorization evidence can come from same-actor explicit confirmation plus verified object ACL, user-delegated credential with sufficient document/comment scope, admin policy grant for the object scope, or app-owned object authority. A pasted link, comment token in an event, wiki reverse lookup, or tenant-token API reachability is not enough.

### P3: Calendar, Task, Feishu Approval Instance, and Narrow Contacts

Add business object tools only after P0-P2 security and audit gates are proven.

Terminology is strict in this phase: a Hermes execution-confirmation card is a gateway exec approval card; it is not Feishu approval authorization evidence. A Feishu approval workflow object is a Feishu approval instance and uses separate capability scopes.

Confirmation-only is insufficient for every P3 operation. P3 authorization must come from an allowed evidence combination for the object and action:

| Object/action | Allowed evidence combination |
| --- | --- |
| Calendar busy/free read | `user_delegated_credential` with calendar read scope, or `verified_object_acl` proving actor access, or `admin_policy_grant` naming actor/object scope |
| Calendar create/update/invite/room booking | `user_delegated_credential` with the corresponding calendar write, invite, or room-booking scope; or `verified_object_acl` proving the actor has the corresponding write/booking permission; or `admin_policy_grant` naming the actor/object/action scope. Then add same-actor explicit confirmation, exact payload hash, one-time grant, route match, expiry, and idempotency |
| Task read | `user_delegated_credential` with task read scope, or `verified_object_acl`, or `admin_policy_grant` naming actor/object scope |
| Task create/update/status change | Task write evidence plus same-actor explicit confirmation, exact payload hash, one-time grant, route match, expiry, and idempotency |
| Feishu approval instance read | `user_delegated_credential` with approval read scope, or `verified_object_acl` proving actor can view that instance, or `admin_policy_grant` naming actor/object scope |
| Feishu approval instance approve/reject | Approver-authority evidence from `user_delegated_credential` with approval action scope or `verified_object_acl` proving actor is the approver, plus same-actor explicit confirmation, exact payload hash, one-time grant, route match, expiry, and idempotency |

`explicit_user_confirmation` alone never authorizes P3 read or write. It can only commit an already-authorized payload.

Calendar:

- first allow only actor-bound or explicitly referenced busy/free query with valid authorization evidence, or local draft generation without remote read/write
- event creation, invitations, room booking, or updates require calendar write/invite/booking authorization evidence, one-time object grants, exact payload hash, same-actor confirmation, route match, expiry, and idempotency key

Task:

- first allow current actor scoped read or explicit object read with valid authorization evidence
- create/update/status changes require task write authorization evidence, one-time object grant, exact payload hash, same-actor confirmation, route match, expiry, and idempotency key

Feishu approval instances:

- first allow narrowly scoped read of explicitly referenced or actor-owned Feishu approval items only with valid authorization evidence
- approve/reject is higher risk than task or calendar writes and must use a stricter, separate capability scope
- Feishu approval write actions must not be included in a generic confirmation flow
- Feishu approval write actions require exact payload hash, approver-authority evidence, one-time grant, same-actor confirmation, route match, expiry, idempotency key, and typed denial for insufficient approver authority

No P3 read path may become tenant-wide read. Calendar, task, and Feishu approval metadata are sensitive even when read-only.

Contacts:

- allow explicit name-to-Feishu-identity resolution only as a dependency of an already authorized calendar, task, or Feishu approval instance action
- deny directory browsing, organization-wide search, group member enumeration, and group administration in P3

### P3.5: Current-event Data Objects

Base and Sheets are data objects even when read-only. Current-event or explicit-reference read/summary may be considered only after object-level authorization evidence and object capability grants exist. Search, export, write, batch update, formula, dashboard, workflow, and cross-object aggregation remain P4+.

### P4: Cross-object and Administrative Feishu Capabilities

Only after the earlier phases are stable should Hermes consider:

- cross-chat send
- Drive search
- wiki/doc search
- organization contact lookup beyond explicit resolution
- group member operations
- Base and Sheets broad read, search, export, write, batch, formula, dashboard, and workflow APIs
- bulk operations
- automated Feishu approval workflows
- admin or tenant-wide capabilities
- Minutes/VC, Mail, Slides, Whiteboard, and Apps/Miaoda capabilities

These capabilities remain default off and require explicit policy, object/tenant capability grants, dedicated audit, and real smoke coverage. They should not be exposed as raw OpenAPI passthrough.

## Readiness, Smoke, and Deployment Gates

Readiness must be layered.

Liveness:

- process is running
- runtime status can be read

Local preflight:

- Feishu config is present and redacted in diagnostics
- state directory is writable
- ledger schema is valid
- app lock is available
- redaction is enabled
- pending, stale, unknown, failed delivery counts are reported
- contract/action/grant schema versions are supported

Local preflight against the real state directory must not inject business events into the live ledger. It may validate existing live ledger schema and use temporary probe state for synthetic event application, but real production state must not be polluted with fake inbound, fake delivery, or fake ack records.

Platform connectivity:

- WebSocket/webhook runtime state is current
- bot identity probe succeeds or fails with a typed platform-connectivity class
- platform error state is available without leaking credentials

Recent smoke evidence:

- evidence was produced by a dedicated smoke test chat/object
- evidence is within TTL
- evidence matches current Hermes commit, NixOS generation or Hermes unit revision, service unit hash, sanitized config hash, policy version, contract schema version, action schema version, grant schema version, app/account hash, and route key
- evidence includes a classified ledger lifecycle state: `acked`, `sent_ack_not_supported`, `sent_ack_pending`, `sent_ack_timeout`, `failed`, or `unknown`

Deploy gate:

- preflight passes against the real state directory
- runtime reports Feishu connected
- synthetic Feishu smoke passes in a dedicated test chat/object
- smoke evidence has matching app/account, route key, message ID, and an acceptable ledger lifecycle state

The real smoke should not run on every ordinary CI job and should not be required for basic process liveness. Service readiness may cite recent smoke evidence with a TTL, but stale smoke evidence must be `smoke_evidence_stale`, not ready. A conservative initial TTL is 24 hours for unchanged deployment identity; any Hermes commit, service unit hash, sanitized config hash, policy version, schema version, app/account hash, or route-key change invalidates the evidence immediately. External Feishu API instability should produce `external_feishu_unavailable` or degraded/retrying state, not a false success and not a reason to corrupt local state.

Smoke state machine:

- `acked`: inbound, pending, sent with valid message ID, and `feishu_ack` mapped to the delivery. This is full pass.
- `sent_ack_not_supported`: inbound, pending, and sent are complete, and the configured Feishu surface does not support an ack signal. This is deploy-pass but readiness should report `ack_not_supported`.
- `sent_ack_pending`: inbound, pending, and sent are complete, but the ack window has not expired. This is degraded and must not satisfy deploy gate.
- `sent_ack_timeout`: inbound, pending, and sent are complete, but expected ack did not arrive within the smoke ack TTL. This is degraded/not-ready for ack-verified readiness and must carry `feishu_ack_timeout`.
- `failed`: Feishu or policy returned a classified failure. This fails deploy gate.
- `unknown`: lifecycle evidence is missing, inconsistent, stale, or truncated. This fails deploy gate.

Deploy smoke should require `acked` when the configured Feishu surface supports ack. It may accept `sent_ack_not_supported` only when the adapter configuration declares ack unsupported for that surface. `sent_ack_pending` and `sent_ack_timeout` are not successful deploy-gate states.

Smoke evidence schema:

- `smoke_id`
- `created_at`
- `time_source`
- `expires_at`
- `hermes_commit`
- `nixos_generation`
- `service_unit_hash`
- `sanitized_config_hash`
- `policy_version`
- `contract_schema_version`
- `action_schema_version`
- `grant_schema_version`
- `app_account_hash`
- `route_partition_key`
- `test_chat_hash`
- `test_actor_hash`
- `delivery_id`
- `feishu_message_id_hash`
- `ledger_lifecycle_hash`
- `result`: `acked`, `sent_ack_not_supported`, `sent_ack_pending`, `sent_ack_timeout`, `failed`, or `unknown`
- `failure_class`

Minimum smoke lifecycle:

1. Send a unique `smoke_id` from an allowed test actor in a controlled Feishu test chat.
2. Observe `feishu_inbound`.
3. Observe `delivery_pending`.
4. Observe `delivery_sent` with valid Feishu `message_id`.
5. Observe `feishu_ack` mapped to the known delivery when ack is supported; otherwise record `sent_ack_not_supported`.
6. Replay or restart once and verify idempotency.
7. Verify logs contain no app secrets, tokens, raw open IDs, raw user IDs, document tokens, or raw private content.

Production conversation history must not be used as a substitute for dedicated readiness smoke evidence, and readiness evidence must never increase tool permissions.

## Phase-gated Acceptance Matrix

Each phase needs explicit acceptance cases, not only coverage labels. Every negative case must assert the stable failure class, assert that no Feishu SDK call was made when authorization failed before execution, and assert that no success ledger event was written.

P0a acceptance:

- missing platform account, chat, thread/root, actor, route, or identity evidence fails with typed contract failure
- `open_id/user_id/union_id` conflict fails closed
- `scope_assignment_status = ambiguous` or `legacy_unscoped` cannot issue grants
- detached sessions cannot issue grants and produce a stable failure class
- backfilled sessions cannot issue grants and produce a stable failure class
- resume-pending sessions cannot issue grants and produce a stable failure class
- CLI-handoff sessions cannot issue grants and produce a stable failure class
- implicit-switch or compression-route-mismatch sessions cannot issue grants and produce a stable failure class
- route snapshot/current route/ledger lock mismatch fails before renderer or SDK execution
- shared context with a different actor cannot reuse another actor's object grant
- existing Feishu doc/Drive/comment tools are denied or broker-routed; direct tenant-token calls are not reachable through ordinary tool discovery
- descriptor sends, gateway exec approval-card callbacks, status-card updates, generic card-action-to-`/card` callbacks, and reaction callbacks are broker-routed or fail before SDK calls, direct client calls, synthetic command injection, local state mutation, or success ledger writes
- legacy Feishu tool availability through toolsets, registry check cache, quiet-mode model tool cache, and direct dispatch cannot outlive broker context
- denial audit write failure blocks the path from reporting success
- old ledger schema without contract evidence cannot authorize grants

P0b acceptance:

- post, markdown, plain, table, code, link, chunked, image/file, and card/button render plans match snapshots
- fallback matrix states whether action payloads are preserved, replaced, or dropped
- button action digest, expiry, operator scope, route, and payload hash are present
- descriptor, gateway exec approval-card, status-card, generic card, and reaction callback fixtures use opaque action IDs, same-operator route/session binding, expiry, idempotency, and payload hash
- invalid or unsupported render/action descriptors fail before SDK execution, synthetic command injection, or success ledger writes

P1 acceptance:

- DM, group mention, thread reply, webhook, and WebSocket events produce correct scoped replies
- duplicate inbound events do not duplicate side effects
- successful sends prove `acked` or `sent_ack_not_supported` according to the smoke state machine
- `sent_ack_pending` and `sent_ack_timeout` are degraded/not-ready, not successful deploy-gate states
- SDK success without message ID becomes `unknown_delivery_state`
- Feishu API error classes become failed or unknown according to contract
- inbound attachments record file key, MIME, size, source event, cache retention, sensitivity, and redaction state
- renderer-generated attachments require provenance closure and content hash
- arbitrary local file sends are denied before upload

P2 acceptance:

- document/comment discovery without authorization evidence is denied
- read with missing, stale, revoked, wrong-actor, wrong-object, or app-token-only evidence is denied before SDK execution
- draft generation performs no Feishu write API call
- commit requires one-time grant, content hash, same actor, route match, object match, expiry, and idempotency
- duplicate, expired, different-user, different-thread, changed-content, and route-mismatched confirmations fail with typed classes

P3 acceptance:

- calendar/task/Feishu approval instance read is denied without valid actor-object authorization evidence
- tenant-wide read is denied even if app token and SDK are available
- calendar write, invitation, or room booking is denied when the only evidence is calendar read evidence plus confirmation
- calendar write, invitation, room booking, task write, and Feishu approval write require exact payload hash, same actor confirmation, one-time grant, route match, expiry, and idempotency
- Feishu approval instance write requires approver-authority evidence and cannot use generic task/calendar confirmation

Cross-cutting acceptance:

- callback, replay, restart, stale queue, resume, and compression paths cannot implicitly switch session
- policy denied, invalid grant, invalid descriptor, signature/token failure, Feishu 403/404, and redaction failure do not write success events
- unknown Feishu code, network failure after admission, ledger write failure, output truncation, and missing delivery evidence become typed unknown/failed states
- logs and audit contain stable hashes and failure classes, not app secrets, tenant tokens, user tokens, document tokens, raw IDs, raw document content, raw Feishu approval content, private file paths, or raw API response bodies

Real Feishu smoke should be opt-in or deploy-gated, not part of ordinary unit CI.

## Deployment Discipline

For NixOS deployment, only Hermes package pin, service command, and Hermes-specific service configuration should change. Do not change unrelated NixOS modules, boot behavior, firewall, SSH, credentials, cloud lifecycle, or host recovery channels as part of this feature.

Before activating:

- record current Hermes commit
- record current NixOS generation
- record Hermes service unit hash and sanitized config hash
- inspect flake/closure diff and confirm the allowlist contains only Hermes package pin, service command, or Hermes-specific service options
- run build/preflight against the target state directory
- record sanitized service status and log baseline

After activating:

- restart only Hermes gateway service unless a broader restart is explicitly required
- verify preflight, runtime connected state, and deploy smoke
- if gate fails, roll back the Hermes pin or Hermes-specific service change and repeat verification
- use `nixos-rebuild switch --rollback` only when the previous generation is proven to contain only the Hermes change being reverted, or when the user explicitly approves generation rollback

## Explicit Non-goals

- Do not make task/status cards the state authority.
- Do not add raw OpenAPI passthrough.
- Do not depend on `lark-cli` for runtime tool execution.
- Do not expose tenant-wide read/search by default.
- Do not treat Feishu app token as user authorization.
- Do not add broad Base/Sheets/Drive/calendar/task/Feishu approval writes in MVP.
- Do not send arbitrary local files to Feishu without object grant.
- Do not hardcode current model/channel availability into Hermes behavior.
- Do not change unrelated NixOS or remote-host configuration.

## Implementation Packages After Approval

After user approval, implementation planning should start with Package A only. Package B is listed to show sequence, not to start simultaneous live behavior work.

Package A:

1. P0a contract and ledger gate: `ConversationContract`, `AuthorizationEvidence`, `FeishuActionContract`, and `ObjectCapabilityGrant` schemas, canonical hash rules, validators, Feishu inbound population, legacy session fail-closed behavior, legacy Feishu tool isolation, legacy callback/descriptor/card-action isolation, and broker-denial tests.
2. P0b renderer/action planning: `RenderPlan` offline types and snapshots for post/markdown/plain/chunk/card/button/attachment plans, without broad Feishu tool exposure or live behavior expansion.
3. Readiness/preflight tests for missing contract, route mismatch, missing grant, unknown delivery, redaction failure, stale smoke evidence, legacy-tool bypass denial, and smoke ack state classification.

Package B:

1. P1 current-conversation send/reply/thread/chunk path bound to contract hash and route key.
2. P1 interactive confirmation/clarification cards through brokered `ActionIntent`.
3. P1 attachment boundary tests for inbound event files, Hermes outputs with provenance closure, and arbitrary local file denial.

Docs/comments, calendar, task, Feishu approval instance, Drive, Base, Sheets, group management, and cross-chat features are deliberately excluded from the first package.

## Internal Review Status

Passed multi-round subagent review on 2026-06-08.

Reviewed views:

- conversation/scope/session routing
- security, authorization, and failure semantics
- Feishu-native UX, renderer, and action dispatcher
- tests, readiness, smoke, and NixOS deployment discipline

Last resolved blocking issue: Calendar write, invite, and room-booking actions cannot be authorized by calendar read evidence plus confirmation; they require corresponding write/invite/booking authorization evidence.
