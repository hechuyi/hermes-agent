# Official main intake audit

> Date: 2026-06-11
> Working branch: `fix/live-gateway-hermes-tools`
> Local fork main: `origin/main` at `5921d667855880b0aa2083a50f001748aed52f3e`
> Official main ref: `upstream/main` at `93a2f680fd18f08f70eaf5c96944cdf4dc477143`
> Deployed fork head at intake start: `38144944bde7fd5e16051a4e8f739b9e417121ae`

## Scope

This audit covers official `NousResearch/hermes-agent` commits after the
previous reviewed baseline `5921d667855880b0aa2083a50f001748aed52f3e`.

The production target remains the Backend1 Feishu-specialized Hermes fork.
Changes are therefore classified by whether they improve production safety,
runtime correctness, or Feishu/live-gateway behavior without weakening the
fork's existing boundaries.

## Baseline

Commands used to establish the baseline:

```bash
git fetch --no-tags https://github.com/NousResearch/hermes-agent.git main
git update-ref refs/remotes/upstream/main FETCH_HEAD
git rev-list --count origin/main..upstream/main
git rev-list --count HEAD..upstream/main
git rev-list --count --cherry-pick --right-only HEAD...upstream/main
```

Initial results:

- `origin/main..upstream/main`: 1228 commits
- `HEAD..upstream/main`: 1493 commits
- `HEAD...upstream/main` right-only after patch-equivalence filtering: 1450 commits

Current refresh after the 2026-06-12 intake continuation:

- `upstream/main`: `93a2f680fd18f08f70eaf5c96944cdf4dc477143`
- `origin/main..upstream/main`: 1288 commits
- `HEAD..upstream/main`: 1553 commits
- `HEAD...upstream/main` right-only after patch-equivalence filtering: 1509 commits

Continuation refresh:

- `git fetch https://github.com/NousResearch/hermes-agent.git refs/heads/main:refs/remotes/upstream/main`
  advanced `upstream/main` from `4d22b8293374fd9eaeac75e1f607b20ddea3a1b3`
  to `e71d746820bf262214e4e1887683d3f65d211cc1`.
- A later refresh advanced `upstream/main` from
  `e71d746820bf262214e4e1887683d3f65d211cc1` to
  `93a2f680fd18f08f70eaf5c96944cdf4dc477143`.
- A follow-up read-only subagent review was dispatched for
  `4d22b8293374fd9eaeac75e1f607b20ddea3a1b3..upstream/main`.

High-level file-surface distribution from `origin/main..upstream/main`:

| Area | Changed files |
| --- | ---: |
| `apps` | 515 |
| `tests` | 439 |
| `website` | 192 |
| `optional-skills` | 122 |
| `hermes_cli` | 109 |
| `ui-tui` | 75 |
| `plugins` | 65 |
| `web` | 60 |
| `tools` | 49 |
| `agent` | 48 |
| `skills` | 41 |
| `gateway` | 27 |

The update is broad and cannot be safely merged wholesale.

## Classification Rules

P0 candidates:

- security fixes for credential handling, secret redaction, dependency CVEs, or
  fail-closed authorization behavior;
- production reliability fixes for gateway message delivery, stream recovery,
  session corruption, process leaks, restart behavior, or state persistence;
- Feishu-specific fixes that preserve live event-ledger, session routing, and
  default-deny semantics.

P1 candidates:

- CLI/tool/runtime fixes that are low-conflict and improve operator workflow on
  the deployed fork;
- dependency/packaging fixes that do not introduce broad runtime behavior
  changes;
- tests or diagnostics that directly protect existing fork behavior.

P2/deferred:

- desktop/dashboard feature expansion, optional skills, new external platform
  integrations, visual/UI work, and generated docs unless they directly affect
  Backend1 operation;
- model-catalog churn that would confuse the current `gpt-5.5` runtime channel
  constraint;
- changes that move authorization into adapter-owned policy without preserving
  the fork's gateway-level final guard;
- broad refactors of `gateway/run.py`, `gateway/platforms/feishu.py`,
  `gateway/session.py`, or config/session identity without a manual port plan.

## Parallel Review Tracks

Four read-only subagent tracks were dispatched:

- Gateway/Feishu/live-service/session.
- Agent/CLI/tooling/MCP/file-tools/runtime stability.
- Security/auth/providers/models/dependencies/update/packaging.
- Desktop/dashboard/kanban/skills/docs/low-priority surface.

Their findings will be merged below before any code absorption batch.

## Subagent Findings

### Desktop / Dashboard / Kanban / Skills Surface

Read-only review classified most desktop and dashboard work as deferred for the
Backend1 Feishu target. The dashboard admin/profile builder/file browser,
desktop remote-gateway attachment surface, public-bind/OAuth/OIDC/BasicAuth
work, new external platforms, and external tool integrations all expand runtime
or supply-chain surface and should not be folded into the first upstream intake.

The same review identified lower-risk candidates for later batches:

- Kanban stability fixes that do not define the Feishu production path.
- Skill safety and reliability fixes, especially path traversal and write
  approval gates.
- Skill catalog path corrections and moving risky skills out of the built-in
  default surface.

### Agent / CLI / Tooling Runtime

Read-only review identified three P0 groups:

- Configuration and approval write-bypass protections.
- Runtime CWD and file-tool workspace anchoring.
- Compression/session-handoff contamination and session-id rotation fixes.

The first group has the smallest conflict surface and was selected as the first
absorption batch. The CWD and compression groups are still important but touch
file-tool/runtime and gateway/session paths that overlap the fork's live
gateway changes, so they require separate manual-port batches.

## Absorbed Batches

### 2026-06-11 — P0 Approval And Hermes Config Write Gates

Absorbed upstream commits:

- `8f2931e3ee518ddbb78789fc013fbc69aa868851` — block file-tool writes to
  `~/.hermes/config.yaml`.
- `4e9d886d9d9391d096093071cd79ece7e543f3e0` — add terminal-side approval
  gate for Hermes config writes.
- `a6a4e6f9d756a1a35ae8125b4e6c3fafb6807165` — gate `perl`/`ruby -i`
  in-place edits of Hermes config/env files.
- `b04c6e95f60e0ab9bf926bbf566faa9ec523a43b` — catch `perl`/`ruby -i` when
  `-i` appears as a separate flag token.
- `621bf3a873b6b466b7fca6fbd6f4c7cf83a70fdd` — strip shell escapes in the
  denylist normalizer and fail closed when the approval module is unavailable.
- `b0efe1d64b3ab005e9c3096b7023eb5e076a3a61` — gate resolved Hermes config
  paths, not only `~` or `$HERMES_HOME` spelling.
- `89d380261d1b5ebd69c3b87504da2f89fb0666f5` — resolve Hermes home at
  detection time rather than import time.

Fork-local adjustment:

- `tools/file_tools.py` now checks the exact resolved Hermes config path before
  generic sensitive system prefixes. This preserves the fork's existing
  `/private/var/` guard while keeping the new Hermes-config failure class
  observable on macOS temporary paths.

Verification:

```bash
uv run pytest -q tests/tools/test_approval.py tests/tools/test_file_tools.py
```

Result: `244 passed`.

### 2026-06-11 — Feishu Card Action And Message Text Cache Hardening

Manually ported upstream semantics:

- `3fa15b33dd910699f18a0529f448f97eb8f02042` — fail closed for update-prompt
  card actions when the operator is not authorized for the target chat, or when
  the callback chat does not match the stored prompt chat.
- `e8cacb57d531137dec3109617e807b30ff5187c9` plus the Feishu part of
  `32899279a744805350be891ccf3ae08289efc702` — bound
  `_message_text_cache` with an LRU cap.

Fork-local integration notes:

- The update-prompt callback keeps this fork's existing broker-context and
  delivery-audit preconditions; the upstream authorization and chat-mismatch
  checks were inserted after those fail-closed gates rather than replacing them.
- `_resolve_update_prompt()` repeats the operator/chat checks when evidence is
  supplied, so bypassing the synchronous card-action entrypoint does not skip
  the scope guard.
- The message-text cache uses `OrderedDict` and refreshes entries on cache hits;
  new inserts rely on normal insertion order and evict oldest entries after the
  configured cap.

Verification:

```bash
uv run pytest -q tests/gateway/test_feishu_approval_buttons.py tests/gateway/test_feishu.py::TestFeishuFetchMessageText
```

Result: `86 passed, 2 warnings`.

### 2026-06-11 — Dependency And Config Hygiene Batch

Absorbed upstream commits:

- `ee7948ea6e3b7b6187d40702a76204927b6688e3` — exclude `[dev]` tooling
  from `[all]`.
- `b434f8c3efb716d88177a3e7509827bdc6336aef` — promote `Markdown` to a
  core dependency so rich message delivery has its renderer available.
- `c6d27addf73d8f7a51d4640f120085e5bcf8942e` — align `aiohttp` extras with
  the lazy Slack pin.
- `2f510ca8e415bd38d81362b1300c290bd0bfbdc7` — align the Anthropic extra
  with the lazy dependency pin and add a general pyproject/lazy-deps drift
  guard.
- `b5421f4ba606df706472ac1a4b36a84d9e1973c8` — declare `packaging` as a
  core dependency.
- `f6416f50fced55260144a702e06ec5026c2dcffd` — bump `PyJWT` and `urllib3`
  for published vulnerability fixes.
- `e4a1b35a3d69e98ca4ad20f29c81aeb808e82a58` — preserve existing `.env`
  file mode in `save_env_value()`.
- `15813336c4d6100630583b0a5309b41db3d84ed6` — preserve existing `.env`
  file mode in `remove_env_value()`.

Fork-local integration notes:

- Lockfile conflicts were resolved by preserving this fork's already-selected
  `[all]` profile: `pty`, `web`, Google, YouTube, MCP, Home Assistant, SMS,
  ACP, CLI, and cron remain in `[all]`; `[dev]` is excluded.
- Official-main context that was adjacent to these patches but outside their
  stated purpose was not absorbed: `nemo-relay`, core `uvicorn`,
  `pathspec`, and `pillow` were not added as new direct/core dependencies.
- No model-catalog or model-selection changes were absorbed in this batch.

Verification:

```bash
uv lock --check
uv run pytest -q tests/test_project_metadata.py
uv run pytest -q tests/test_packaging_metadata.py tests/test_project_metadata.py
uv run pytest -q tests/hermes_cli/test_config.py
```

Observed results:

- `tests/test_project_metadata.py`: `9 passed`.
- `tests/test_packaging_metadata.py tests/test_project_metadata.py`: `16 passed`.
- `tests/hermes_cli/test_config.py`: `89 passed`.

### 2026-06-11 — Gateway Local-Path And Media Safety

Absorbed upstream commits:

- `02d1da49de5086946256cc157ff928dcffbe8ca1` — block active-profile and
  shared Hermes root config/control files from native media delivery.
- `bdfba45247ed2b742d6f452353e6dbcdaaf8e9e6` — skip bare local-path
  auto-upload detection for `EphemeralReply` system/command notices.
- `9b78f411c8be21ff90136cafefae65451c24804b` — backtick-wrap mutation
  verifier footer paths so gateway bare-path extraction cannot turn them into
  native attachments.

Fork-local integration notes:

- `gateway/platforms/base.py` already had Feishu/live-gateway delivery
  metadata changes, so the `02d1da49` conflict was resolved manually by
  preserving local behavior and adding the upstream `_HERMES_ROOT` denylist
  semantics.
- The tips text no longer embeds literal `~/.hermes/...` paths in ephemeral
  user-facing notices. This keeps system notices readable without turning
  sensitive local paths into delivery candidates.

Verification:

```bash
uv run pytest -q tests/gateway/test_platform_base.py tests/gateway/test_ephemeral_reply.py tests/run_agent/test_file_mutation_verifier.py
uv run pytest -q tests/gateway/test_feishu.py tests/gateway/test_feishu_approval_buttons.py tests/gateway/test_feishu_upload_denial.py
```

Observed results:

- `tests/gateway/test_platform_base.py tests/gateway/test_ephemeral_reply.py tests/run_agent/test_file_mutation_verifier.py`:
  `171 passed, 2 skipped`.
- `tests/gateway/test_feishu.py tests/gateway/test_feishu_approval_buttons.py tests/gateway/test_feishu_upload_denial.py`:
  `317 passed, 2 warnings`.

### 2026-06-12 — Runtime CWD And File Anchoring

Absorbed manually across several small commits:

- `730ac6deb` — route prompt construction through runtime cwd.
- `64271e071` — preserve terminal session cwd and raw task-id overrides.
- `2e6c351a4` — anchor file tools to runtime cwd.
- `3b7571b31` — preserve TUI session/profile/remote cwd.
- `7be85c4be` — keep SSH maintenance subprocesses from inheriting TUI stdin.

Upstream semantic sources include `e45b7458`, `7a315bd7`, `6459b3d9`,
`329c33da`, `ad69d3ed`, `4bc72960`, `16047655`, `f90777a6`, `0e0d704f`,
`f9ea4927`, and `93340fa3`.

Verification:

- `uv run pytest -q tests/tools/test_file_tools_cwd_resolution.py tests/tools/test_resolve_path.py tests/tools/test_file_ops_cwd_tracking.py tests/tools/test_file_tools.py`:
  `69 passed`.
- `uv run pytest -q tests/agent/test_runtime_cwd.py tests/gateway/test_session_env.py tests/agent/test_prompt_builder.py tests/agent/test_system_prompt.py`:
  `160 passed, 1 skipped`.
- `uv run pytest -q tests/tools/test_terminal_task_cwd.py tests/tools/test_shared_container_task_id.py`:
  `22 passed`.
- `uv run pytest -q tests/test_tui_gateway_server.py tests/tools/test_file_tools_cwd_resolution.py tests/tools/test_resolve_path.py tests/tools/test_file_ops_cwd_tracking.py tests/tools/test_file_tools.py`:
  `266 passed, 1 warning`.
- `uv run pytest -q tests/tools/test_ssh_environment.py tests/tools/test_ssh_bulk_upload.py`:
  `36 passed, 11 skipped`.

### 2026-06-12 — Gateway Service And Restart Reliability

Absorbed:

- `1447b6d39` — refuse service definition writes when `HERMES_HOME` is a
  temporary test/home path.
- `275f6276d` — preserve detached restart watcher environment and scrub
  `_HERMES_GATEWAY` on Windows and POSIX restart watchers.
- `6b1243997` — probe launchd domain (`gui/<uid>` vs `user/<uid>`) instead of
  hardcoding one domain.

Verification:

- `uv run pytest -q tests/hermes_cli/test_gateway_service.py tests/hermes_cli/test_gateway.py tests/hermes_cli/test_gateway_linger.py`:
  `186 passed` for the temp-home guard batch and `192 passed` after launchd
  domain probing.
- `uv run pytest -q tests/gateway/test_restart_drain.py`: `19 passed`.
- `uv run python -m py_compile hermes_cli/gateway.py tests/hermes_cli/test_gateway_service.py`:
  passed.

### 2026-06-12 — Backup, MCP, And Provider Runtime Fixes

Absorbed:

- `38c6c17a6` — stage SQLite snapshots beside the backup archive and include
  nested `skills/.../hermes-agent/` directories while excluding only the root
  repo checkout.
- `e19dea6c0` — preserve MCP argv passthrough, propagate profile-scoped
  `HERMES_HOME` onto the MCP event loop, and represent configured/connecting/
  disabled MCP startup states without false failed status.
- `418e2a5e4` — fall back to non-streaming Bedrock InvokeModel when streaming
  is denied by IAM.

Verification:

- `uv run pytest -q tests/hermes_cli/test_backup.py`: `117 passed`.
- `uv run pytest tests/hermes_cli/test_apply_profile_override.py tests/hermes_cli/test_mcp_add_command_dest.py tests/hermes_cli/test_mcp_config.py tests/hermes_cli/test_banner.py tests/tools/test_mcp_loop_profile_override.py tests/tools/test_mcp_tool.py -q`:
  `253 passed, 1 warning`.
- `uv run pytest -q tests/agent/test_bedrock_adapter.py tests/run_agent/test_streaming.py::TestStreamingFallback`:
  `132 passed, 1 warning`.

### 2026-06-12 — Messaging And Dashboard Safety Fixes

Absorbed:

- `d5467a645` — gate oversized Telegram voice/audio files before download.
- `9481b15e` — normalize dashboard main model assignments at the
  `/api/model/set` persistence chokepoint without changing model catalog or
  defaults.
- `cf7e55f47` — fix a non-secret web test fixture so dashboard web tests run
  under current redaction semantics.

Verification:

- `uv run pytest -q tests/gateway/test_telegram_documents.py tests/gateway/test_telegram_audio_vs_voice.py tests/gateway/test_telegram_max_doc_bytes.py`:
  `51 passed`.
- `uv run pytest -q tests/hermes_cli/test_web_server.py`: `153 passed,
  1 warning`.

### 2026-06-12 — State Database Recovery

Absorbed:

- `f75191fcd` — recover from malformed `sqlite_master` state.db schemas
  (duplicate FTS schema objects) by backing up the raw DB, applying a narrow
  repair ladder, and reopening once. The fork-local implementation keeps stable
  failure evidence (`failure_class`, `stage`, `strategy`, `backup_name`) and
  does not downgrade non-malformed SQLite errors into success.

Verification:

- `uv run pytest -q tests/test_state_db_malformed_repair.py tests/test_hermes_state.py tests/test_hermes_state_wal_fallback.py`:
  `297 passed`.
- `uv run pytest -q tests/hermes_cli/test_doctor.py tests/hermes_cli/test_sessions_optimize.py`:
  `60 passed, 1 warning`.
- `uv run python -m py_compile hermes_state.py hermes_cli/main.py hermes_cli/doctor.py tests/test_state_db_malformed_repair.py`:
  passed.
- `uv run ruff check hermes_state.py hermes_cli/main.py hermes_cli/doctor.py tests/test_state_db_malformed_repair.py`:
  passed.

## Remaining Deferred Or Skipped Upstream Areas

No unconditional P0/P1 remains from the reviewed `e71d7468..93a2f680f`
increment. Deferred areas remain intentionally unmerged:

- model catalog/default/picker/visibility and model steering changes, including
  `a4f179c50` and later model-policy commits. These conflict with the current
  "do not add models; current channel is 5.5" constraint and require separate
  model-policy review.
- desktop remote filesystem APIs and desktop UI fixes (`51f47f9a9`,
  `db79e9013`, `8878484f8`, `56a0f48ba`, `9121834b3`, `8505e9d66`,
  `93a2f680f`).
- cron/automation blueprint feature stack (`9a09ea69f`, `1593ca540`,
  `e976faac7`, `e8b757845`, `cb29e8a82`).
- broad dashboard/profile management and SKILL.md editor surfaces
  (`875aa8f1`, `a09343cc`, `c7bfc938d`) except for the narrow main-model
  assignment normalization already absorbed.
- generic gateway topic/session routing and platform-specific stacks that
  overlap Feishu routing or are not in the Backend1 production target, including
  Matrix room isolation and WhatsApp stale-bridge restart. These should be
  reviewed as platform-specific work, not broad intake.
