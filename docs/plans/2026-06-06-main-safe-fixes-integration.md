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

`2765b02021c1e6eb743e2ce2eb359fc66a5aa89e` was attempted and became an empty
patch because the effective plugin-manifest packaging metadata was already
present in the current fork.

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

- `100536134` / `db96fc60d`: topic recovery/session identity changes.
- `655090b3d` / `6a2e3c2d2` / `fd09b2c55`: startup risk warnings and adapter
  access-policy changes, including default-deny semantics.
- `a1cb5fa2c`: service `WorkingDirectory` behavior, adjacent to the NixOS live
  service and internal preflight path.
- `08c0b2241` / `781604ce4` / `51d165a8e`: media extraction and tool-result
  scan semantics, adjacent to gateway ledger event attribution.
- `45bc65abb`: delivery outcome semantics for silence narration filtering.
- `0bfe19ba1` / `44f3e5186`: nested gateway platform config handling.
- `2b16b756a`: post-interrupt model recovery and fallback status behavior.
- `45465b0d5`: reconnect/pause policy for transient network and DNS failures.
- `7379f1755`: planned-stop/takeover marker behavior.
- `91b174038`: Feishu chat-lock LRU. This looks narrow and likely desirable,
  but it still touches the Feishu P0 file and should be ported with Feishu
  gateway tests rather than mixed into this safe-fix batch.

### Tooling and runtime commits requiring separate batches

- `7427b9d58`, `21aeefe5f`, `108397726`, `317184547`, `4bdae3477`: tool-search
  scope and code-exec approval-context changes. These have security value but
  touch tool execution/session context contracts; port them as an approval and
  tool-execution batch.
- `5f84c9144`: UTF-8 BOM handling for file tools. This depends on the file-tool
  behavior around atomic writes and should be tested as a file-tools batch.
- `2334228ec`, `2475244ca`, `c1b2d0917`, `54aa4db1d`: update/uninstall edge
  cases. One update commit conflicted in `hermes_cli/main.py`; port the final
  update behavior as a single update-path batch.
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

Docker-only, Kanban, optional skill catalog, docs-only, release author mapping,
voice/video, and platform-specific changes for unused adapters remain out of
scope for this pass unless the corresponding feature becomes production-relevant.

Special caution: dashboard commits that allow insecure public binds and external
skill-tap commits should be reviewed under a separate security model before
being accepted.

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
