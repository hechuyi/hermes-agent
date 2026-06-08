# Feishu-native Hermes master plan

Status: ready for user review.
Date: 2026-06-08.
Scope: make this Hermes fork a Feishu-native assistant, not a generic assistant with an external Feishu tool wrapper.
Implementation state: do not implement from this document until user approval. Current Package A contract work remains incomplete until its tests, reviews, and commits finish.

## Goal

Hermes should operate as a first-class Feishu assistant: it should understand Feishu conversation identity, reply surfaces, action callbacks, documents, comments, attachments, business objects, readiness evidence, and deployment state through internal Hermes contracts. Runtime authority must live inside Hermes: typed contracts, broker decisions, object capability grants, render plans, action plans, audit ledgers, readiness classifiers, and Feishu adapter execution. `lark-cli` and one-off OpenAPI exploration may remain development aids, but they must not be runtime authorization, dispatch, or state authority.

The initial deliverable is not a tenant-wide workspace automation system. The first usable product is a current-conversation Feishu-native assistant that can reliably receive, reason, reply, render, confirm, and report readiness in Feishu without silently succeeding, leaking raw identifiers, or letting old Feishu API paths bypass the broker.

## Current Baseline

The branch already contains a functioning Feishu live gateway and the start of Package A contract work. Existing relevant surfaces are:

- `gateway/platforms/feishu.py`: Feishu adapter, inbound handling, delivery, interactive card callbacks, descriptor sends, status-card actions, reaction/card synthetic events, and Drive comment event handoff.
- `gateway/platforms/feishu_comment.py`: existing document comment automation path with direct Feishu API calls and injected document/drive tools.
- `tools/feishu_doc_tool.py` and `tools/feishu_drive_tool.py`: legacy Feishu document/comment tools that currently represent bypass risk until broker-gated.
- `gateway/conversation_scope.py`: conversation and route partition helpers.
- `gateway/gateway_event_contract.py` and `gateway/gateway_event_ledger.py`: existing internal event contract and durable ledger.
- `gateway/feishu_contracts.py` and `tests/gateway/test_feishu_contracts.py`: partially implemented Package A contract primitives.

Existing design material:

- `docs/plans/2026-06-08-feishu-native-hermes-design.md`: architecture and phase constraints.
- `docs/superpowers/plans/2026-06-08-feishu-native-package-a.md`: executable Package A plan.

This master plan is the reviewable sequence that sits above both documents.

## Terminology Contract

`gateway_exec_approval_card` means Hermes' own execution-confirmation card used to approve or deny a pending Hermes tool/action. It is not a Feishu approval workflow object and must not be used as evidence that a user may approve or reject a Feishu approval instance.

`feishu_approval_instance` means a Feishu/Lark Approval business object. Reading, approving, or rejecting it requires Feishu approval-domain authorization evidence and its own object capability grant.

`broker context` means an out-of-band, process-local authority context created by Hermes policy code. It is not a kwargs field, env var, tool argument, user JSON key, serialized card payload, or model-visible handle. Any name such as `_feishu_broker_grant` in user/model-supplied data is untrusted data, not authority.

## Non-negotiable Invariants

Feishu app credentials are app authority, not user authority. App token reachability, SDK availability, object discovery, tool registration, model JSON, and actor identity alone cannot authorize reading or writing a user's documents, calendar, tasks, Feishu approval instances, Drive objects, Base records, Sheets, contacts, or cross-chat messages.

Unknown is not success. Missing contract fields, route mismatch, stale smoke evidence, unsupported descriptor kinds, SDK success without a message ID, unclassified Feishu API responses, ledger write failure, output truncation, stale grants, revoked evidence, and old ledger states must become typed `blocked`, `failed`, `unknown`, `degraded`, or `not_ready` states.

Discovery is not authorization. A Feishu URL, document token, file token, comment ID, calendar ID, Feishu approval instance ID, task ID, chat ID, or user ID in an inbound event is only a candidate reference. It does not grant object access.

Authority and shared context are separate. A group thread may share transcript context, but object grants remain bound to actor, authority subject, object, action, route, session, expiry, policy version, and evidence hash.

No raw OpenAPI passthrough is allowed in runtime behavior. The model must not choose arbitrary Feishu method/path/body/token type. Every side effect must go through a typed internal descriptor and allowlisted broker path.

Cards, buttons, and reactions are interaction surfaces, not state authority. They may display confirmation, clarification, progress, post-click state, or lightweight user feedback, but authorization and delivery state come from contracts, grants, idempotency keys, and ledger events.

Legacy Feishu paths must either route through broker context or fail before SDK calls, synthetic command injection, local success state mutation, or success ledger writes. This includes document tools, drive tools, direct comment-handler API calls, descriptor sends, status-card actions, `gateway_exec_approval_card` callbacks, update prompt buttons, generic card actions, and reaction callbacks.

Denial audit is part of the denial. If a denied Feishu side-effect path cannot append its sanitized denial event to the stable ledger, the path must return a typed `failed`, `unknown`, or `not_ready` state and still must not continue to the side effect.

Logs and ledger records have the same redaction standard. Neither may contain app secrets, tenant tokens, user tokens, private keys, raw Feishu IDs, raw document tokens, raw message bodies, raw document content, raw Feishu approval content, raw file paths, or raw API response bodies.

Legacy tests that currently assert success without broker context must be inverted. Positive tests for old descriptor, document, Drive, comment, `gateway_exec_approval_card`, update prompt, status-card, card action, or reaction paths are valid only inside broker context; `skip` or `deferred` markers must not hide a bypass.

Persistent grants are out of scope until separately designed. Early phases use one-time or short session grants only.

No model catalog churn belongs in this work. The user's current usable channel is not a Hermes product constraint.

## Capability Internalization Map

Hermes should internalize Feishu capability families as typed brokered abilities, not as exposed `lark-cli` commands.

The immediate core is Feishu IM and interaction runtime: inbound event normalization, actor/chat/thread identity, mention admission, route/session binding, reply anchoring, render planning, delivery lifecycle, card/button/reaction action contracts, and readiness smoke. This is the foundation for every later ability.

Document, Drive, and comment capabilities should become object-scoped abilities after Package A. They are currently the most important legacy bypass surface because the fork already has document/comment code paths. The correct near-term shape is current-event or explicit-reference document/comment support with object grant, draft/commit split, same-actor confirmation, content hash, route binding, expiry, and idempotency. Broad Drive search, wiki search, tenant-wide document lookup, and arbitrary file export stay deferred.

Calendar, task, and `feishu_approval_instance` support should be later business-object abilities. Read operations require user-delegated credential, verified object ACL, or admin policy grant. Write operations additionally require exact payload hash, same-actor confirmation, one-time grant, route match, expiry, and idempotency. `feishu_approval_instance` approve/reject needs approver-authority evidence and cannot share a generic confirmation path with calendar or tasks.

Contacts can be used narrowly to resolve explicitly named participants only when an already authorized calendar, task, or `feishu_approval_instance` action needs a Feishu identity. Organization-wide search, directory browsing, and group administration are administrative abilities and stay default off.

Base and Sheets need their own data-object phase. Even read, export, and search can leak sensitive data. The only near-term data-object shape is current-event or explicit-reference read/summary with `AuthorizationEvidence` and `ObjectCapabilityGrant`; broad search, export, write, batch, formula, workflow, and dashboard operations stay deferred.

Wiki search, broad Drive search, Minutes, VC, Mail, Slides, Whiteboard, Apps/Miaoda, group administration, cross-chat send, and bulk workflows are later administrative/cross-object packages unless a narrower user-approved object grant and dedicated smoke fixture exist.

## Package Sequence

### Package A: Contract, Broker, Audit, and Offline Planning

Package A is the current implementation package and must finish before any new live Feishu behavior is enabled.

Required outcomes:

- `ConversationContract`, `AuthorizationEvidence`, `ObjectCapabilityGrant`, `FeishuActionContract`, and `RenderPlan` exist as typed internal contracts.
- Canonical hashes are deterministic, domain-separated, schema-versioned, and never include raw tokens, raw Feishu IDs, raw document content, raw message bodies, or raw file paths.
- Feishu inbound normalization produces contract evidence or a typed non-authorizable state.
- Legacy, ambiguous, detached, backfilled, resume-pending, CLI-handoff, implicit-switch, and route-mismatched sessions cannot issue object grants.
- Out-of-band broker context exists and cannot be forged by user/model JSON keys.
- Legacy document, Drive, direct comment-handler API, descriptor, status-card, `gateway_exec_approval_card`, update-prompt, generic card-action, and reaction paths are denied or broker-routed before side effects.
- Tool discovery, `hermes-feishu` toolset membership, quiet-mode tool definition cache, registry TTL/cache, direct `registry.dispatch`, and `model_tools.handle_function_call` cannot bypass the broker.
- Denials append sanitized audit events before returning; denial audit write failure is itself a typed failed/unknown/not-ready result.
- Logs and audit outputs are covered by redaction tests.
- Readiness and smoke classifiers exist as pure primitives and do not pollute production ledgers with fake business events.
- Offline render/action plan snapshots cover text, post, markdown, table, code, links, chunking, image/file parts, card/button actions, fallback matrices, expiry, operator scope, payload hash, and idempotency.

Package A acceptance requires targeted unit tests for every negative case, `git diff --check`, scope-creep audit against the merge base, and subagent spec/quality approval.

### Package B: Current-conversation Feishu-native I/O

Package B makes the assistant visibly useful in Feishu while staying inside the current conversation.

Required outcomes:

- DM, group mention, thread reply, webhook, and WebSocket events bind to the right contract, route, session, actor, and reply anchor.
- Post, markdown, plain text, code block, table, link, and long-message chunk rendering flow through `RenderPlan`.
- Interactive cards are limited to clarification and confirmation actions represented as opaque brokered action IDs.
- Current-conversation sends and limited bot-owned message edits use delivery lifecycle evidence.
- Duplicate inbound events do not duplicate side effects.
- SDK success without a usable Feishu message ID becomes `unknown_delivery_state`.
- Inbound user attachments are tied to the current event with file key, source event, size, MIME, retention, sensitivity, and redaction state.
- Renderer-generated attachments require provenance closure: safe output root, producing tool/action ID, content hash, sensitivity classification, redaction state, retention policy, and source grant handles when generated from Feishu object data.
- Arbitrary local file sending is denied before upload.

Package B does not include calendar, task, `feishu_approval_instance`, Drive search, Base, Sheets, organization contact search, group administration, generic OpenAPI, or arbitrary file export.

### Package C: Authorization Providers and Broker Policy

Package C defines the object authorization provider layer without opening new business tools.

Required outcomes:

- Provider interfaces exist for user-delegated credential, verified object ACL, admin policy grant, app-owned object, system-test object, and explicit user confirmation.
- Fake providers and system-test providers exercise the broker allowlist and denial classes.
- Provider output is typed `AuthorizationEvidence`; provider availability, SDK reachability, app token, and object discovery still cannot issue grants alone.
- Provider errors, unsupported scopes, stale credentials, revoked evidence, and incomplete ACL responses fail closed with stable failure classes.
- This package does not expose document, calendar, task, `feishu_approval_instance`, Base, Sheets, search, or admin tools.

### Package D: Current-event Documents and Comments

Package D replaces the unsafe legacy document/comment behavior with brokered, current-object behavior.

Allowed shape:

- Resolve document/comment candidate objects only from current event evidence or explicit user-provided references.
- Deny discovery-only access with a stable failure class.
- Read or summarize only after valid authorization evidence and object grant.
- Generate reply drafts without Feishu write APIs.
- Commit a comment only after one-time grant, exact content hash, same actor, same object, same route, origin event, expiry, and idempotency.
- Changed content, duplicate confirmation, expired confirmation, different user, different thread, different object, and route mismatch all fail before SDK execution.

Package D still excludes broad Drive search, wiki-wide search, tenant-wide document lookup, automated document editing, Base/Sheets export, and persistent grants.

### Package E: Calendar, Task, and Narrow Contact Resolution

Package E adds calendar and task support after Packages A-D prove the broker and audit model. It also adds narrow contact resolution only as a dependency of already authorized calendar/task payloads.

Calendar:

- Busy/free read requires calendar read authorization evidence.
- Create, update, invite, and room booking require corresponding write/invite/booking authority plus same-actor confirmation and exact payload hash.
- Read evidence plus confirmation is not enough for writes.

Tasks:

- Read requires task read authorization evidence.
- Create, update, status change, and assignment require task write authority plus same-actor confirmation and exact payload hash.

Contacts:

- Explicit name-to-Feishu-identity resolution is allowed only when constructing or validating an already authorized calendar/task payload.
- Directory browsing, organization search, group membership lookup, and group administration remain deferred.

No Package E path may become tenant-wide read or write. Read-only metadata is still sensitive.

### Package F: Feishu Approval Instances

Package F handles `feishu_approval_instance` objects separately from Hermes `gateway_exec_approval_card` callbacks.

- Read requires Feishu approval read authority for the actor/object.
- Approve/reject requires approver-authority evidence, exact payload hash, same-actor confirmation, one-time grant, route match, expiry, and idempotency.
- Feishu approval writes use a separate stricter capability scope and cannot be hidden inside generic task/calendar confirmation.
- This package must not reuse `tools/approval.py` semantics as Feishu approval authorization evidence.

No Package F path may become tenant-wide read or write. Read-only Feishu approval metadata is sensitive.

### Package G: Data Objects, Cross-object, and Administrative Capabilities

Package G remains default off and separately approved.

Candidate abilities include Drive search, wiki/doc search, Base and Sheets read/search/export/write/batch operations, group membership operations, organization contact search beyond explicit resolution, cross-chat send, Minutes/VC retrieval, Mail, Slides, Whiteboard, Apps/Miaoda, bulk workflows, admin policy automation, and tenant-wide operations.

Each ability needs explicit policy, object or tenant capability grant, dedicated audit events, smoke fixtures, revocation semantics, and negative tests proving that app token, discovery, and raw OpenAPI availability do not authorize it.

## Engineering Execution Model

Each package should be implemented with subagent-driven development. The controller keeps ownership of sequencing and integration. Workers get disjoint file/module ownership; reviewers get exact plan/spec paths and code diff boundaries. No worker should inherit broad conversation history when a focused prompt is enough.

The safe parallelism pattern is:

- Contract/dataclass tests can run separately from renderer snapshot planning.
- Readiness/smoke pure classifiers can run separately after schema names stabilize.
- Legacy doc/drive/comment gates should be one slice because they share tool injection and Feishu comment state, including direct comment-handler API calls before the agent runs.
- Descriptor/status-card/`gateway_exec_approval_card`/update/generic card/reaction gates should be one slice because they share `gateway/platforms/feishu.py` callback and send-result semantics.
- Gateway event contract/ledger extensions should be serial with any audit-writing call-site work because event validation semantics are shared.
- Adapter live behavior and deployment readiness should be serial after Package A, because a false pass can mask unsafe runtime behavior.

Commit at each verified boundary: failing tests recorded, minimal implementation passing, reviewer-driven hardening, package verification, and documentation updates. Commits must be scoped to the package and must not include unrelated NixOS or model changes.

## Verification Matrix

Package verification must prove both positive and negative behavior. A test that only checks returned data is insufficient if a denied path can still make an SDK call, mutate local `gateway_exec_approval_card`/update state, inject a synthetic command, or write a success ledger event.

Minimum Package A test classes:

- canonical hash stability and sensitive-field rejection
- contract/evidence/grant issue and denial decisions
- route/session/scope/actor/shared-context isolation
- out-of-band broker context cannot be forged by args
- legacy tool discovery, `hermes-feishu` membership, quiet-mode model tool cache, registry TTL/cache, and direct dispatch cannot leak availability after broker context exits
- doc/drive tool handlers and comment handler calls deny before any injected client call without broker context
- descriptor/status-card/`gateway_exec_approval_card`/update/generic card/reaction calls deny before SDK calls, synthetic command injection, local state mutation, prompt resolution, or success ledger writes
- audit event schemas reject raw secrets, raw IDs, raw content, raw paths, and raw API responses
- denial audit write failure blocks the denied path from being reported as successful
- log redaction tests cover comment API failure logging and other Feishu denial paths
- readiness and smoke classifiers distinguish pass, degraded, failed, unknown, stale, legacy-denied, and unchecked legacy surfaces

Minimum Package B test classes:

- DM/group/thread/webhook/WebSocket route binding
- duplicate inbound idempotency
- delivery lifecycle: pending, sent, acked, ack unsupported, ack pending, ack timeout, failed, unknown
- render snapshots and fallback matrices
- inbound attachment provenance and arbitrary local file denial
- restart/replay does not duplicate delivery or switch route

Minimum Package C-G test classes:

- discovery-only denial
- missing/stale/revoked/wrong-actor/wrong-object/app-token-only evidence denial
- draft without write API
- commit with exact payload hash and one-time grant
- duplicate, expired, changed payload, different user/thread/route denial
- provider errors and unsupported scopes fail closed
- calendar/task read/write scope separation, especially calendar write, room booking, task status change, and `feishu_approval_instance` approve/reject
- Base/Sheets read/search/export/write separation and default-off administrative behavior

## Readiness and Deployment Gate

Local readiness is not equivalent to live deployment readiness. The deploy gate needs layered evidence:

- process liveness and readable runtime status
- redacted config presence
- writable state directory
- valid ledger schema
- Feishu audit schema compatibility and denial-event appendability
- route/session lock health
- supported contract/action/grant schema versions
- Feishu platform connectivity or typed platform failure
- recent smoke evidence from a dedicated test chat/object
- matching Hermes commit, NixOS generation, service unit hash, sanitized config hash, policy version, schema versions, app/account hash, and route key
- Package A legacy-surface readiness summary proving doc/drive/comment/descriptor/status-card/`gateway_exec_approval_card`/update/generic-card/reaction gates are checked, not skipped

Acceptable smoke outcomes:

- `acked`: full deploy pass when ack is supported.
- `sent_ack_not_supported`: deploy pass only when adapter configuration explicitly declares ack unsupported.

Non-passing outcomes:

- `sent_ack_pending`: degraded, not deploy-pass.
- `sent_ack_timeout`: degraded/not-ready with `feishu_ack_timeout`.
- `failed`: deploy fail.
- `unknown`: deploy fail.
- stale or mismatched evidence: deploy fail with a stable stale/mismatch failure class.
- unchecked legacy surface, missing denial audit evidence, or preflight that excludes a known legacy surface: deploy fail.

Deployment changes are restricted to Hermes package pin, service command, and Hermes-specific service configuration. Do not change unrelated NixOS modules, boot behavior, firewall, SSH, credentials, cloud lifecycle, or host recovery channels as part of Feishu-native Hermes work.

## Approval Boundary

User approval of this master plan should authorize only Package A completion and the preparation of Package B design details. It should not authorize live Package B-G rollout, broad Feishu tool exposure, persistent grants, raw OpenAPI passthrough, or unrelated infrastructure changes.

Before moving from one package to the next, produce:

- package plan update
- test list and expected failure classes
- subagent spec review
- subagent code-quality review
- verification command output summary
- commit list
- explicit statement of deferred capabilities

## Subagent Review Status

First review round completed on 2026-06-08:

- Product/Feishu capability review: Issues Found. Resolved here by adding narrow contacts, data-object phasing, reaction gating, Feishu-native later-capability inventory, and approval terminology split.
- Authorization/audit/broker review: Issues Found. Resolved here by strengthening comment-handler direct API denial, toolset/cache/direct dispatch tests, denial audit failure semantics, log redaction, and preflight legacy-surface coverage.
- Engineering execution/test review: Issues Found. Resolved here by splitting Package C-G, hardening parallel/serial boundaries, and making deploy evidence package-scoped.

Second-round subagent review completed on 2026-06-08:

- Security/broker review: Approved. No blocker remains for user review.
- Cross-document consistency review: Issues Found on first pass because Package A still used stale P1/P2/P3 boundaries. Resolved by changing Package A to prohibit all Package B-G live behavior and by expanding scope-creep audit coverage. Targeted re-review: Approved.

This document is ready to send to the user for approval.

## Convergence Notes

Root issue ledger:

- R1 Package boundaries were too broad after Package B. Locked resolution: split authorization providers, documents/comments, calendar/task/contact, Feishu approval instances, and data/admin capabilities into separate packages.
- R2 Legacy bypass scope was incomplete. Locked resolution: include direct comment-handler API calls, toolset/cache/direct dispatch, reaction callbacks, and old success-test inversion in Package A gates.
- R3 Readiness/deployment evidence was too weak. Locked resolution: unchecked legacy surfaces, denial audit failure, stale/unknown smoke, and schema incompatibility cannot satisfy readiness or deploy gates.
- R4 Approval terminology was ambiguous. Locked resolution: use `gateway_exec_approval_card` for Hermes confirmation cards and `feishu_approval_instance` for Feishu Approval objects.
- R5 Redaction scope was incomplete. Locked resolution: logs and ledger share the same redaction standard and require tests.

Propagation coverage:

- Terminology Contract, Non-negotiable Invariants, Capability Internalization Map, Package Sequence, Engineering Execution Model, Verification Matrix, Readiness and Deployment Gate, and Approval Boundary were updated against R1-R5.

Cold scan result:

- The master plan now contains no deliberate authorization shortcut, no raw OpenAPI runtime path, no package that opens P1-G behavior under Package A approval, and no remaining use of unqualified `approval` where the distinction affects workflow semantics.
