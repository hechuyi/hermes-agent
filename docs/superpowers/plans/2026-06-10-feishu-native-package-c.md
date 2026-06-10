# Feishu-native Package C Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Package C from `docs/plans/2026-06-08-feishu-native-hermes-master-plan.md`: authorization provider interfaces and broker policy for typed object authorization evidence, without opening document/calendar/task/approval/Base/Sheets/search/admin live business tools.

**Architecture:** Add an internal provider layer that converts provider-specific outcomes into typed `AuthorizationEvidence` plus sanitized provider decision metadata, then add a broker policy layer that issues one-time or short-session `ObjectCapabilityGrant` only from current, complete, object-scoped evidence. Keep card callback handles and object capability grants as separate authorities: existing brokered card action bindings may trigger policy checks, but they are never object authorization by themselves. Extend audit, readiness, smoke, and scope checks so provider availability, SDK reachability, app token presence, discovery, and user confirmation alone cannot be misread as object authority.

**Tech Stack:** Python, pytest, existing Hermes gateway contracts/ledger/tool registry.

---

## Overview

Package C builds the authorization provider and broker policy foundation needed by later Feishu object packages. It must not make document, comment, calendar, task, Feishu approval instance, Base, Sheets, search, admin, Drive, wiki, contact, generic OpenAPI, or cross-chat behavior live. The only positive runtime capability added by this package is internal grant issuance from typed evidence that is already scoped to a contract, object, action, authority subject, route snapshot, expiry, and policy version.

Existing Package A/B foundations are already present:

- `gateway/feishu_contracts.py` defines `AuthorizationEvidence`, `ObjectCapabilityGrant`, and `can_issue_object_grant`. Evidence kinds already include `verified_object_acl`, `user_delegated_credential`, `admin_policy_grant`, `app_owned_object`, `explicit_user_confirmation`, `system_test_object`, `app_token_only`, and `discovery_only`; states include `current`, `stale`, and `revoked`.
- `can_issue_object_grant` already denies missing scoped contract, route mismatch, stale or revoked contract/evidence, app-token-only evidence, discovery-only evidence, confirmation-alone P3 actions, and subject/object/scope mismatch.
- `gateway/feishu_legacy_guard.py` has an out-of-band `ContextVar`, but its `FeishuBrokerContext` currently carries card/action-like fields: `grant_handle`, `action_id`, `contract_hash`, and `route_partition_key`. Package C must not treat that existing handle as proof of object authority.
- `gateway/platforms/feishu.py` has brokered card action binding with opaque action/grant handles, route partition/snapshot hash, operator hash, contract hash, payload hash, expiry, and idempotency. These are interaction authority, not object access authority.
- `gateway/gateway_event_contract.py` and `gateway/gateway_event_ledger.py` already know generic authorization event names. Package C must make those events useful for provider/evidence/grant provenance without storing raw Feishu IDs, tokens, ACL bodies, document content, message bodies, or local paths.
- `gateway/feishu_readiness.py` and `gateway/feishu_smoke.py` have Package A/B readiness. Package C readiness does not exist.
- `toolsets.py`, `model_tools.py`, `tools/feishu_current_tool.py`, `tools/feishu_doc_tool.py`, and `tools/feishu_drive_tool.py` keep the model-visible `hermes-feishu` surface at the Package B allowlist, while doc/drive legacy tools remain separate and broker-gated.

## Non-goals

Package C does not implement Feishu document reads, document comments, Drive search, wiki search, calendar reads/writes, task reads/writes, Feishu approval instance reads/approvals/rejections, Base, Sheets, contact search, admin operations, raw OpenAPI passthrough, lark-cli runtime authority, NixOS changes, remote-host changes, deployment changes, model/provider configuration changes, or persistent grants.

Package C also does not define tenant-wide automation semantics. Any later live object behavior belongs to Package D-G and must receive its own plan, tests, review, and approval boundary.

## Preconditions

Package A/B contracts, broker guards, card action bindings, readiness primitives, and scope gates must already be on the working branch. The implementer starts from a clean worktree, stays on `fix/live-gateway-hermes-tools`, and must not revert unrelated user or peer changes.

Every task is TDD-first. Write the failing tests, run the focused `uv run --extra dev pytest ... -q` command to observe RED, implement the minimal green path, rerun focused tests, run `git diff --check`, review the task diff for Package C scope, then commit the task before expanding behavior.

Focused pytest commands that use `-k` must not be able to pass by silent deselection. New RED test functions or classes must contain the selector substrings used by the command, and the captured pytest output must prove that selected tests are non-zero. If `-q` output does not show a non-zero selected count or named selected tests, rerun the same selector with `-vv` or `--collect-only` before accepting the RED or GREEN result.

## File Structure

Expected Package C write-set:

- Create `gateway/feishu_authorization_providers.py`: provider protocol/request/result types, stable provider failure classes, fake providers, and system-test providers. This module contains no Feishu SDK calls unless a later task adds system-test fixtures behind fake/test-only interfaces.
- Create `gateway/feishu_broker_policy.py`: policy entrypoint that evaluates provider results and existing `ConversationContract`/`AuthorizationEvidence` rules, returns `ObjectCapabilityGrant` or stable denial, and keeps card action handles separate from object grants.
- Modify `gateway/feishu_contracts.py`: add only minimal contract helpers needed by providers and policy. Prefer adding wrapper types in the new modules over changing `AuthorizationEvidence` v1 hashing.
- Modify `gateway/gateway_event_contract.py`: add or tighten audit validators for provider decision, evidence observation/denial, capability grant/denial, and broker policy denial.
- Modify `gateway/gateway_event_ledger.py`: persist sanitized Package C audit events or projections needed by readiness; fail closed on schema incompatibility or raw sensitive fields.
- Modify `gateway/feishu_readiness.py`: add Package C provider/policy readiness and scope audit classifiers.
- Modify `gateway/feishu_smoke.py`: add smoke evidence classification only for provider/policy readiness fixtures, not live business object calls.
- Maybe modify `gateway/feishu_legacy_guard.py`: add a separate object capability context only if needed; do not overload the existing card callback `grant_handle`.
- Maybe modify tests: `tests/gateway/test_feishu_contracts.py`, `tests/gateway/test_gateway_event_ledger.py`, `tests/gateway/test_feishu_readiness.py`, `tests/gateway/test_feishu_package_b_scope.py`, `tests/gateway/test_feishu_legacy_guard.py`, `tests/gateway/test_feishu_brokered_cards.py`, and `tests/tools/test_feishu_tools.py`.
- Create tests:
  - `tests/gateway/test_feishu_authorization_providers.py`
  - `tests/gateway/test_feishu_broker_policy.py`
  - `tests/gateway/test_feishu_package_c_readiness.py`
  - `tests/gateway/test_feishu_package_c_scope.py`

## Non-negotiable Boundaries

- Provider availability, SDK reachability, app token availability, object discovery, and successful provider initialization are never grants.
- Provider output used for grants must include typed `AuthorizationEvidence` and sanitized provider decision metadata. The required metadata is provider id/version, policy version, issued/expires timestamps or freshness class, credential freshness, ACL completeness, provider reachability, unsupported scope status, revocation reason when revoked, and evidence source class.
- Preserve existing `AuthorizationEvidence` v1 hash stability. The default Package C design is to introduce `AuthorizationProviderDecision` and `AuthorizationProviderResult` wrappers in `gateway/feishu_authorization_providers.py`; do not add provider fields into the existing dataclass unless tests explicitly create a schema-versioned v2 migration with v1 compatibility assertions.
- Persistent grants remain out of scope. Grants are one-time or short-session only, carry expiry, route snapshot, policy version, evidence hashes, and object/action binding, and must fail closed when stale or reused outside policy.
- Card callback handles are not object capability grants. A card action may carry user confirmation evidence into policy, but the policy must still require object authority evidence for object actions that need it.
- No document, calendar, task, Feishu approval instance, Base, Sheets, search, admin, Drive, wiki, contact, raw OpenAPI, or lark-cli runtime tool is exposed by Package C.
- Every denied path must report a stable failure class and must not issue a grant, call a business SDK, mutate success state, or write a success ledger event.

## Failure Classes

Tests must assert stable failure classes for at least:

- `feishu_package_c_scope_creep`
- `feishu_provider_unsupported`
- `feishu_authorization_provider_missing`
- `feishu_authorization_evidence_missing`
- `feishu_authorization_evidence_stale`
- `feishu_authorization_evidence_revoked`
- `feishu_app_token_only_evidence`
- `feishu_discovery_only_evidence`
- `feishu_object_authority_scope_insufficient`
- `feishu_broker_policy_denied`
- `feishu_route_snapshot_mismatch`
- `feishu_object_ref_mismatch`
- `feishu_authority_subject_mismatch`
- `feishu_p3_requires_object_authority_evidence`
- `feishu_object_authority_evidence_missing`
- `feishu_contract_evidence_stale`
- `feishu_contract_evidence_revoked`
- `feishu_business_tool_surface_denied`
- `feishu_provider_unavailable`
- `feishu_provider_sdk_unreachable`
- `feishu_provider_app_token_unavailable`
- `feishu_provider_acl_incomplete`
- `feishu_provider_stale_credential`
- `feishu_provider_revoked_credential`
- `feishu_provider_unsupported_scope`

Use more specific subclasses where useful, but do not replace these package-level classes with ad hoc exception messages.

## Task C1: Provider Request, Result, and Decision Contracts

**Purpose:** Define the internal authorization provider interface and provider decision metadata without changing existing `AuthorizationEvidence` v1 hashes.

**Write-set:**
- Create: `gateway/feishu_authorization_providers.py`
- Modify: `gateway/feishu_contracts.py` only if helper exports are needed
- Test: `tests/gateway/test_feishu_authorization_providers.py`
- Maybe test: `tests/gateway/test_feishu_contracts.py`

**RED tests:**
- `AuthorizationProviderRequest` requires contract hash, route snapshot, authority subject ref, object ref, object type, action, requested scopes, policy version, and one-time/short-session semantics.
- `AuthorizationProviderDecision` records provider id/version, evidence source class, reachability state, issued/expires or freshness class, credential freshness, ACL completeness, unsupported scope, revocation reason, denial failure class, and sanitized decision hash.
- Provider result is either `evidence: AuthorizationEvidence` plus current decision metadata, or a stable denial. It is never a bare boolean and never a raw SDK response.
- `AuthorizationEvidence` v1 hash remains unchanged for existing fixtures when Package C wrappers are added.
- Raw tokens, raw Feishu object IDs, raw ACL response bodies, raw document content, raw message content, and raw local paths are rejected by provider decision hashing.

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_authorization_providers.py tests/gateway/test_feishu_contracts.py -q -k "provider_contract or evidence_hash_stability or provider_decision"
```

Expected RED: missing module/types or missing hash-stability assertions.

**GREEN implementation constraints:**
- Add `AuthorizationProviderRequest`, `AuthorizationProviderDecision`, `AuthorizationProviderResult`, `AuthorizationProviderProtocol`, and `AuthorizationProviderError` in `gateway/feishu_authorization_providers.py`.
- Reuse `AuthorizationEvidence`, `HashedRef`, and `feishu_contract_hash`; do not duplicate canonical hashing.
- Keep provider decision metadata sanitized and hashable. Use provider id/version strings, refs/hashes/classes, booleans, timestamps rounded to stable test values, and failure classes.
- Keep `AuthorizationEvidence` v1 unchanged unless a v2 migration is deliberately tested; wrapper metadata is the preferred Package C provenance carrier.

**Verification commands:**

```bash
uv run --extra dev pytest tests/gateway/test_feishu_authorization_providers.py tests/gateway/test_feishu_contracts.py -q -k "provider_contract or evidence_hash_stability or provider_decision"
git diff --check
git diff --name-only
```

Before committing, compare `git diff --name-only` against the C1 write-set and explain any extra file.

**Commit message suggestion:** `feat(feishu): add authorization provider contracts`

**Review focus:** Confirm provider metadata proves provenance without destabilizing existing evidence hashes or carrying raw Feishu data.

**Explicit don't-do list:** Do not call Feishu SDKs; do not add live OAuth flows; do not expose provider results to the model; do not store raw provider responses.

## Task C2: Fake and System-test Authorization Providers

**Purpose:** Provide deterministic fake/system-test providers that exercise positive and negative broker policy decisions without granting authority from availability alone.

**Write-set:**
- Modify: `gateway/feishu_authorization_providers.py`
- Test: `tests/gateway/test_feishu_authorization_providers.py`

**RED tests:**
- Fake `user_delegated_credential` provider returns current evidence only when authority subject, object, route snapshot, and requested scope match.
- Fake `verified_object_acl` provider denies incomplete ACL responses with `feishu_provider_acl_incomplete`.
- Fake `admin_policy_grant` provider denies unsupported scopes with `feishu_provider_unsupported_scope`.
- Fake `app_owned_object` provider grants only app-owned object evidence and cannot grant user-owned objects.
- Fake `explicit_user_confirmation` provider produces confirmation evidence but cannot satisfy object authority by itself for P3 object actions.
- Fake `system_test_object` provider can produce current test evidence only for system-test object refs and test-only provider ids.
- Availability-only, SDK-reachable-only, app-token-only, and discovery-only providers return typed evidence/denial that broker policy must deny.
- Provider unavailable, SDK unreachable, app token unavailable, stale credential, revoked credential, unsupported provider, and unsupported scope all fail closed with stable failure classes.

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_authorization_providers.py -q -k "fake_provider or system_test_provider or provider_failure"
```

Expected RED: fake providers and failure states do not exist.

**GREEN implementation constraints:**
- Implement deterministic fake providers and system-test providers in the provider module or a clearly marked test fixture subsection of that module.
- Do not import Feishu SDK clients. System-test providers may simulate SDK states but must not use lark-cli or OpenAPI runtime authority.
- Provider positive cases must return `AuthorizationProviderResult` with typed `AuthorizationEvidence`, current provider decision metadata, source class, and sanitized decision hash.
- Provider negative cases return denial results, not exceptions, unless construction input is invalid. Exceptions must carry stable failure classes.
- Every provider result must declare whether it is object-authority evidence, confirmation evidence, app-token-only evidence, discovery-only evidence, or non-grantable provider state.

**Verification commands:**

```bash
uv run --extra dev pytest tests/gateway/test_feishu_authorization_providers.py -q
git diff --check
git diff --name-only
```

Before committing, compare `git diff --name-only` against the C2 write-set and explain any extra file.

**Commit message suggestion:** `test(feishu): add fake authorization providers`

**Review focus:** Verify that fake providers cannot accidentally model app token reachability, discovery, or confirmation as object authority.

**Explicit don't-do list:** Do not add document/calendar/task/Base/Sheets/search/admin clients; do not add raw OpenAPI passthrough; do not grant from provider availability.

## Task C3: Broker Policy Grant Issuance API

**Purpose:** Add the internal broker policy API that converts provider evidence into one-time or short-session `ObjectCapabilityGrant` or a stable denial.

**Write-set:**
- Create: `gateway/feishu_broker_policy.py`
- Modify: `gateway/feishu_contracts.py` only for minimal helper exports if needed
- Test: `tests/gateway/test_feishu_broker_policy.py`

**RED tests:**
- Missing provider registry returns `feishu_authorization_provider_missing`.
- Missing evidence returns `feishu_authorization_evidence_missing`.
- Provider unavailable, SDK unreachable, app token unavailable, stale credential, revoked credential, incomplete ACL, unsupported provider, and unsupported scope are denied before grant issuance.
- App-token-only evidence is denied with `feishu_app_token_only_evidence`.
- Discovery-only evidence is denied with `feishu_discovery_only_evidence`.
- Confirmation-only P3 object action is denied with `feishu_p3_requires_object_authority_evidence` or `feishu_object_authority_evidence_missing` and does not issue a grant.
- Wrong authority subject, wrong object ref, wrong route snapshot, stale evidence, revoked evidence, and insufficient scope are denied using `feishu_authority_subject_mismatch`, `feishu_object_ref_mismatch`, `feishu_route_snapshot_mismatch`, `feishu_contract_evidence_stale`, `feishu_contract_evidence_revoked`, and `feishu_object_authority_scope_insufficient` where applicable.
- Valid object-authority evidence returns an issued grant wrapper with contract hash, evidence hashes, object type, object ref, action, authority subject ref, policy version, expiry, one-time/short-session semantics, and grant hash.
- Reusing a one-time grant request id or changing payload hash/route/object/action after decision returns `feishu_broker_policy_denied`.
- If the broker normalizes any of the stable contract or provider denial classes into `feishu_broker_policy_denied`, `BrokerPolicyDecision` and denial audit events must still preserve the original class as a separate `denial_reason_class`.

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_broker_policy.py tests/gateway/test_feishu_contracts.py -q -k "grant or broker_policy or provider_denial"
```

Expected RED: policy module and grant issuance API do not exist.

**GREEN implementation constraints:**
- Provide a single public entrypoint such as `issue_object_capability_grant(request, registry, *, now, policy_version) -> BrokerPolicyDecision`.
- `BrokerPolicyDecision` must contain either `grant: BrokerPolicyGrant` or `failure_class`; it must never return a grant alongside denial. If the public denial class is normalized, include `denial_reason_class` for the underlying stable reason.
- The preferred Package C design is a new wrapper in `gateway/feishu_broker_policy.py`, named `BrokerPolicyGrant` or `IssuedObjectCapabilityGrant`. The wrapper must contain the existing `ObjectCapabilityGrant` plus Package C issuance metadata: `expires_at`, `route_snapshot_hash`, `policy_version`, `grant_semantics`, `request_id_hash`, `payload_hash`, and sanitized replay metadata sufficient to detect one-time reuse without storing raw request payloads.
- Do not require new expiry, route snapshot, policy version, or one-time semantics fields on the current `ObjectCapabilityGrant` to make C3 executable. A schema-versioned `ObjectCapabilityGrant` v2 is allowed only as an explicitly tested alternative with v1 compatibility/hash-stability assertions.
- Use `can_issue_object_grant` for core evidence checks. Add Package C checks around provider decision completeness, provider freshness, expiry, one-time/short-session semantics, and policy version.
- Keep persistent grants out of scope. If a ledger projection is needed for one-time replay checks, store only sanitized hashes and expiry.
- Do not accept a `FeishuBrokerContext.grant_handle` or card action id as object authority. If a callback supplies explicit confirmation, convert it into typed `explicit_user_confirmation` evidence and still require object authority evidence for object actions that need it.

**Verification commands:**

```bash
uv run --extra dev pytest tests/gateway/test_feishu_broker_policy.py tests/gateway/test_feishu_contracts.py -q
git diff --check
git diff --name-only
```

Before committing, compare `git diff --name-only` against the C3 write-set and explain any extra file.

**Commit message suggestion:** `feat(feishu): issue object grants through broker policy`

**Review focus:** Confirm all grant positives are object/action/subject/route/scope bound and all denial paths fail before capability issuance.

**Explicit don't-do list:** Do not expose policy as a model-visible tool; do not add persistent grant storage; do not infer object access from card handles, discovery, app token, or SDK reachability.

## Task C4: Separate Object Capability Context from Card Action Context

**Purpose:** Prevent the existing brokered card/action handle context from being conflated with object capability authority.

**Write-set:**
- Maybe modify: `gateway/feishu_legacy_guard.py`
- Modify: `gateway/feishu_broker_policy.py`
- Maybe modify: `gateway/platforms/feishu.py` only if tests show callback handoff needs explicit confirmation evidence plumbing
- Test: `tests/gateway/test_feishu_broker_policy.py`
- Test: `tests/gateway/test_feishu_legacy_guard.py`
- Test: `tests/gateway/test_feishu_brokered_cards.py`

**RED tests:**
- Existing `feishu_broker_context(...)` with a valid card/action handle does not satisfy object grant checks.
- User/model JSON keys such as `_feishu_broker_grant`, `grant_handle`, `action_id`, or object-looking handles do not create provider evidence.
- A brokered confirmation card callback may produce `explicit_user_confirmation` evidence only when operator, route, contract hash, payload hash, expiry, and idempotency match.
- The confirmation evidence is denied for P3 object actions unless combined with current object authority evidence from a provider.
- Legacy doc/drive/comment tools remain denied without proper broker context and do not become live through the new object capability types.

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_broker_policy.py tests/gateway/test_feishu_legacy_guard.py tests/gateway/test_feishu_brokered_cards.py -q -k "object_capability_context or card_context or confirmation"
```

Expected RED: existing contexts are not separated or confirmation evidence is not represented.

**GREEN implementation constraints:**
- If a new context is needed, add `FeishuObjectCapabilityContext` with fields derived from `ObjectCapabilityGrant`, not card handles. Store it in a separate `ContextVar`.
- Keep the existing legacy guard API compatible for Package A/B tests.
- Do not let object capability context be created from user/model kwargs. It must be created only by broker policy code after grant issuance.
- Treat callback confirmation as one evidence kind among many, not as a universal grant.

**Verification commands:**

```bash
uv run --extra dev pytest tests/gateway/test_feishu_broker_policy.py tests/gateway/test_feishu_legacy_guard.py tests/gateway/test_feishu_brokered_cards.py -q
git diff --check
git diff --name-only
```

Before committing, compare `git diff --name-only` against the C4 write-set and explain any extra file.

**Commit message suggestion:** `fix(feishu): separate card and object grant authority`

**Review focus:** Confirm a worker cannot use the old card callback `grant_handle` to authorize object reads/writes.

**Explicit don't-do list:** Do not relax legacy doc/drive/comment gates; do not add model-visible object tools; do not serialize object grants into card payloads.

## Task C5: Provider, Evidence, Grant, and Denial Audit Events

**Purpose:** Make Package C decisions auditable with sanitized provenance and fail-closed schema validation.

**Write-set:**
- Modify: `gateway/gateway_event_contract.py`
- Modify: `gateway/gateway_event_ledger.py`
- Modify: `gateway/feishu_broker_policy.py` if policy writes or returns audit payloads
- Test: `tests/gateway/test_gateway_event_ledger.py`
- Test: `tests/gateway/test_feishu_broker_policy.py`

**RED tests:**
- Audit validators accept sanitized `feishu_authorization_provider_decision`, `feishu_authorization_evidence_observed`, `feishu_authorization_evidence_denied`, `feishu_auth_decision`, `feishu_capability_granted`, `feishu_capability_denied`, and `feishu_broker_policy_denied` events.
- Provider decision events require provider id/version, evidence source class, provider reachability class, credential freshness class, ACL completeness class, unsupported scope status, revocation reason when applicable, decision hash, contract hash, route snapshot hash, object ref hash, authority subject hash, policy version, and failure class for denials.
- Capability grant events require grant hash, evidence hashes, contract hash, object type, object ref hash, action, authority subject hash, expiry, one-time/short-session class, and policy version.
- Denial events require stable failure class and sanitized denial reason class.
- Raw token, raw app token, raw user token, raw Feishu open ID/user ID/chat ID/document token/file token/comment ID, raw ACL JSON, raw OpenAPI body, raw document content, raw message body, and raw local path fields are rejected.
- Ledger schema incompatibility, output truncation, missing event hash, unknown provider state, or unknown policy decision state fails closed and cannot be counted as readiness success.

Run:

```bash
uv run --extra dev pytest tests/gateway/test_gateway_event_ledger.py tests/gateway/test_feishu_broker_policy.py -q -k "authorization_provider or capability_granted or broker_policy_denied or raw"
```

Expected RED: audit event validators are too generic for Package C provenance.

**GREEN implementation constraints:**
- Extend existing event names where they already exist; add only the minimum new event type needed for provider decision provenance.
- Keep event payloads sanitized, append-only, and schema-versioned.
- Ledger write failure must return failed/unknown/not-ready evidence and must not let broker policy report success if audit is required for that path.
- Store counts, hashes, classes, and refs; do not store raw provider data.

**Verification commands:**

```bash
uv run --extra dev pytest tests/gateway/test_gateway_event_ledger.py tests/gateway/test_feishu_broker_policy.py -q
git diff --check
git diff --name-only
```

Before committing, compare `git diff --name-only` against the C5 write-set and explain any extra file.

**Commit message suggestion:** `feat(feishu): audit authorization provider decisions`

**Review focus:** Verify provenance is sufficient to explain grant/denial decisions without leaking sensitive Feishu material.

**Explicit don't-do list:** Do not dump SDK responses; do not write success audit on denied policy; do not silently skip audit failures.

## Task C6: Package C Readiness, Smoke, and Scope Audit

**Purpose:** Add readiness/smoke classifiers that prove provider and broker policy foundations are present while Package D-G live business tools remain unavailable.

**Write-set:**
- Modify: `gateway/feishu_readiness.py`
- Modify: `gateway/feishu_smoke.py`
- Maybe modify: `model_tools.py`, `toolsets.py`, or `tools/feishu_current_tool.py` only to add stricter Package C scope audit checks; do not add business tools
- Test: `tests/gateway/test_feishu_package_c_readiness.py`
- Test: `tests/gateway/test_feishu_package_c_scope.py`
- Maybe test: `tests/gateway/test_feishu_package_b_scope.py`
- Maybe test: `tests/tools/test_feishu_tools.py`

**RED tests:**
- Readiness fails when provider registry is missing with `feishu_authorization_provider_missing`.
- Readiness fails when required provider categories are absent: user-delegated credential, verified object ACL, admin policy grant, app-owned object, system-test object, and explicit user confirmation.
- Readiness fails when provider decision audit is missing, stale, incomplete, revoked, unsupported, unreachable, or based only on app token/discovery.
- Readiness fails when broker policy cannot produce a valid grant from fake object-authority evidence or cannot deny app-token-only/discovery-only/confirmation-only evidence.
- Smoke classifies fake/system-test provider fixtures without invoking live document/calendar/task/approval/Base/Sheets/search/admin APIs.
- Scope audit fails with `feishu_package_c_scope_creep` or `feishu_business_tool_surface_denied` when any denied Feishu business identifier appears as model-visible or adapter-invoked success behavior.
- Package B allowed current-conversation identifiers remain allowed and unchanged.

Run:

```bash
uv run --extra dev pytest tests/gateway/test_feishu_package_c_readiness.py tests/gateway/test_feishu_package_c_scope.py tests/gateway/test_feishu_package_b_scope.py tests/tools/test_feishu_tools.py -q
```

Expected RED: Package C readiness/scope classifiers do not exist.

**GREEN implementation constraints:**
- Add exact Package C provider category requirements; do not use broad labels as test oracles.
- Model-visible `hermes-feishu` surface must remain the Package B allowlist. Internal provider/policy identifiers may be adapter-only or readiness-only, never model-visible tools.
- Unknown Feishu identifiers fail closed until classified as Package B allowed, Package C internal, or denied business surface.
- Scope readiness must inspect tool registry, model tool definitions, quiet-mode cache, and any Feishu toolset aliases already covered by Package A/B tests.

**Verification commands:**

```bash
uv run --extra dev pytest tests/gateway/test_feishu_package_c_readiness.py tests/gateway/test_feishu_package_c_scope.py tests/gateway/test_feishu_package_b_scope.py tests/tools/test_feishu_tools.py -q
git diff --check
git diff --name-only
```

Before committing, compare `git diff --name-only` against the C6 write-set and explain any extra file.

**Commit message suggestion:** `feat(feishu): add package c readiness gates`

**Review focus:** Confirm readiness proves provider/policy foundations but does not certify any live business object tool.

**Explicit don't-do list:** Do not register document/calendar/task/approval/Base/Sheets/search/admin tools; do not run lark-cli; do not change NixOS, remote host, model, provider, or deployment configuration.

## Task C7: Package Verification and Scope Audit

**Purpose:** Verify Package C as a complete authorization-provider and broker-policy increment and prove it does not expose Package D-G business tools.

**Write-set:**
- No production changes unless verification exposes a defect.
- Test or docs changes only if a verification finding proves the implementation or plan is incomplete.

**RED tests:** Not applicable as a feature task. Any verification failure becomes a new focused RED test in C1-C6 before a fix is implemented.

**GREEN implementation constraints:**
- If verification exposes a defect, return to the smallest responsible task, add a failing test, implement the fix, rerun focused and package commands, then commit.
- Do not batch unrelated cleanup into the verification commit.
- Do not use verification failure as a reason to enable Package D-G behavior.

**Verification commands:**

Package C focused run:

```bash
uv run --extra dev pytest \
  tests/gateway/test_feishu_authorization_providers.py \
  tests/gateway/test_feishu_broker_policy.py \
  tests/gateway/test_feishu_package_c_readiness.py \
  tests/gateway/test_feishu_package_c_scope.py \
  -q
```

Package A-C regression subset:

```bash
uv run --extra dev pytest \
  tests/gateway/test_feishu_contracts.py \
  tests/gateway/test_feishu_action_plan.py \
  tests/gateway/test_feishu_readiness.py \
  tests/gateway/test_feishu_smoke.py \
  tests/gateway/test_feishu_legacy_guard.py \
  tests/gateway/test_gateway_event_ledger.py \
  tests/gateway/test_feishu_brokered_cards.py \
  tests/gateway/test_feishu_package_b_scope.py \
  tests/gateway/test_feishu_authorization_providers.py \
  tests/gateway/test_feishu_broker_policy.py \
  tests/gateway/test_feishu_package_c_readiness.py \
  tests/gateway/test_feishu_package_c_scope.py \
  tests/tools/test_feishu_tools.py \
  -q
```

Scope audit commands:

```bash
BASE=$(git merge-base HEAD origin/fix/live-gateway-hermes-tools)
git diff --name-only "$BASE"..HEAD
git diff "$BASE"..HEAD -- gateway tools tests/gateway tests/tools docs/superpowers/plans model_tools.py toolsets.py
rg -n "feishu_doc_read|feishu_drive_|feishu\\.doc\\.|feishu\\.docs\\.|feishu\\.document\\.|feishu\\.drive\\.|feishu\\.wiki\\.|feishu\\.calendar\\.|feishu\\.task\\.|feishu\\.approval\\.|feishu\\.base\\.|feishu\\.sheets\\.|feishu\\.search\\.|feishu\\.contact\\.|feishu\\.admin\\.|feishu\\.openapi\\.|feishu\\.file\\.export\\.|feishu\\.cross_chat\\.|feishu\\.comment\\.|feishu\\.descriptor\\.|feishu\\.status_card\\.|feishu\\.generic_card\\.|feishu\\.reaction\\." gateway tools tests/gateway tests/tools docs/superpowers/plans model_tools.py toolsets.py
rg -n "OpenAPI|lark-cli|NixOS|remote|persistent grant|calendar|task|approval|Base|Sheets|search|admin" gateway tools tests/gateway tests/tools docs/superpowers/plans model_tools.py toolsets.py
```

Expected: Package C files show provider interfaces, fake/system-test providers, broker policy, audit/readiness/scope checks, and negative tests. Out-of-scope strings may appear only in non-goals, deny lists, scope audit assertions, failure classes, legacy guard tests, or package plan text. If `git merge-base` cannot identify an upstream base, stop and require an explicit base commit.

C7 scope audit must classify every match as one of:

- allowed denial/docs match: non-goal documentation, explicit denial test, legacy guard assertion, readiness/scope-audit finding, provider negative fixture, or sanitized failure-class string.
- unexpected live enablement match: production registration, model-visible schema, SDK call construction, lark-cli runtime dependency, raw OpenAPI dispatch, success-path test, permissive fixture, or adapter success branch for Package D-G behavior.

Any unexpected live enablement match is a verification failure and must be routed back to the smallest responsible C1-C6 task before Package C is marked complete.

**Commit message suggestion:** `test(feishu): verify package c authorization scope`

**Review focus:** Confirm Package C can be described as authorization-provider and broker-policy foundations only.

**Explicit don't-do list:** Do not update deployment, remote host, NixOS, model/provider config, or broad Feishu tools; do not introduce persistent grants.

## Scope Audit Terms

Package C scope tests and final audit must include the following terms exactly:

```text
feishu_doc_read
feishu_drive_
feishu.doc.
feishu.docs.
feishu.document.
feishu.drive.
feishu.wiki.
feishu.calendar.
feishu.task.
feishu.approval.
feishu.base.
feishu.sheets.
feishu.search.
feishu.contact.
feishu.admin.
feishu.openapi.
feishu.file.export.
feishu.cross_chat.
feishu.comment.
feishu.descriptor.
feishu.status_card.
feishu.generic_card.
feishu.reaction.
```

The only acceptable matches for these terms during Package C are deny lists, negative tests, legacy guard tests, readiness/scope-audit findings, and documentation. Any model-visible registration, successful adapter path, SDK request construction, or permissive fixture for these terms is scope creep.

## Review Requirements

Each task must receive review before the next task expands behavior. The reviewer context should include this plan path, the master plan path, the focused diff, the focused test output, and the scope audit classification for the task. Reviewers should check:

- TDD order was followed and the RED failure was meaningful.
- Provider result semantics do not grant from availability, app token, discovery, or confirmation alone.
- `AuthorizationEvidence` v1 hash stability was preserved unless a tested v2 migration was intentionally introduced.
- Broker policy issues object grants only from current, complete, object-authority evidence with route/object/action/subject/scope binding.
- Card action handles and object capability grants remain separate.
- Audit/readiness evidence is sanitized and traceable.
- No Package D-G live tool, SDK call path, or model-visible schema was opened.

## Before Moving to Implementation/User Review

The controller must collect and attach:

- [ ] Plan update: confirm this Package C plan is the implementation source and list any deviations.
- [ ] Expected tests and failure classes: include all Package C focused tests and every required failure class from this plan.
- [ ] Spec/code quality review: include reviewer outcome and any resolved findings.
- [ ] Verification summary: include focused commands, Package A-C regression subset, `git diff --check`, and scope audit classification.
- [ ] Commit list: include one commit per completed task or justified grouping.
- [ ] Deferred capabilities: explicitly state that document/comment/calendar/task/Feishu approval instance/Base/Sheets/search/admin/Drive/wiki/contact/raw OpenAPI/lark-cli runtime authority/persistent grants remain deferred.
