# Official main intake audit

> Date: 2026-06-11
> Working branch: `fix/live-gateway-hermes-tools`
> Local fork main: `origin/main` at `5921d667855880b0aa2083a50f001748aed52f3e`
> Official main ref: `upstream/main` at `08b1c44a5330cb690a26a5e1983ff7719ec4439c`
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

- `upstream/main`: `4d67ac617296be2581461837085c807a7a9db99b`
- `origin/main..upstream/main`: 1331 commits
- `HEAD..upstream/main`: 1596 commits
- `HEAD...upstream/main` right-only after patch-equivalence filtering: 1551 commits

Continuation refresh:

- `git fetch https://github.com/NousResearch/hermes-agent.git refs/heads/main:refs/remotes/upstream/main`
  advanced `upstream/main` from `4d22b8293374fd9eaeac75e1f607b20ddea3a1b3`
  to `e71d746820bf262214e4e1887683d3f65d211cc1`.
- A later refresh advanced `upstream/main` from
  `e71d746820bf262214e4e1887683d3f65d211cc1` to
  `93a2f680fd18f08f70eaf5c96944cdf4dc477143`.
- A further refresh advanced `upstream/main` from
  `93a2f680fd18f08f70eaf5c96944cdf4dc477143` to
  `08b1c44a5330cb690a26a5e1983ff7719ec4439c`.
- A further refresh advanced `upstream/main` from
  `9e484f052a99ca4ed312e8232b91a3c77a289cab` to
  `4d67ac617296be2581461837085c807a7a9db99b`.
- A later refresh advanced `upstream/main` to
  `24f74eb88853162899fe345dc34a9c5a20f66657`.
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
- model-catalog/default churn is reviewed for compatibility and operator
  impact; `gpt-5.5` is the current runtime channel, not a repository-level
  model/catalog freeze;
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

### 2026-06-12 — Attachment Guidance, Web Build, And Discord Cleanup

Reviewed upstream increment:

- `93a2f680fd18f08f70eaf5c96944cdf4dc477143..08b1c44a5330cb690a26a5e1983ff7719ec4439c`

Absorbed semantics:

- `e7ae145ac` and `4e9be3ee3` — document attachment context now tells the
  agent to extract text from binary documents such as PDF/DOCX before
  answering, instead of asking the user to paste the contents. The fork-local
  wording avoids upstream-specific skill names and preserves the existing
  agent-visible cache path handling.
- `13650ab7f` — audio-file attachment context now tells the agent to
  transcribe/process the saved file when the request concerns the audio
  contents, instead of steering it into asking the user what to do.
- `ce99a8112` — deterministic web UI npm installs now set `CI=1` so
  postinstall hooks that write directly to `/dev/tty` do not pollute or wedge
  non-interactive builds. This was hand-ported because the fork-local helper
  signature differs from upstream.
- `020ef76cf` and `08b1c44a5` — Discord adapter startup failure paths now
  cancel and await the background bot task on ready-timeout, generic
  connection failure, and disconnect. This prevents a discarded Discord client
  from later connecting and processing duplicate inbound events.

Fork-local review notes:

- Feishu remains on its existing websocket-thread lifecycle. The Discord
  zombie-client pattern was not applied to Feishu because Feishu `connect()`
  does not create an adapter-owned `client.start()` task and then wait for a
  ready event; disconnect already disables websocket auto-reconnect, cancels
  tasks on the websocket thread loop, and waits for the executor future.
- No model catalog, default model, model visibility, or model steering changes
  were absorbed.
- The desktop DnD dependency/hash change from `743c55efa` remains deferred.

Verification:

- `uv run pytest -q tests/gateway/test_document_context_note.py tests/gateway/test_telegram_audio_vs_voice.py tests/hermes_cli/test_web_ui_build.py`:
  `30 passed`.
- `uv run pytest -q tests/gateway/test_discord_connect.py tests/gateway/test_document_context_note.py tests/gateway/test_telegram_audio_vs_voice.py tests/hermes_cli/test_web_ui_build.py`:
  `48 passed`.
- `uv run ruff check gateway/run.py hermes_cli/main.py tests/gateway/test_document_context_note.py tests/gateway/test_telegram_audio_vs_voice.py tests/hermes_cli/test_web_ui_build.py`:
  passed.
- `uv run python -m py_compile gateway/run.py hermes_cli/main.py tests/gateway/test_document_context_note.py tests/gateway/test_telegram_audio_vs_voice.py tests/hermes_cli/test_web_ui_build.py`:
  passed.
- `uv run ruff check plugins/platforms/discord/adapter.py tests/gateway/test_discord_connect.py`:
  passed.
- `uv run python -m py_compile plugins/platforms/discord/adapter.py tests/gateway/test_discord_connect.py`:
  passed.

### 2026-06-12 — MCP, Plugin Discovery, And Web Provider Registration

Absorbed:

- `5affecb44` — gate MCP `tools/list` and keepalive probing on the server's
  advertised `tools` capability, so prompt-only/resource-only MCP servers do
  not fail with method-not-found during discovery or keepalive.
- `114e26573` — reset plugin-manager discovery state when a sweep raises, so
  a swallowed plugin discovery failure is not cached as a permanently empty
  registry.
- `93764b930` and `32a73010b` — guarantee bundled `plugins/web/*` provider
  registration after a failed or incomplete plugin sweep.

Fork-local integration notes:

- The web provider fallback was intentionally scoped to registration recovery.
  The larger Parallel free-MCP/keyless runtime from `e0e257171` was not folded
  into this batch because it touches provider transport, CLI tools config,
  display labels, dependency lockfiles, and a broad test set.
- No dashboard/frontend web surface was restored; `tools/web_tools.py` and
  `plugins/web/*` are runtime tools, not the pruned visual frontend.

Verification:

- `uv run pytest -q tests/tools/test_mcp_capability_gating.py tests/tools/test_mcp_tool.py tests/hermes_cli/test_plugins.py`:
  `282 passed, 1 warning`.
- `uv run pytest -q tests/tools/test_web_keyless_default_fallback.py tests/integration/test_web_tools.py tests/tools/test_web_providers.py tests/tools/test_web_tools_config.py tests/plugins/web/test_web_search_provider_plugins.py`:
  `119 passed, 1 warning`.
- `uv run ruff check tools/web_tools.py tests/tools/test_web_keyless_default_fallback.py`:
  passed.

### 2026-06-12 — Stale Compaction Handoff Safeguards

Manually backported the semantics from:

- `d5e2fbf24` — frame compaction handoff task/pending/remain-work sections as
  historical context.
- `8f8cad7ec` — remove the "consistent -> use as background" carveout that
  allowed stale-task resumption on topic overlap, and strengthen the
  latest-user-message-wins rule.
- `acb2954d8` — freeze the retired carveout-era `SUMMARY_PREFIX` for
  detection and renormalization after upgrade.
- `6c752ca3a` — tighten the final prefix wording and stale heading references.

Fork-local integration notes:

- This was a manual backport rather than a cherry-pick because the fork already
  had earlier compaction-handoff changes and diverged heavily from upstream's
  file layout.
- The live prefix now points at `## Historical Task Snapshot`,
  `## Historical In-Progress State`, `## Historical Pending User Asks`, and
  `## Historical Remaining Work`; old summaries containing the previous
  `## Active Task` wording remain detectable through frozen historical
  prefixes.

Verification:

- Red check before production changes:
  `uv run pytest -q tests/agent/test_summary_prefix_semantics.py` produced
  `3 failed, 3 passed` on the new stale-handoff assertions.
- Green checks after the backport:
  `uv run pytest -q tests/agent/test_summary_prefix_semantics.py`:
  `6 passed`.
- `uv run pytest -q tests/agent/test_context_compressor.py tests/agent/test_resume_stale_active_task.py tests/agent/test_summary_prefix_semantics.py tests/agent/test_context_compressor_summary_continuity.py`:
  `104 passed, 1 warning`.
- `uv run ruff check agent/context_compressor.py tests/agent/test_context_compressor.py tests/agent/test_resume_stale_active_task.py tests/agent/test_summary_prefix_semantics.py`:
  passed.

### 2026-06-12 — Terminal Environment Persistence Guidance

Absorbed the applicable terminal subset from `ab06ef8ed`:

- terminal tool description now states that filesystem, current working
  directory, and exported environment variables persist between calls;
- tests pin that exported env changes and virtualenv-style activation survive
  across `LocalEnvironment.execute()` calls.

Not applicable:

- `96cc7ee1` and the `agent/coding_context.py` part of `ab06ef8ed` target an
  `agent/coding_context.py` module that is absent from this fork. No new
  coding-context module was created for intake bookkeeping.

Verification:

- Red check before the description change:
  `uv run pytest -q tests/tools/test_terminal_tool.py tests/tools/test_local_shell_init.py`
  produced `1 failed, 30 passed`; the failure was the new terminal schema
  persistence assertion.
- Green checks:
  `uv run pytest -q tests/tools/test_terminal_tool.py tests/tools/test_local_shell_init.py`:
  `31 passed`.
- `uv run pytest -q tests/tools/test_terminal_tool.py tests/tools/test_local_shell_init.py tests/tools/test_terminal_task_cwd.py tests/tools/test_terminal_config_env_sync.py`:
  `46 passed`.
- `uv run ruff check tools/terminal_tool.py tests/tools/test_local_shell_init.py tests/tools/test_terminal_tool.py`:
  passed.

### 2026-06-12 — Parallel Keyless Web Runtime

Absorbed upstream commits:

- `e0e257171` — Parallel-backed web search/extract with keyless free Search
  MCP when no `PARALLEL_API_KEY` is configured, and v1 REST when keyed.
- `0a5762c78` — genericize the keyless MCP client identity.
- `383d44bc9` — rank explicit credentials ahead of managed gateway probes in
  web backend auto-detection.
- `2ee8c983c` and `7df81d055` — make SearXNG and web backend lookup honor
  Hermes config/env resolution, not only process environment variables.

Fork-local integration notes:

- Preserved the fork's bundled web-provider fallback registration path from
  the earlier `93764b930`/`32a73010b` batch, so keyless Parallel does not
  depend on a perfect plugin discovery sweep.
- Kept runtime web tools (`tools/web_tools.py`, `plugins/web/*`) in scope.
  This does not restore the removed visual frontend/dashboard web surface.
- Resolved lockfile churn narrowly: `parallel-web` moved from `0.4.2` to
  `0.6.0`; unrelated upstream lock/dependency changes were not imported.
- Restored Feishu default toolset recovery for broker-gated legacy
  `feishu_doc`/`feishu_drive` while keeping `hermes-feishu` itself free of the
  legacy document/comment business tools required by Package B/C scope tests.

Verification:

- `uv run pytest -q tests/tools/test_web_providers_ddgs.py tests/tools/test_web_providers_searxng.py tests/hermes_cli/test_tools_config.py::test_get_platform_tools_feishu_includes_doc_and_drive tests/hermes_cli/test_tools_config.py::test_get_platform_tools_feishu_tools_not_on_other_platforms tests/gateway/test_feishu_package_b_scope.py::test_hermes_feishu_toolset_does_not_expose_legacy_doc_drive_tools tests/gateway/test_feishu_package_c_scope.py::test_legacy_feishu_doc_drive_toolsets_remain_visible_as_brokered_legacy_risk_only`:
  `47 passed`.
- `uv run pytest -q tests/plugins/web/test_parallel_keyless_mcp.py tests/plugins/web/test_web_search_provider_plugins.py tests/tools/test_web_providers.py tests/tools/test_web_tools_config.py tests/tools/test_web_providers_searxng.py tests/tools/test_web_providers_ddgs.py tests/tools/test_web_providers_brave_free.py tests/agent/test_display.py tests/hermes_cli/test_tools_config.py tests/tools/test_web_keyless_default_fallback.py`:
  `350 passed, 1 warning`.
- `uv run pytest -q tests/hermes_cli/test_plugins.py`:
  `73 passed, 1 warning`.
- `uv lock --check`, `git diff --check`, `uv run python -m py_compile ...`,
  and `uv run ruff check ...`: passed.

### 2026-06-12 — Gateway MEDIA Extraction Hardening

Manually ported upstream MEDIA safety semantics from:

- `e8827ef70` — ignore `MEDIA:` bare paths embedded inside serialized JSON
  string values.
- `fb1b681b3` — use masks only to locate real tag spans, so JSON-embedded
  `MEDIA:` text remains verbatim in cleaned output.
- `3ccf4fdc6` — ignore `MEDIA:` examples in fenced code blocks, inline code,
  and blockquotes.
- `6c73e8ffa` — verify protected spans remain verbatim when a real MEDIA tag is
  stripped from the same response.
- `521d06975` — auto-append tool-result media only from tools that intentionally
  produce deliverable artifacts.
- `9351cbafa` — auto-append `image_generate` JSON local paths without relying
  on the model restating the path.

Fork-local integration notes:

- Preserved this fork's broader `MEDIA_DELIVERY_EXTS`, Windows path support,
  path validation, Feishu upload provenance, and current-turn scan helper.
- The producer gate intentionally keeps `execute_code` in addition to upstream
  TTS/image producers, because this fork already supports current-turn generated
  files from code execution as deliverable artifacts.
- Skill docs, logs, stored JSON tool results, code blocks, inline code, and
  blockquotes no longer become native attachments merely because they contain
  example or historical `MEDIA:` text.

Verification:

- `uv run pytest -q tests/gateway/test_platform_base.py::TestExtractMedia tests/gateway/test_platform_base.py::TestMediaInsideSerializedJson tests/gateway/test_media_extraction.py tests/gateway/test_run_tool_media_re.py`:
  `69 passed`.
- `uv run pytest -q tests/gateway/test_platform_base.py tests/gateway/test_media_extraction.py tests/gateway/test_run_tool_media_re.py tests/gateway/test_feishu_upload_denial.py`:
  `193 passed, 2 skipped, 2 warnings`.
- `uv run pytest -q tests/gateway/test_stream_consumer.py::TestCleanForDisplay tests/gateway/test_stream_consumer.py::TestSendOrEditMediaStripping tests/gateway/test_stream_consumer.py::TestStreamRunMediaStripping`:
  `20 passed`.
- `uv run pytest -q tests/gateway/test_feishu_attachment_provenance.py::test_generated_attachment_bare_path_denies_despite_matching_provenance`:
  `1 passed, 2 warnings`.
- `uv run ruff check ...`, `uv run python -m py_compile ...`, and
  `git diff --check`: passed.

## Remaining Deferred Or Skipped Upstream Areas

No unconditional P0/P1 remains from the previously reviewed
`e71d746820bf262214e4e1887683d3f65d211cc1..08b1c44a5330cb690a26a5e1983ff7719ec4439c`
increment after the 2026-06-12 continuation above. Deferred areas remain
intentionally unmerged:

- model catalog/default/picker/visibility and model steering changes, including
  `a4f179c50` and later model-policy commits. These remain under separate
  model/catalog compatibility review; the current `gpt-5.5` runtime channel is
  not a policy freeze.
- desktop remote filesystem APIs and desktop UI fixes (`51f47f9a9`,
  `db79e9013`, `8878484f8`, `56a0f48ba`, `9121834b3`, `8505e9d66`,
  `93a2f680f`, `743c55efa`).
- cron/automation blueprint feature stack (`9a09ea69f`, `1593ca540`,
  `e976faac7`, `e8b757845`, `cb29e8a82`).
- broad dashboard/profile management and SKILL.md editor surfaces
  (`875aa8f1`, `a09343cc`, `c7bfc938d`) except for the narrow main-model
  assignment normalization already absorbed.
- generic gateway topic/session routing and platform-specific stacks that
  overlap Feishu routing or are not in the Backend1 production target, including
  Matrix room isolation and WhatsApp stale-bridge restart. These should be
  reviewed as platform-specific work, not broad intake.
