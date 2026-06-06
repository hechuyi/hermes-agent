# Main safe fixes integration audit

> Date: 2026-06-06
> Base branch: `fix/live-gateway-hermes-tools`
> Integration branch: `integrate/main-safe-fixes`
> Compared main head: `origin/main` at `5921d667855880b0aa2083a50f001748aed52f3e`

## Scope

This integration pass reviewed `HEAD..origin/main` after the Hermes live gateway
event-ledger/preflight internalization work. The rule for this pass was narrow:
absorb fixes that do not conflict with, or materially change, the custom live
gateway ledger/preflight work. Anything touching the live runner, Feishu P0
event path, internal gateway event ledger, gateway preflight, service launch
semantics, access-control default-deny behavior, or media/delivery outcome
contract remains deferred for a later manual port.

The protected local paths for this pass were:

- `gateway/gateway_event_ledger.py`
- `gateway/gateway_event_contract.py`
- `gateway/hermes_tools_gateway_event.py`
- `hermes_cli/gateway.py`
- `gateway/platforms/feishu.py`
- high-risk sections of `gateway/run.py`, gateway config, media extraction,
  delivery outcome handling, and platform pause/reconnect policy

## Absorbed commits

The following upstream commits were cherry-picked with `-x` and verified on the
integration branch:

| Upstream commit | Category | Reason |
| --- | --- | --- |
| `54bf798765d3d529978dd04e3bfc95d93d6504eb` | approval safety | Requires approval for dangerous Docker lifecycle commands. |
| `4126da65ae80643618c067ea5aca023561af8c6d` | secret safety | Blocks direct file-tool reads of `cache/bws_cache.json`. |
| `86a389fee29796a079599d479229a68f5e845671` | credential pool | Marks terminal OAuth failures as `STATUS_DEAD` instead of recycling broken credentials. |
| `c01a2df0a322d958a37d013ff87bb5f1ac9d447f` | OAuth UX/safety | Avoids launching text-mode browsers inside terminal sessions. |
| `95b5b72404cb9fea277fef572f0eaa0c1fec720b` | security staging | Predecessor to the Bedrock env-strip narrowing; retained with the follow-up fix below. |
| `6bebab4761e853010ad32d48e9d3aacebd72ca46` | security | Narrows subprocess stripping to `AWS_BEARER_TOKEN_BEDROCK`, preserving the general AWS credential chain. |
| `27a2c4f36ff323e5edc300496d001e28a0c35678` | MCP auth | Prevents `hermes mcp login` from reporting success when no OAuth token was obtained. |
| `aa283d1e4f45731087653bbf7c057761c517cf28` | model credentials | Isolates custom provider picker credentials. |
| `45b00bb49aa3c27a8838cdaa8673133f0a89db82` | packaging | Ships `hermes_cli.*` subpackages in wheels. |
| `9f5afc7636246320dc3e8fd4f9d5aef50fbadcfb` | MCP stability | Treats `CancelledError` as `BaseException` when logging MCP connection failures. |
| `689ef5e233980f5d5a32080e959f44c8991dd03a` | CLI/update | Warns on unsupported pip installs and fixes stale update-check cache. |
| `f9daa4a41d6394663fe4acb8888ebf723b91ca9b` | test packaging deps | Declares `setuptools` in the dev extra and syncs the lockfile. |
| `1bdb29d938533e03bc3f6df0b448b4c72ce13c33` | update path | Uses `uv tool upgrade` for uv-tool installs. |
| `bebd4f851631e65e0ca0eaa1266ad4bce8aad701` | update path | Restricts uv-tool-install detection to the running interpreter. |
| `a29d64e50ce40bd78d63146f5c2653e60301d7c3` | MCP process cleanup | Reaps stdio MCP grandchildren via process-group signaling. |
| `39f6b6e9d225bf8e05b0f2ccbc733b8063eb8973` | file tools | Makes `write_file` and `patch` use temp-file plus rename. |
| `14517ac1f5977f4d21e10153069eb52aac60311c` | update path | Exports launcher virtualenv information to uv. |
| `a57cc0008166109df85505e9b2996df67dbc210b` | packaging | Includes `mcp_serve` in `py-modules`. |
| `5a1aa9e68c9c1de80fed947f89102839c23926e2` | account cache | Adds a threading lock around `nous_account` cache access. |
| `ea6eaabd8f6ee01fac73ea4c0398ee2f987a7e17` | read_file perf | Compacts read-file line-number gutter output. |
| `9fbde54b5176b6a2fef198b6d870704a3fd994d6` | CLI fail-closed | Fails closed on empty oneshot responses. |
| `5921d667855880b0aa2083a50f001748aed52f3e` | CLI terminal safety | Stops OSC 11 background probing from trapping users in a stray editor. |
| `90b3c54de97267015aa3b65d7330edd3f3c01f91` | process drain | Prevents drain-thread crashes on fd-less stdout streams. |
| `75d2c081c9abd60333694b13f1d753c3a8361f61` | logging | Recovers the `gateway.log` handler after external log rotation. |
| `2259c15e4d6f80d026d555c1c4b7019581283a82` | status UX | Clarifies `/status` session usage label. |
| `9d4c81130a39f4a725b8301610d52c7cbff06fc6` | status UX | Names the `/status` token number more explicitly. |
| `0437137fff821854066088fbcd590d7af54c6857` | security | Pins `starlette==1.0.1` in server-surface extras and lazy deps for CVE-2026-48710. |

### Supplemental low-risk batch

After the remaining `origin/main` changes were reviewed again under the stricter
rule "do not absorb anything that conflicts with or changes the current
live-gateway/hermes-tools boundary", the following additional commits were
absorbed:

| Upstream commit | Local commit | Category | Reason |
| --- | --- | --- | --- |
| `66265a0571347ce6c88f940d79869002b09af0ca` | `6af1be0cc` | Nix | Drops stale `vercel` group from the `#full` package variant. |
| `593e4b435ea5bb5ff73ee8972977388911c062ec` | `c7ee191e1` | Docker image | Adds `iputils-ping` to the image for network diagnostics. |
| `8d129d013bae4293c9232a36ec4b8e4f184dbab0` | `8424f10b9` | Docker metadata | Adds identifying labels to Hermes-created containers without enabling reuse semantics. |
| `40fa0c1d19d5c24955e9b9c6b1f3c6c625d1f81a` | `f4e0ac9f2` | Docker mounts | Minimally ported credential/skills/cache mount shape guards; the conflict resolution intentionally omitted upstream container-reuse/orphan-reaper tests. |
| `48083211ef606f3305c09df576514ac99bc7f594` | `f66a1491d` | Docker UID/GID | Minimally ported `PUID`/`PGID` aliases and stage2-hook tests; Docker docs were resolved in the current s6 wording. |
| `ec7736f8a7fc867405e33ca3356a8bfba423dff9` | `94531dd6b` | Docker socket | Adds Docker socket group membership setup in the stage2 hook. |
| `83a7d0b6016495a5d67f341a5252642ab8128f14` | `fef4ebcc1` | skills sync | Preserves bundled-skill manifests on failed restore and removes read-only backups safely. |
| `8ae0802d59b26b5fdf104c902ca82e434132dd9a` | `cdba35200` | skills sync | Handles read-only directories as well as read-only files. |
| `6a08fd3c3f9046c3037f4924904cfc95df557fb7` | `3046c2e51` | skills test | Makes the restore assertion resilient to redirected `HERMES_HOME`. |
| `41decf2c4a6a0e18387bec52b57a2ab531535e99` | `bb3ad1773` | MCP test | Minimally resolved import-only test collection fix; no MCP runtime code was changed. |
| `1c53d39eaaf2fc57a7fb5039911e28d07ad71cb1` | `396efde69` | test deflake | Prevents process-registry tests from touching real process groups and deflakes PTY resize receive handling. |

`git cherry` marks `40fa0c1d1`, `48083211e`, and `41decf2c4` as non-equivalent
because their conflict resolutions were narrowed for this fork. They should
still be treated as handled for the purposes above, not retried mechanically.

`2765b02021c1e6eb743e2ce2eb359fc66a5aa89e` was attempted and became an empty
patch because the effective plugin-manifest packaging metadata was already
present in the current fork.

### Manual Feishu P0 port

`91b174038c7e2bf6cde056da05f8f90673e8c87a`
(`fix(feishu): bound _chat_locks with LRU eviction`) was manually ported in
local commit `3cef0b1df`. The port is intentionally narrower than a mechanical
cherry-pick: it reuses the fork's existing `OrderedDict` import style, only
touches `_chat_locks` and focused tests, and does not change Feishu inbound
ledger application, delivery outcome handling, replay, media extraction, or
`hermes-tools` gateway event contracts.

One upstream edge was adjusted for the fork: held locks are never evicted. If
all cached chat locks are currently held, the cache may temporarily exceed
`CHAT_LOCK_MAX_SIZE` rather than creating a second live lock for a chat and
weakening per-chat serialization.

### Manual service and file-tools ports

`a1cb5fa2c7cd5239a4909261888453d97086986d`
(`fix(gateway): anchor service WorkingDirectory at HERMES_HOME`) was manually
ported in local commit `582f665ea`. The port is limited to CLI-managed
systemd/launchd service generation: user services anchor `WorkingDirectory` at
the current `HERMES_HOME`, and system-scope services anchor it at the target
user's remapped `HERMES_HOME`. The NixOS module was intentionally not changed:
its `WorkingDirectory` continues to represent the configured workspace while
`HERMES_HOME` remains the state/profile root.

`5f84c9144a2c1f1248e92f53eeb2ea8146ad0883`
(`fix(file-tools): handle UTF-8 BOM in read_file / write_file / patch`) was
manually ported in local commit `6c6557dfc`. The port preserves the upstream
disk-byte behavior and adds a fork-specific guard: lint/LSP analysis receives
BOM-stripped in-memory content while atomic writes still preserve a single
leading UTF-8 BOM on disk. This avoids spurious parser noise such as Python's
`invalid non-printable character U+FEFF` while keeping byte signatures stable.

### Code-exec approval security batch

The code-exec approval-context batch was absorbed in local commit `bf08f524f`:

- `21aeefe5fd1cbed15f6e8c479d3b100b091eae57`
- `1083977261ec96a3234851c74f2dada0eec20518`
- `4bdae3477139129ac0e4774bc4d81c1cc5de0ae2`
- `3171845479f459ed95d052770b35b38254d4a71a`

This batch adds shared thread-context propagation for ContextVars plus
thread-local approval/sudo callbacks, wraps both local and remote
`execute_code` RPC threads, adds a whole-script `execute_code` approval guard
for gateway/ask/cron-deny surfaces before child spawn, tightens child
environment scrubbing from broad `HERMES_` passthrough to an explicit
operational allowlist, and logs dropped non-secret `HERMES_*` vars for
diagnosability.

`7427b9d58` remains deferred: it is a tool-search session toolset scoping fix,
but this fork does not currently contain the `tools/tool_search.py` /
progressive-disclosure base that commit depends on. It should be reviewed only
with the tool-search base, not as part of the code-exec approval batch.

### Update and uninstall path batch

The update/uninstall edge-case batch was absorbed in local commit `fe121bb74`:

- `2334228ecaf972818b987bd3ce6a29042f6e18a8`
- `2475244ca01fa5eb82bb0e3107119ae6258f5d88`
- `c1b2d0917fff3ff68064757c229cef8d717aa4e0`
- `54aa4db1de76a7c4bb02c8a7f7411727384b8fea`

This is a manual equivalent port rather than a mechanical cherry-pick. The
final behavior is: pipx-managed installs use `pipx upgrade hermes-agent`;
bare uv/pip installs outside a venv use `uv pip install --system --upgrade`;
launcher-shim venv installs inject `VIRTUAL_ENV` only for the `uv pip install`
venv path; Windows concurrent-update detection excludes only launcher-shim
ancestors and shows an exact `taskkill /PID ... /F` remediation; container
installation detection trusts the `.install_method=docker` stamp instead of
classifying every container as the published Docker image; uninstall removes
Hermes-managed `node`/`npm`/`npx` symlinks only when they still resolve into
the current `HERMES_HOME/node`.

The container detection change was checked against the current Docker startup
path: `docker/stage2-hook.sh` writes `.install_method=docker` as the `hermes`
user during boot, so published images still fail closed to Docker update
guidance while unstamped manual container installs fall through to git/pip.

`git cherry` may continue to mark these upstream commits as non-equivalent
because the port is consolidated and conflict-resolved against this fork's
current update implementation. Treat them as handled and do not retry them
mechanically.

### Additional low-risk tool/plugin fixes

Two independent low-risk fixes were absorbed in local commit `432f41f80`:

- `44df52005a1b59ae2c8439c4e68e7696851b7035`
- `d473e7c9385e04c975b32d2d2cde3a02ba7d4f47`

The first makes direct Modal credential detection fail closed to `False` when
`Path.home()` or the home-directory probe raises `PermissionError`/`OSError`,
while preserving environment-variable credentials as the higher-priority
signal. The second narrows disk-cleanup cron auto-categorization to
`cron/output/...` and `cronjobs/output/...`, so control-plane files such as
`cron/jobs.json` and `.tick.lock` are not tracked or deleted as disposable cron
output.

Both were manually ported with focused regression tests and do not touch the
live-gateway/hermes-tools boundary.

## Reverted attempted commit

`96643b4a52b118477b07c838e30eb8ae7372062c`
(`fix(file-tools): anchor relative-path resolution to absolute base`) was
attempted and then reverted on this branch. Its new tests showed that the
upstream behavior assumes relative file-tool paths are resolved through live
tracking cwd before file-safety rejection. The current fork rejects bare
relative write paths earlier as sensitive-system-path attempts. That is a
semantic conflict in the file-safety boundary, not a simple merge conflict.

This item should be treated as a manual design task: if the upstream behavior
is desired, first define the ordering contract between live tracking cwd
resolution and file-safety checks, then add regression tests for both
workspace-relative writes and sensitive absolute/system paths.

## Deferred commits

The remaining `origin/main` commits are not rejected globally; they are deferred
because they either touch the custom live gateway closure or are low-value for
the current production target.

### Gateway and live-service commits requiring manual port

These must not be merged mechanically:

- `ac8e238bc` / `d77d87766` / `5c2170a7c` / `2f0f03c40`: Docker container
  reuse, orphan reaper, and persist-mode cleanup semantics. Although adjacent
  Docker metadata was absorbed, these commits touch `gateway/run.py` and
  session-close/container-lifecycle behavior and need a separate Docker runtime
  contract review.
- `100536134` / `db96fc60d`: topic recovery/session identity changes.
- `655090b3d` / `6a2e3c2d2` / `fd09b2c55`: startup risk warnings and adapter
  access-policy changes, including default-deny semantics.
- `08c0b2241` / `781604ce4` / `51d165a8e`: media extraction and tool-result
  scan semantics, adjacent to gateway ledger event attribution.
- `45bc65abb`: delivery outcome semantics for silence narration filtering.
- `0bfe19ba1` / `44f3e5186`: nested gateway platform config handling.
- `2b16b756a`: post-interrupt model recovery and fallback status behavior.
- `45465b0d5`: reconnect/pause policy for transient network and DNS failures.
- `7379f1755`: planned-stop/takeover marker behavior.

### Tooling and runtime commits requiring separate batches

- `7427b9d58`: tool-search session toolset scoping. Defer until the
  progressive tool-search base exists in this fork.
- `2062a8400`, `40fcb9658`, `622e53437`, `f6a2ba626`, `41ff6e593`,
  `7e958dafc`, `95cf8f984`, `a22c25000`: auxiliary/auth-provider behavior.
  Review together so the credential and provider fallback semantics stay
  coherent.
- `5ad2b4c6d`, `97ecfa0fc`, `4fa20f9a8`, `a7421dc7d`, `38695254f`,
  `904c0b479`, `794519c6a`: session/state/SQLite/FTS work. Valuable, but should
  be tested against the current session database and migration contracts.
- `a30480bd2`, `db2ce9e7d`, `e38b0b55d`, `020601d41`, `56b8dccf2`,
  `42bbd221e`: compression/resume behavior. Port as a conversation-compression
  batch, not together with gateway runtime changes.

### Low priority or out of current production scope

Kanban, optional skill catalog, docs-only, release author mapping, voice/video,
and platform-specific changes for unused adapters remain out of scope for this
pass unless the corresponding feature becomes production-relevant.

Special caution: dashboard commits that allow insecure public binds and external
skill-tap commits should be reviewed under a separate security model before
being accepted.

## Recommended next absorption order

The next pass should stay narrow: one behavior domain, one targeted test set,
and no broad `origin/main` merge. Recommended order:

1. `2062a8400`, `40fcb9658`, `622e53437`, `f6a2ba626`, `41ff6e593`,
   `7e958dafc`, `95cf8f984`, `a22c25000`: review the auth/Nous/provider
   behavior together so fallback and credential semantics stay coherent.
2. `08c0b2241`, `781604ce4`, `51d165a8e`: media extraction and tool-result
   scan semantics need a separate gateway ledger attribution review.

The Docker reuse/orphan-reaper group, tool-search base/scoping group,
auth/Nous/provider group, compression/state group, and MEDIA extraction group
should remain separate batches because each changes a runtime contract rather
than just a local implementation detail.

## Verification

Fresh verification on the integration branch:

```bash
uv run --extra dev pytest tests/test_packaging_metadata.py -q -rs
```

Result: `6 passed`.

```bash
uv run --extra dev pytest tests/hermes_cli/test_gateway_service.py::TestGatewayEventPreflight tests/gateway/test_gateway_event_ledger.py tests/gateway/test_hermes_tools_gateway_event.py tests/gateway/test_feishu_gateway_event_apply.py tests/gateway/test_run_progress_topics.py tests/gateway/test_status_command.py tests/test_hermes_logging.py -q -rs
```

Result: `192 passed, 10 skipped`. The skipped tests are the existing
`status-card/task card internalization intentionally deferred by user scope`
cases.

```bash
uv run --extra dev pytest tests/agent/test_anthropic_oauth_pkce.py tests/agent/test_credential_pool.py tests/cli/test_cli_light_mode.py tests/hermes_cli/test_cmd_update.py tests/hermes_cli/test_graphical_browser_detection.py tests/hermes_cli/test_mcp_config.py tests/hermes_cli/test_model_switch_custom_providers.py tests/hermes_cli/test_pip_install_detection.py tests/hermes_cli/test_runtime_provider_resolution.py tests/hermes_cli/test_tui_resume_flow.py tests/hermes_cli/test_update_check.py tests/hermes_cli/test_uv_tool_update.py tests/test_packaging_metadata.py tests/tools/test_file_operations.py tests/tools/test_file_operations_edge_cases.py tests/tools/test_file_write_safety.py tests/tools/test_local_env_blocklist.py tests/tools/test_mcp_stability.py -q -rs
```

Result: `572 passed`.

```bash
uv run --extra dev ruff check pyproject.toml tools/lazy_deps.py tests/test_packaging_metadata.py agent/anthropic_adapter.py agent/credential_pool.py agent/file_safety.py agent/google_oauth.py cli.py hermes_cli/auth.py hermes_cli/banner.py hermes_cli/config.py hermes_cli/main.py hermes_cli/mcp_config.py hermes_cli/model_switch.py hermes_cli/nous_account.py hermes_cli/oneshot.py hermes_logging.py tools/approval.py tools/environments/base.py tools/environments/local.py tools/file_operations.py tools/mcp_tool.py tests/agent/test_anthropic_oauth_pkce.py tests/agent/test_credential_pool.py tests/cli/test_cli_light_mode.py tests/gateway/test_status_command.py tests/hermes_cli/test_cmd_update.py tests/hermes_cli/test_graphical_browser_detection.py tests/hermes_cli/test_mcp_config.py tests/hermes_cli/test_model_switch_custom_providers.py tests/hermes_cli/test_pip_install_detection.py tests/hermes_cli/test_runtime_provider_resolution.py tests/hermes_cli/test_tui_resume_flow.py tests/hermes_cli/test_update_check.py tests/hermes_cli/test_uv_tool_update.py tests/test_hermes_logging.py tests/tools/test_file_operations.py tests/tools/test_file_operations_edge_cases.py tests/tools/test_file_write_safety.py tests/tools/test_local_env_blocklist.py tests/tools/test_mcp_stability.py
```

Result: `All checks passed!`.

`git diff --check` produced no output.

Supplemental low-risk batch verification:

```bash
uv run --extra dev pytest tests/tools/test_docker_environment.py tests/tools/test_stage2_hook_puid_pgid.py tests/tools/test_skills_sync.py tests/tools/test_mcp_stability.py tests/tools/test_process_registry.py tests/hermes_cli/test_web_server.py::TestPtyWebSocket::test_resize_escape_is_forwarded -q -rs
```

Result: `171 passed`.

```bash
uv run --extra dev ruff check tests/tools/test_docker_environment.py tests/tools/test_stage2_hook_puid_pgid.py tests/tools/test_skills_sync.py tests/tools/test_mcp_stability.py tests/tools/test_process_registry.py tests/hermes_cli/test_web_server.py tools/environments/docker.py tools/skills_sync.py
```

Result: `All checks passed!`.

```bash
bash -n docker/stage2-hook.sh
```

Result: exit `0`.

`git diff --check` produced no output.

Manual Feishu chat-lock port verification:

```bash
uv run --extra dev pytest tests/gateway/test_feishu.py::TestChatLockEviction -q -rs
```

Result: `5 passed`.

```bash
uv run --extra dev pytest tests/gateway/test_feishu.py -q -rs
```

Result: `213 passed`.

```bash
uv run --extra dev ruff check gateway/platforms/feishu.py tests/gateway/test_feishu.py
```

Result: `All checks passed!`.

`git diff --check` produced no output.

Manual file-tools BOM port verification:

```bash
uv run --extra dev pytest tests/tools/test_file_write_safety.py::TestBomHandling -q -rs
```

Result: `9 passed`.

```bash
uv run --extra dev pytest tests/tools/test_file_write_safety.py tests/tools/test_file_operations.py tests/tools/test_file_operations_edge_cases.py tests/tools/test_resolve_path.py -q -rs
```

Result: `150 passed`.

```bash
uv run --extra dev ruff check tools/file_operations.py tests/tools/test_file_write_safety.py
```

Result: `All checks passed!`.

`tests/tools/test_line_ending_preservation.py` was also run and produced
`6 failed, 6 passed`; the failures are existing tool-entry sensitive-path
refusals for macOS `/private/var/...` pytest temp paths, not BOM-layer
regressions.

Manual service `WorkingDirectory` port verification:

```bash
uv run --extra dev pytest tests/hermes_cli/test_gateway_service.py::TestSystemUnitPathRemapping::test_system_unit_has_no_root_paths tests/hermes_cli/test_gateway_service.py::TestServiceWorkingDirIsStable -q -rs
```

Result: `5 passed`.

```bash
uv run --extra dev pytest tests/hermes_cli/test_gateway_service.py -k "WorkingDirectory or stable_working_dir or SystemUnitPathRemapping or HermesHome or launchd or systemd_unit" -q -rs
```

Result: `28 passed, 110 deselected`.

```bash
uv run --extra dev pytest tests/gateway/test_gateway_event_ledger.py tests/gateway/test_hermes_tools_gateway_event.py -q -rs
```

Result: `64 passed`.

```bash
uv run --extra dev ruff check hermes_cli/gateway.py tests/hermes_cli/test_gateway_service.py
```

Result: `All checks passed!`.

Full `tests/hermes_cli/test_gateway_service.py` was also run and produced
`6 failed, 132 passed`; the failures are current macOS/user-systemd D-Bus
preflight availability failures in systemctl routing tests, not
`WorkingDirectory` regressions.

Code-exec approval batch verification:

```bash
uv run --extra dev pytest tests/tools/test_execute_code_approval_cluster.py -q -rs
```

Result: `17 passed`.

```bash
uv run --extra dev pytest tests/run_agent/test_tool_executor_contextvar_propagation.py tests/tools/test_code_execution.py tests/tools/test_code_execution_windows_env.py tests/tools/test_code_execution_modes.py -q -rs
```

Result: `134 passed, 3 skipped`.

```bash
uv run --extra dev pytest tests/tools/test_approval.py tests/gateway/test_session_boundary_security_state.py tests/gateway/test_session_boundary_hooks.py tests/gateway/test_command_bypass_active_session.py -q -rs
```

Result: `242 passed`.

```bash
uv run --extra dev ruff check agent/tool_executor.py tools/thread_context.py tools/approval.py tools/code_execution_tool.py tests/run_agent/test_tool_executor_contextvar_propagation.py tests/tools/test_code_execution_windows_env.py tests/tools/test_execute_code_approval_cluster.py
```

Result: `All checks passed!`.

`git diff --check` produced no output after the implementation batches.
