# Package C User Review Evidence Packet

本证据包用于 Package C 本地实现审查。审查对象是分支 `fix/live-gateway-hermes-tools` 上从基线 `810af0330f325b1ec567df5f9c824fc37117c7d5` 到 HEAD `875a1a6f34377a8305dac74f893e042fc80fe4e2` 的 Feishu-native Package C 增量。Package C 的实现来源是 `docs/superpowers/plans/2026-06-10-feishu-native-package-c.md`，目标限定为 authorization provider interfaces、typed object authorization evidence、broker policy、sanitized audit/readiness/scope gates；不把 document、calendar、task、approval、Base、Sheets、search、admin 等 live business tools 暴露给模型或适配器成功路径。

## Plan Source and Deviations

`docs/superpowers/plans/2026-06-10-feishu-native-package-c.md` 是 Package C 的 implementation source。实现按 C1-C7 分段推进：C1/C2 建立 provider request/result/decision contracts 与 deterministic fake/system-test providers；C3/C4 建立 broker policy grant issuance 并把 card action authority 与 object capability authority 分离；C5 建立 provider/evidence/grant/denial audit events；C6 建立 readiness、smoke 与 scope audit gates；C7 执行 package verification 和 scope audit。

已知偏差均仍在 Package C 边界内。C5/C6 之后存在多轮 hardening commit，覆盖 provider typed validation、broker provider decision invariants、audit class domain closure、revoked provider audit reason、readiness escape closure 等额外收敛项。这些修复没有扩大 live Feishu business surface，而是把 plan 中的 fail-closed、stable failure class、sanitized provenance、contract closure 和 scope audit 要求表达得更严格。C7 验证阶段发现的问题被回流到 C3-C6 的最小责任边界修复；没有引入部署、NixOS、remote host、model-provider config、persistent grant 或 business SDK enablement。

## Expected Tests and Failure Classes

Package C focused tests 覆盖以下集合：`tests/gateway/test_feishu_authorization_providers.py`、`tests/gateway/test_feishu_broker_policy.py`、`tests/gateway/test_feishu_package_c_readiness.py`、`tests/gateway/test_feishu_package_c_scope.py`。C5 审计路径还要求 `tests/gateway/test_gateway_event_ledger.py` 与 broker policy tests 覆盖 provider decision、evidence observed/denied、capability granted/denied、broker policy denied 等 event payload schema。C6 边界路径还要求 `tests/gateway/test_feishu_package_b_scope.py` 与 `tests/tools/test_feishu_tools.py` 证明 Package B allowed current-conversation surface 未被 Package C 改写或扩大。A-C regression subset 覆盖 contracts、action plan、readiness、smoke、legacy guard、event ledger、brokered cards、Package B scope、Package C provider/policy/readiness/scope 和 Feishu tools。C7 scope audit 覆盖 diff、model tool definitions、tool registry、quiet-mode cache、toolset aliases、Feishu deny-list terms 与 out-of-scope text classification。

预期 stable failure classes/category families 包括：`feishu_authorization_provider_missing`、provider category missing、provider unavailable、provider SDK unreachable、provider app token unavailable、provider unsupported、provider unsupported scope、provider ACL incomplete、provider stale credential、provider revoked credential、authorization evidence missing/stale/revoked、contract evidence stale/revoked、app-token-only evidence、discovery-only evidence、explicit confirmation only、P3 requires object authority evidence、object authority evidence missing、object authority scope insufficient、route snapshot mismatch、object ref mismatch、authority subject mismatch、broker policy denied、broker replay invalid、grant semantics invalid、scope creep、business tool surface denied、audit incomplete、source denied、raw marker rejected、schema invalid、unknown provider state、unknown policy decision state、ledger schema incompatible、ledger output truncated、audit event hash missing、readiness stale、readiness revoked、readiness unsupported、readiness unreachable、business denied。关键要求不是穷尽每个断言，而是所有 denial 必须以稳定 failure class 或可追溯类别呈现，且不能降级为 success readiness、grant issuance 或 live business behavior。

## Spec and Code Quality Review Outcomes

C5 审查结果：provider decision、evidence observation/denial、capability grant/denial、broker policy denial 的 audit payload 已收敛到 sanitized、schema-versioned、append-only evidence。已解决的主要 finding 类别包括 raw token/object id/ACL body/document/message/local path marker rejection、provider denial reason 缺失、broker audit event output validation、semantic class domain 过宽、revoked provider audit reason 缺失，以及 denial public class 与 underlying reason class 不可追溯的问题。

C6 审查结果：readiness、smoke 和 scope audit gates 证明的是 Package C provider/policy foundation，而不是 live Feishu business capability。已解决的主要 finding 类别包括 provider registry/category 缺失未 fail-closed、provider decision audit stale/incomplete/revoked/unsupported/unreachable 未稳定分类、app-token/discovery/confirmation-only 被误读为 authority 的风险、Package B current-conversation allowlist 被 Package C scope audit 误伤，以及 model-visible `hermes-feishu` surface 被扩大的风险。

C7 和整包最终审查结果：最终状态保持 card callback handles 与 object grants 分离，object grant 只从 current、complete、object-scoped typed evidence 派生，并绑定 contract hash、route snapshot、object ref、action、authority subject、scope、policy version、expiry 和 one-time/short-session semantics。多轮 hardening 已关闭 provider typed result、broker route boundary、decision invariant、classifier sanitization、replay semantics、audit class domain、readiness escape 等 finding 类别。没有记录 Package C 范围内未解决的 spec/code quality blocker；剩余限制列在本文件的 residual risks。

## Verification Summary

审查 HEAD：`875a1a6f34377a8305dac74f893e042fc80fe4e2`。Package C review merge-base/baseline：`810af0330f325b1ec567df5f9c824fc37117c7d5`，已确认是当前 HEAD 的祖先。当前分支状态在生成本证据包前为 `fix/live-gateway-hermes-tools...origin/fix/live-gateway-hermes-tools [ahead 103]` 且工作树干净。

Package C focused verification：`uv run --extra dev pytest tests/gateway/test_feishu_authorization_providers.py tests/gateway/test_feishu_broker_policy.py tests/gateway/test_feishu_package_c_readiness.py tests/gateway/test_feishu_package_c_scope.py -q`，结果 `246 passed`。

Package A-C regression subset：`uv run --extra dev pytest tests/gateway/test_feishu_contracts.py tests/gateway/test_feishu_action_plan.py tests/gateway/test_feishu_readiness.py tests/gateway/test_feishu_smoke.py tests/gateway/test_feishu_legacy_guard.py tests/gateway/test_gateway_event_ledger.py tests/gateway/test_feishu_brokered_cards.py tests/gateway/test_feishu_package_b_scope.py tests/gateway/test_feishu_authorization_providers.py tests/gateway/test_feishu_broker_policy.py tests/gateway/test_feishu_package_c_readiness.py tests/gateway/test_feishu_package_c_scope.py tests/tools/test_feishu_tools.py -q`，结果 `789 passed, 2 warnings`。

C7 scope audit：PASS。审计分类为 allowed denial/docs match、legacy guard assertion、readiness/scope-audit finding、provider negative fixture、sanitized failure-class string 或 Package C internal provider/policy identifier；未发现 unexpected live enablement match。`git diff --check` 在 Package C verification 中通过；本次证据包创建时 targeted `ruff check` 对 provider/policy/readiness/scope/test touched Python files 返回 `All checks passed!`。本证据包创建时重新运行了 `git diff --check`、`git status --short --branch`，并用 `rg -n "Package C User Review|875a1a6f|Deferred" docs/superpowers/plans/2026-06-10-feishu-native-package-c-review-packet.md` 定位关键锚点。

## Commit List

Plan and Package C source:

| Area | Commit | Message |
| --- | --- | --- |
| Package C plan | `adcfd2c58` | `docs(feishu): plan package c authorization providers` |
| Package C plan hardening | `f352f47a0` | `docs(feishu): tighten package c execution plan` |

C1 provider contracts:

| Area | Commit | Message |
| --- | --- | --- |
| C1 | `94d340a26` | `feat(feishu): add authorization provider contracts` |
| C1 hardening | `14363a7cd` | `fix(feishu): reject raw provider decision values` |
| C1 hardening | `f83b45754` | `fix(feishu): fail closed on provider metadata strings` |
| C1 hardening | `c4757c5c3` | `fix(feishu): harden provider decision contracts` |

C2 fake/system-test providers:

| Area | Commit | Message |
| --- | --- | --- |
| C2 | `f8dc6b025` | `test(feishu): add fake authorization providers` |
| C2 hardening | `791d6c16e` | `fix(feishu): gate system test fake evidence` |
| C2 hardening | `568062dba` | `fix(feishu): stabilize provider failure classes` |

C3/C4 broker policy and authority separation:

| Area | Commit | Message |
| --- | --- | --- |
| C3 | `5ad101e3e` | `feat(feishu): issue object grants through broker policy` |
| C3 hardening | `2a26ece5d` | `fix(feishu): harden broker policy grant issuance` |
| C3 hardening | `c6195a8f2` | `fix(feishu): sanitize broker policy classifiers` |
| C3 hardening | `f929fb9c8` | `fix(feishu): bind broker replay grant semantics` |
| C3 hardening | `41377c8f6` | `fix(feishu): reject sensitive broker classifiers` |
| C3 hardening | `2a4aa0757` | `fix(feishu): require typed authorization provider results` |
| C3 hardening | `9587af643` | `fix(feishu): harden broker route and decision boundaries` |
| C3 hardening | `3439a105c` | `fix(feishu): validate broker provider decision invariants` |
| C4 | `179229d1b` | `fix(feishu): separate card and object grant authority` |

C5 audit events:

| Area | Commit | Message |
| --- | --- | --- |
| C5 | `ce5a64a45` | `feat(feishu): audit authorization provider decisions` |
| C5 hardening | `7e717a952` | `fix(feishu): audit provider denials safely` |
| C5 hardening | `e2bdd2d71` | `fix(feishu): validate broker audit event output` |
| C5 hardening | `c26870bca` | `fix(feishu): require provider audit denial reasons` |
| C5 hardening | `5b058c802` | `fix(feishu): validate broker audit semantic classes` |
| C5 hardening | `b213b9a58` | `fix(feishu): constrain broker audit class domains` |
| C5 hardening | `7fe30d6f7` | `fix(feishu): close broker audit class domains` |
| C5 hardening | `bc7758bab` | `fix(feishu): require revoked provider audit reason` |

C6/C7 readiness, scope audit, and final closure:

| Area | Commit | Message |
| --- | --- | --- |
| C6 | `a8068136a` | `feat(feishu): add package c readiness gates` |
| C6/C7 hardening | `7deb966c9` | `fix(feishu): harden package c readiness gates` |
| C7 final closure | `875a1a6f3` | `fix(feishu): close package c readiness escapes` |

## Deferred Capabilities

Document reads/comments, calendar, task, Feishu approval instance, Base, Sheets, search, admin, Drive, wiki, contact, raw OpenAPI passthrough, lark-cli runtime authority, persistent grants, live Feishu business tools, deploy changes, NixOS changes, remote-host changes, and model-provider config changes remain deferred/not changed。Package C does not claim real deployment completion, live Feishu integration availability, tenant-wide automation semantics, persistent authorization, or production business-object access.

## Residual Risks

全仓库测试未运行；验证证据限于 Package C focused run、Package A-C regression subset、scope audit、diff-check 与 targeted ruff evidence。未运行 live Feishu integration；fake/system-test providers 只证明 typed provider/policy semantics，不证明真实 Feishu document/calendar/task/approval/Base/Sheets/search/admin 业务能力。分支未 push，生成证据包前本地状态为 ahead 103。legacy doc/drive SDK code 仍存在，但 broker-gated 且位于 `hermes-feishu` model-visible Package B allowlist 之外；Package C 没有把这些 legacy code path 转为 live business tools。
