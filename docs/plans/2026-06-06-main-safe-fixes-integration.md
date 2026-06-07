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

### Ntfy echo-loop batch

The ntfy echo-loop batch was absorbed in local commit `0972935ad`:

- `9405cdc8dd0347fee65b554e3aba0d2eab7a7b7f`
- `8055d0f09246555d9a7d9b95295def612e500c70`

Outgoing ntfy gateway sends and standalone cron/`send_message` sends now carry
`X-Tags: hermes-agent`, and inbound ntfy events carrying that tag are ignored
as echoed self-messages. Events with unrelated ntfy tags still dispatch
normally. The port intentionally omitted the unrelated `scripts/release.py`
author-map hunk from upstream.

### Windows profile-wrapper batch

The Windows profile-wrapper batch was absorbed in local commit `c8dadb385`:

- `6312dd8c3a3d2da2c5276c652ea4a21df29ddf47`
- `8836b3a113f8b8781a1935217f008fe67ae8e09f`

Profile aliases now use `where` for Windows collision detection, create
`.bat` wrappers on Windows, remove Windows `.bat` wrappers safely, and route
custom alias names through the same platform-aware wrapper generator instead
of rewriting the file with a POSIX `#!/bin/sh` body.

### LSP Windows shim batch

The LSP Windows wrapper/shim batch was absorbed in local commit `735a642eb`:

- `460771bf0f20ed8f931e17ce9c57a65eaa9d6ec0`
- `296fcdfa52f464feeaa3d345e9d5c89a7727e161`

LSP installer probes now recognize staged Windows `.cmd`, `.exe`, and `.bat`
wrappers, pip installs check native `Scripts/` launchers as well as POSIX
`bin/`, `hermes lsp which` and backend warnings reuse the same binary probe,
and the LSP client wraps `.cmd`/`.bat` shims through `cmd.exe /c` before
spawning on Windows.

### Send-message email target batch

The send-message email target batch was absorbed in local commit `6d38906e8`:

- `d3724c0be68858e9a2816526c5b5e7f5f9f12ebc`
- `bfc4a26032cbc3bab1c33c98d40a17fd7802342c`

Raw email addresses are now explicit `send_message` targets for the email
platform instead of falling through to channel-name resolution, and the
no-home-channel guidance now points email users at `EMAIL_HOME_ADDRESS` rather
than the unused generic `EMAIL_HOME_CHANNEL`. A focused test file was added so
this coverage does not get skipped when optional Telegram dependencies are
absent.

### Vision download retry batch

The vision image-download retry batch was absorbed in local commit `63a33f303`:

- `b4cf114f68da5d1de6b53cdc4a208d270e0654d7`

Image downloads now fail fast for deterministic terminal failures:
non-429 HTTP 4xx responses, website-policy `PermissionError`, and local
validation `ValueError` cases such as oversized images or blocked redirects.
429, 5xx, and unclassified transport-style errors remain retryable. The port is
limited to download retry classification and does not change native vision
routing, auxiliary model resolution, gateway event contracts, or model catalog
customizations.

### CLI/browser low-risk runtime batch

Three independent CLI/browser fixes were absorbed as separate local commits:

- `26b83a5f5f0acf32599f6449b685bec5a136e3d8` in local commit `b52891de5`.
- `92ad7cc62cf030820d1eee9ceabd40f9b4c2cd9e` in local commit `d45402451`.
- `433bffff51ec1a731fabc29637a17d5f4fc9f422` in local commit `eb1bf53f1`.

Terminal focus-in/focus-out reports are now mapped to `Keys.Ignore` at the
prompt_toolkit parser layer and consumed by a no-op key binding before their
visible tails can enter the prompt buffer. The CDP supervisor now retries only
the specific DOM-node serialization failure (`Object reference chain is too
long`) with `returnByValue=false`, while the subprocess fallback converts that
protocol error into guidance to extract a primitive or stringify the value.
Oneshot mode now fails closed with `rc=1` and a real-stderr error when the
agent raises a normal exception, while preserving `KeyboardInterrupt` and
`SystemExit` propagation.

The upstream `scripts/release.py` author-map hunk from `433bffff` was
intentionally omitted as release metadata unrelated to this fork's runtime
behavior.

### Auth/provider runtime batch

The read-only audited auth/provider subset was absorbed as focused local
commits:

- `f6a2ba62611dd92c659df683060174d14425913a` in local commit `806c0912e`.
- `40fcb96585395c6adb58d4d43156a6d0e68522cb` and
  `622e534379fa2f4bdf43e4e6f3d480a74b4106e5` in local commit `b8119df2a`.
- `2062a84000a666c449b9fb7768a4b4e4718e2c88` in local commit `b55418a3e`.
- `6a72af044c44c9a05137bc448bc65ecf0ace5a89` in local commit `c44ec8685`.

xAI OAuth now treats 403 `unauthenticated:bad-credentials` failures as auth
errors, maps `api.x.ai` clients to the `xai-oauth` credential pool, and can
refresh xAI OAuth credentials before retrying. Runtime main custom-provider
metadata now carries `base_url`, `api_key`, and `api_mode` into auxiliary
auto-routing so config-less `custom:<name>` live endpoints do not fall through
to unrelated aggregators. Auxiliary OpenAI-compatible chat calls no longer send
default `max_tokens` / `max_completion_tokens`; only Anthropic Messages wire
keeps mandatory `max_tokens`. Managed gateway availability checks now use a
cached-token peek path and avoid synchronous Nous OAuth refresh, while actual
gateway request/client paths still use the refresh-aware token reader.

This batch does not modify curated model catalogs or the fork's `5.5`
customizations. Nous JWT-only behavior remains deferred separately because this
fork still intentionally retains legacy Nous session-key inference paths.

### xAI schema sanitizer batch

`1386a7e4789c9b886395804e8475a4252217e4ac`
(`fix(xai-sanitize): deepcopy tools_for_api before in-place mutation`) was
manually absorbed in local commit `7cfd1d782`.

xAI Responses requests still strip `pattern`, `format`, and slash-containing
`enum` values from outgoing tool schemas to avoid xAI schema validation
failures. The main-agent and auxiliary Responses paths now deep-copy tool
schemas before invoking the in-place sanitizers, so a first xAI request no
longer permanently removes constraints from the shared `agent.tools` registry
or caller-provided auxiliary tool list. Subsequent non-xAI calls, fallback
paths, and model switches therefore keep the original schema constraints.

This batch does not change the sanitizer contract, provider routing, model
catalogs, gateway event ledgers, media extraction, delivery outcomes, or the
fork's `5.5` customizations.

### Agent summary strict-schema batch

`636ff636d7d819503035b87655d2c7247e84def7`
(`fix(agent): strip schema-foreign keys from max-iterations summary request`)
was manually absorbed in local commit `56819b78d`.

The max-iterations summary path hand-builds Chat Completions messages and
calls `chat.completions.create()` directly. It now mirrors the main transport's
strict-schema cleanup for internal bookkeeping fields: `tool_name`,
`codex_reasoning_items`, `codex_message_items`, and underscore-prefixed Hermes
internal keys are removed from copied API messages before the request is sent.
The original in-memory history remains unchanged, preserving FTS and Codex
reasoning bookkeeping for local state.

This batch is limited to agent summary request sanitation. It does not change
gateway event ledgers, media extraction, delivery outcomes, compression
semantics, provider routing, model catalogs, Nous legacy authentication, or the
fork's `5.5` customizations.

### Web plugin discovery batch

`6e179c44b16d0149f5fa014be29490aff15a6b20`
(`fix(web): ensure plugin discovery before web_*_tool registry lookups`) was
manually absorbed in local commit `0dd44f223`.

`web_search_tool` and `web_extract_tool` now trigger idempotent plugin
discovery before consulting `agent.web_search_registry`. This prevents
cold-start subprocesses, delegate children, and standalone imports from seeing
an empty web registry and returning misleading "No web provider configured"
errors when a configured plugin-backed provider is available.

This batch is limited to web tool dispatch registration. It does not change
provider implementation behavior, gateway event ledgers, media extraction,
delivery outcomes, model catalogs, Nous legacy authentication, or the fork's
`5.5` customizations.

### Tool executor interrupt cleanup batch

`bede3cf12d1492043f4ca604fdb2158ffd6bc619`
(`fix(tools): wrap _run_tool cleanup in finally to prevent interrupt state leak`)
was manually absorbed in local commit `730cf589d`.

Concurrent tool worker cleanup now runs in a `finally` block around
`_invoke_tool`, so `BaseException` subclasses such as cancellation-style
failures still remove the recycled worker thread id from
`_tool_worker_threads` and clear its per-thread interrupt bit. This prevents a
stale interrupt marker from poisoning the next tool scheduled on the same
`ThreadPoolExecutor` worker.

This batch is limited to agent tool executor cleanup. It does not change tool
approval semantics, gateway event ledgers, media extraction, delivery outcomes,
model catalogs, Nous legacy authentication, or the fork's `5.5`
customizations.

### Voice PipeWire audio-probe batch

`c834624f7de8136b0010f0771ee7a89dc5e92942`
(`fix(voice): honor PIPEWIRE_REMOTE in PortAudio fallback checks`) was manually
absorbed in local commit `02f1d489f`.

Voice environment detection already recognized `PIPEWIRE_REMOTE` during the
container-level forwarding check. It now also treats `PIPEWIRE_REMOTE` as host
audio forwarding when PortAudio returns an empty device list or raises during
device probing, matching the existing `PULSE_SERVER` fallback behavior. Docker
and Podman voice mode therefore remains available when PipeWire is forwarded
but PortAudio cannot enumerate devices inside the container.

This batch is limited to CLI voice environment detection. It does not change
gateway voice-message handling, media extraction, delivery outcomes, provider
routing, model catalogs, Nous legacy authentication, or the fork's `5.5`
customizations.

### CLI MCP startup batch

`0c6e133c0434ec856d4aea2b08f216f36c0e7dac`
(`perf(cli): stop eager MCP discovery from blocking agent-capable startup`) was
manually absorbed in local commit `e53a6fb11`.

CLI chat/rl startup now launches MCP discovery in a shared background thread
when MCP servers are configured, while TUI chat and entrypoints with dedicated
runtime startup paths (`acp`, `gateway run`, `cron run` / `cron tick`) avoid
duplicating MCP bootstrap work. The first tool snapshot and agent construction
briefly wait for the background discovery thread, preserving tool availability
without allowing slow or dead MCP servers to block interactive startup.

This batch is limited to CLI startup and tool snapshot timing. It does not
change gateway event ledgers, media extraction, delivery outcomes, Nous
authentication behavior, model catalogs, or the fork's `5.5` customizations.

### Ghostty Ctrl+J newline batch

`cf8862cfa316626ab4e673b9e04e0105a937f9bd`
(`fix: preserve Ctrl+J newlines in Ghostty`) was manually absorbed in local
commit `2b81062c9`.

Prompt-toolkit CLI key binding and the Ink TUI input handler now recognize
Ghostty session markers, including `GHOSTTY_RESOURCES_DIR`/`GHOSTTY_BIN_DIR`,
`TERM=xterm-ghostty`, and `TERM_PROGRAM=ghostty`, as terminals where bare LF
can represent Ctrl+J/Ctrl+Enter newline input. The CLI leaves `c-j` unbound in
those sessions so the newline binding can fire, while bare local POSIX
LF-compatible prompts still bind `c-j` to submit. The TUI mirrors that
detection and treats bare LF as newline only in the preserved environments.

This batch is limited to interactive input compatibility. It does not change
gateway event ledgers, media extraction, delivery outcomes, provider routing,
model catalogs, Nous legacy authentication, or the fork's `5.5`
customizations.

### TUI clipboard and CLI usability batch

The following low-risk interactive usability fixes were manually absorbed as
focused local commits:

- `64998fa93e2bd52ee191701ea50c0febcc8e3dc6` and
  `16882cfded90b8c41ff18000c56a84d7f17628b7` in local commit
  `92411c011`.
- `edfdc776649cd50637d8aa3a35b584c4458416ef` and
  `04de307d62277998ee8e52dfa4da59b539917721` in local commit
  `3e88f6de6`.
- `f32b66c758ef16d96bedcdce62ed6a397e741103` in local commit
  `3352444a6`.

PowerShell clipboard writes from the TUI now pass UTF-8 text through a
base64-encoded command argument instead of PowerShell's stdin decoding path,
preserving CJK and emoji text on Windows/WSL. A bare number submitted
immediately after bare `/resume` now selects that displayed session index as a
one-shot prompt instead of being sent to the agent as chat, and inline `/steer`
or `/model` submissions now invalidate the prompt after clearing the input
buffer so submitted text does not visually linger. `hermes plugins list` now
supports filtered, plain, and JSON output, plus better keyboard paging in the
interactive plugin picker.

This batch is limited to interactive CLI/TUI usability and plugin-list
presentation. It does not change gateway event ledgers, media extraction,
delivery outcomes, provider routing, model catalogs, Nous legacy
authentication, or the fork's `5.5` customizations.

### UI diagnostics and Gmail casing batch

Four independent low-risk upstream fixes were manually absorbed in local commit
`1d0b4146e`:

- `28bb7e0a8e8d9218d593eea6c8b5941d225814a6`
- `2fc2280e63964ad96419f1d532a308eb034d42db`
- `bb79bcde6103c564dacb2d796fe8fe8b775f1b18`
- `8bd00607dc53fabd96e95917b77c9a13d6ead6ba`

The web Tailwind theme bridge now exports `--theme-font-sans` and
`--theme-font-mono` through Tailwind's `--font-*` variables. Short-terminal
clarify panels now reserve space for choices before question text so selectable
options are not clipped. `hermes doctor` now detects source-tree drift between
`pyproject.toml` and `hermes_cli.__version__`. The Google Workspace Gmail
helper now normalizes fetched Gmail header names case-insensitively while
emitting conventional MIME header casing for sent/replied messages.

This batch is limited to UI presentation, diagnostics, and Google Workspace
skill helper behavior. It does not change gateway event ledgers, media
extraction, delivery outcomes, provider routing, model catalogs, Nous legacy
authentication, or the fork's `5.5` customizations.

### Browser runtime and TUI gateway test-isolation batch

The browser/Codex runtime and TUI gateway test-isolation batch was manually
absorbed in local commit `ce4f6d805`:

- `a0fc3df878e5d99125d3bbcbaeda6a4966e192c1`
- `73d73f1f0d38ac856bc114b16c659830acdc2f6e`
- `300140e006bd1e356db69772b5ba35914b9d4008`
- `4fd8521e44e920fbf545b408ea8727423436cad4`

Camofox page navigation can now optionally rewrite loopback page URLs to a
Docker host alias while leaving the Camofox control URL unchanged. The Codex
no-byte TTFB watchdog default is relaxed from 12 seconds to 120 seconds so
subscription-backed requests are not killed during normal admission or prompt
prefill. TUI gateway tests now avoid module reload teardown and isolate the
process completion queue; the port also restores the JSON-RPC method registry
from a snapshot during teardown so tests that monkeypatch `_methods` do not
pollute later modules. A browser-manage test assertion was made portable across
macOS fallback launch guidance and no-browser environments.

This batch is limited to browser tool runtime configuration, Codex streaming
watchdog timing, and tests. It does not change gateway event ledgers, media
extraction, delivery outcomes, provider routing, model catalogs, Nous legacy
authentication, or the fork's `5.5` customizations.

### Low-risk documentation cleanup batch

The following documentation and docstring-only fixes were absorbed in local
commit `395056250`:

- `ae9dfa510e668552a804811d18017d1ad71ce157`
- `0673638560a43b1affce9ceecdc60c2758aae7c0`
- `6891e05e78b67beac3ef4f2f5acbdbd24f4e9e7b`
- `d86710528a0245e2638a801f46551dad35230d9b`
- `03bdeaa87697dbfc12d3733aa904a2b3a85b4653`
- `053969fd533a2aea9fe402cb441b531164003f6d`
- `3f0d44af8ae380996057b620afeae258af830634`

This batch fixes wording, dead documentation links, GitHub organization links,
session image base paths, SimpleX download URL shape, invalid `hermes config
get` examples, and Browserbase timeout units in docs/docstrings. It does not
change runtime behavior, gateway event ledgers, media extraction, delivery
outcomes, provider routing, model catalogs, Nous legacy authentication, or the
fork's `5.5` customizations.

`988cf1743be74e939241e9cbbb7695bda0fcc606` was intentionally not absorbed:
the patch only replaces an external video destination in quickstart
documentation, which is out of scope under the workspace's advertising and
external-link injection discipline.

### Configuration and security documentation batch

The configuration/security documentation batch was absorbed in local commit
`58f90d92d`:

- `90f0f32eae0e94323377db0b4dd28a54292c6c2a`
- `2410e1139547abcd5a6705d2a5f3297633f454ff`
- `c692000a57df41c953967f37eb34ed9b593f233c`
- `62e81b2d9b30f2a4c882f57732b5e213b6250c42`
- `2520c9ad68af3b1760f5646936fdc86d741b6f74`
- `c0b17b3c0cb15fa92bd348162e6bc58d6d6336cd`
- `b922e3ff93c457e6079aea8637ffdc7a15dc15b8`
- `ee0a9bf7c702d6369d03a9d55b4c3e93f52b748a`
- `a2d3cff53feb060eec3115fe3fec1e5c81bca8c3`
- `aef04b2b537fbd37b465a4dccc45096af5e73229`
- `3625dbb8442c357b1995e9fa750498dd697ba38b`
- `119390a2a1eeb47a9b59d29e4158cfd31ae63e1f`
- `175885218e82f99fb3cb58335640b7d4f4b7c2f8`
- `eff4626747ae8a32bbda192883121c6c62ca18fb`
- `549a69a925a799001cab63a4244b8e486c8c2ab4`
- `2159d2a72964865d047b1b46f6347be1e2a74e9f`
- `860cf28dabbaf93459a778a835edbc3663e381c5`

This batch adds Docker network-egress isolation documentation and aligns
documentation for xAI OAuth manual paste, Windows WSL shortcuts, Reminders
alarm timing, Weixin allowed users, prompt precedence, update flags, secret
redaction defaults, `MESSAGING_CWD` deprecation, fallback provider examples,
curator provenance, credential-pool usage-limit rotation, and compression
threshold wording. The only Python change is a docstring correction in
`agent/redact.py`; Nix/config changes are option/example descriptions. It does
not change runtime behavior, gateway event ledgers, media extraction, delivery
outcomes, provider routing, model catalogs, Nous legacy authentication, or the
fork's `5.5` customizations.

### CLI prompt-size and TUI MCP startup batch

The prompt-size diagnostic and TUI MCP startup batch was absorbed in local
commit `b43f34f42`:

- `61268ff7a9be93673361e433cbf2e775798a13ae`
- `cbf851ae1d7251708eed16013f49e47e665d2c0f`

`hermes prompt-size` now reports the fixed prompt budget for a fresh session,
including the assembled system prompt, skills index, memory/profile blocks,
prompt tiers, and tool schema JSON. It runs offline without an API call. TUI
gateway startup now launches MCP discovery in a background daemon thread when
MCP servers are configured, emits `gateway.ready` without waiting for slow or
dead servers, and briefly joins the discovery thread before the first agent
build so fast-starting MCP servers can still land in the initial tool snapshot.
`/reload-mcp` also rebuilds the cached agent tool snapshot after rediscovery.

This batch is limited to CLI diagnostics and TUI gateway MCP startup/tool
snapshot timing. It does not change gateway event ledgers, media extraction,
delivery outcomes, provider routing, model catalogs, Nous legacy
authentication, or the fork's `5.5` customizations.

### CLI process-title batch

`84ee80eb5d94838dd5b2c3c74a0fbe53dfb48c28`
(`feat: set process title to 'hermes' in ps/top/htop`) was manually absorbed in
local commit `588342a5e20a340f6eab57ddd673d937993c49d9`.

The local port sets the process title to `hermes` at CLI startup on a
best-effort basis. It prefers the optional `setproctitle` package and falls
back to platform libc calls for Linux and macOS, while swallowing failures so
missing optional dependencies or unsupported libc calls cannot affect command
startup. This is a cosmetic observability change only; it does not change
gateway event ledgers, media extraction, delivery outcomes, provider routing,
model catalogs, Nous legacy authentication, or the fork's `5.5`
customizations.

### Concurrent checkpoint guardrail batch

`6baf0016bebe060f055b5466c6ea604f628d1217`
(`fix(run_agent): gate concurrent checkpoint preflight on block_result`) was
manually absorbed in local commit `a9f95f920`.

The local port preserves the upstream invariant that already existed on the
sequential path: plugin- or guardrail-blocked tools must not mutate checkpoint
state before being rejected. In the concurrent path, checkpoint preflight for
`write_file`, `patch`, and destructive `terminal` commands now runs only after
block evaluation and only when `block_result is None`.

This is limited to agent tool-executor bookkeeping. It does not change gateway
event ledgers, media extraction, delivery outcomes, provider routing, model
catalogs, Nous legacy authentication, or the fork's `5.5` customizations.

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
- `41ff6e593`, `7e958dafc`, `4e4984a`, `95cf8f984`, `a22c25000`: Nous
  JWT-only behavior. Defer until there is an explicit decision to remove this
  fork's retained legacy Nous session-key inference paths.
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

1. `08c0b2241`, `781604ce4`, `51d165a8e`: media extraction and tool-result
   scan semantics need a separate gateway ledger attribution review.
2. `5ad2b4c6d`, `97ecfa0fc`, `4fa20f9a8`, `a7421dc7d`, `38695254f`,
   `904c0b479`, `794519c6a`: session/state/SQLite/FTS work should be reviewed
   as a state-schema and migration batch.
3. `a30480bd2`, `db2ce9e7d`, `e38b0b55d`, `020601d41`, `56b8dccf2`,
   `42bbd221e`: compression/resume behavior should be reviewed as a
   conversation-compression batch.

The Docker reuse/orphan-reaper group, tool-search base/scoping group,
Nous JWT-only decision group, session/state group, compression/resume group,
and MEDIA extraction group should remain separate batches because each changes
a runtime contract rather than just a local implementation detail.

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

Vision download retry batch verification:

```bash
uv run --extra dev pytest tests/tools/test_vision_tools.py::TestDownloadRetryClassification -q -rs
```

Result: `3 passed`.

```bash
uv run --extra dev pytest tests/tools/test_vision_tools.py -q -rs
```

Result: `67 passed, 6 skipped, 1 warning`. The skipped tests require Pillow.

```bash
uv run --extra dev ruff check tools/vision_tools.py tests/tools/test_vision_tools.py
```

Result: `All checks passed!`.

`git diff --check` produced no output.

CLI/browser low-risk runtime batch verification:

```bash
uv run --extra dev pytest tests/cli/test_cli_terminal_shortcuts.py -q -rs
```

Result: `4 passed`.

```bash
uv run --extra dev pytest tests/cli/test_cli_shift_enter_newline.py tests/cli/test_ctrl_enter_newline.py -q -rs
```

Result: `15 passed`.

```bash
uv run --extra dev pytest tests/tools/test_browser_eval_supervisor_path.py -q -rs
```

Result: `17 passed`.

```bash
uv run --extra dev pytest tests/tools/test_browser_console.py -q -rs
```

Result: `27 passed, 1 warning`.

```bash
uv run --extra dev pytest tests/hermes_cli/test_tui_resume_flow.py -q -k 'oneshot_fails_closed_on_empty_final_response or oneshot_prints_nonempty_final_response or oneshot_fails_closed_on_agent_exception or oneshot_reraises_control_flow_exceptions or oneshot_rejects_invalid_only_toolsets'
```

Result: `6 passed, 40 deselected`.

```bash
uv run --extra dev pytest tests/hermes_cli/test_tui_resume_flow.py -q -rs
```

Result: `46 passed`.

```bash
uv run --extra dev ruff check cli.py hermes_cli/pt_input_extras.py tests/cli/test_cli_terminal_shortcuts.py tests/cli/test_cli_shift_enter_newline.py tests/cli/test_ctrl_enter_newline.py
```

Result: `All checks passed!`.

```bash
uv run --extra dev ruff check tools/browser_supervisor.py tools/browser_tool.py tests/tools/test_browser_eval_supervisor_path.py tests/tools/test_browser_console.py
```

Result: `All checks passed!`.

```bash
uv run --extra dev ruff check hermes_cli/oneshot.py tests/hermes_cli/test_tui_resume_flow.py
```

Result: `All checks passed!`.

`git diff --check` produced no output for each code batch.

Auth/provider runtime batch verification:

```bash
uv run --extra dev pytest tests/agent/test_auxiliary_client_xai_oauth_recovery.py tests/agent/test_set_runtime_main_custom_provider.py -q -rs
```

Result: `14 passed`.

```bash
uv run --extra dev pytest tests/agent/test_auxiliary_client.py::TestBuildCallKwargsMaxTokens tests/agent/test_unsupported_temperature_retry.py -q -rs
```

Result: `27 passed, 1 warning`.

```bash
uv run --extra dev pytest tests/tools/test_managed_tool_gateway.py tests/tools/test_managed_browserbase_and_modal.py tests/tools/test_web_tools_config.py -q -rs
```

Result: `67 passed`.

```bash
uv run --extra dev pytest tests/agent/test_auxiliary_client.py tests/gateway/test_model_command_custom_providers.py tests/gateway/test_session_model_override_routing.py -q -rs
```

Result: `195 passed`.

```bash
uv run --extra dev ruff check agent/auxiliary_client.py agent/conversation_loop.py tests/agent/test_auxiliary_client.py tests/agent/test_auxiliary_client_xai_oauth_recovery.py tests/agent/test_set_runtime_main_custom_provider.py tests/agent/test_unsupported_temperature_retry.py tests/gateway/test_model_command_custom_providers.py tests/gateway/test_session_model_override_routing.py
```

Result: `All checks passed!`.

```bash
uv run --extra dev ruff check tools/managed_tool_gateway.py tools/web_tools.py plugins/browser/browser_use/provider.py plugins/web/firecrawl/provider.py tests/tools/test_managed_tool_gateway.py tests/tools/test_managed_browserbase_and_modal.py tests/tools/test_web_tools_config.py
```

Result: `All checks passed!`.

`git diff --check` produced no output.

xAI schema sanitizer batch verification:

```bash
uv run --extra dev pytest tests/run_agent/test_run_agent_codex_responses.py::test_build_api_kwargs_xai_strips_schema_from_outgoing_request tests/run_agent/test_run_agent_codex_responses.py::test_build_api_kwargs_xai_does_not_mutate_agent_tools tests/run_agent/test_run_agent_codex_responses.py::test_build_api_kwargs_xai_is_idempotent_across_repeated_calls tests/agent/test_auxiliary_client.py::TestCodexAdapterReasoningTranslation::test_tool_schema_sanitization_does_not_mutate_input_tools -q -rs
```

Result: `4 passed, 1 warning`.

```bash
uv run --extra dev pytest tests/run_agent/test_run_agent_codex_responses.py tests/tools/test_schema_sanitizer.py -q -rs
```

Result: `111 passed, 1 warning`.

```bash
uv run --extra dev pytest tests/agent/test_auxiliary_client.py -q -rs
```

Result: `191 passed, 1 warning`.

```bash
uv run --extra dev ruff check agent/chat_completion_helpers.py agent/auxiliary_client.py tests/run_agent/test_run_agent_codex_responses.py tests/agent/test_auxiliary_client.py
```

Result: `All checks passed!`.

`git diff --check` produced no output.

Agent summary strict-schema batch verification:

```bash
uv run --extra dev pytest tests/run_agent/test_run_agent.py::TestHandleMaxIterations::test_summary_strips_strict_schema_foreign_fields -q -rs
```

Result: `1 passed, 1 warning`.

```bash
uv run --extra dev pytest tests/run_agent/test_run_agent.py::TestHandleMaxIterations -q -rs
```

Result: `10 passed, 1 warning`.

```bash
uv run --extra dev ruff check agent/chat_completion_helpers.py tests/run_agent/test_run_agent.py
```

Result: `All checks passed!`.

`git diff --check` produced no output.

Web plugin discovery batch verification:

```bash
uv run --extra dev pytest tests/tools/test_web_providers.py::TestDispatchersTriggerPluginDiscovery -q -rs
```

Result: `2 passed, 1 warning`.

```bash
uv run --extra dev pytest tests/tools/test_web_providers.py tests/tools/test_web_tools_config.py -q -rs
```

Result: `65 passed, 1 warning`.

```bash
uv run --extra dev ruff check tools/web_tools.py tests/tools/test_web_providers.py
```

Result: `All checks passed!`.

`git diff --check` produced no output.

Tool executor interrupt cleanup batch verification:

```bash
uv run --extra dev pytest tests/tools/test_interrupt.py::TestRunToolCleanupOnBaseException::test_worker_interrupt_state_is_cleared_when_tool_raises_base_exception -q -rs
```

Result: `1 passed, 1 warning`.

```bash
uv run --extra dev pytest tests/tools/test_interrupt.py tests/run_agent/test_tool_executor_contextvar_propagation.py -q -rs
```

Result: `12 passed, 1 warning`.

```bash
uv run --extra dev ruff check agent/tool_executor.py tests/tools/test_interrupt.py
```

Result: `All checks passed!`.

`git diff --check` produced no output.

Voice PipeWire audio-probe batch verification:

```bash
uv run --extra dev pytest tests/tools/test_voice_mode.py::TestDetectAudioEnvironment::test_docker_with_pipewire_remote_and_no_devices_allows_voice tests/tools/test_voice_mode.py::TestDetectAudioEnvironment::test_docker_with_pipewire_remote_and_query_failure_allows_voice -q -rs
```

Result: `2 passed`.

```bash
uv run --extra dev pytest tests/tools/test_voice_mode.py -q -rs
```

Result: `67 passed`.

```bash
uv run --extra dev ruff check tools/voice_mode.py tests/tools/test_voice_mode.py
```

Result: `All checks passed!`.

`git diff --check` produced no output.

CLI MCP startup batch verification:

```bash
uv run --extra dev pytest tests/hermes_cli/test_mcp_startup.py tests/cli/test_cli_light_mode.py -q -rs
```

Result: `24 passed`.

```bash
uv run --extra dev ruff check cli.py hermes_cli/main.py hermes_cli/mcp_startup.py tests/hermes_cli/test_mcp_startup.py
```

Result: `All checks passed!`.

`git diff --check` produced no output.

Ghostty Ctrl+J newline batch verification:

```bash
uv run --extra dev pytest tests/cli/test_ctrl_enter_newline.py tests/cli/test_cli_init.py::TestPromptToolkitTerminalCompatibility -q -rs
```

Result: `12 passed`.

```bash
uv run --extra dev ruff check cli.py tests/cli/test_cli_init.py tests/cli/test_ctrl_enter_newline.py
```

Result: `All checks passed!`.

```bash
cd ui-tui && npm test -- --run src/__tests__/textInputPassThrough.test.ts
```

Result: `1 passed`, `6 passed`.

```bash
cd ui-tui && npx eslint src/components/textInput.tsx src/__tests__/textInputPassThrough.test.ts
```

Result: exit `0` with one existing warning in
`src/components/textInput.tsx` from `react-compiler/react-compiler`; no errors.

```bash
cd ui-tui && npm run type-check
```

Result: failed on pre-existing TypeScript errors in
`packages/hermes-ink/src/utils/execFileNoThrow.ts`; that file has no diff in
this batch.

`git diff --check` produced no output.

TUI clipboard UTF-8 batch verification:

```bash
cd ui-tui && npm test -- --run src/__tests__/clipboard.test.ts
```

Result: `1 passed`, `19 passed`.

```bash
cd ui-tui && npx eslint src/lib/clipboard.ts src/__tests__/clipboard.test.ts
```

Result: exit `0`.

`git diff --check` and `git diff --cached --check` produced no output.

CLI resume/repaint batch verification:

```bash
uv run --extra dev pytest tests/cli/test_cli_resume_command.py tests/cli/test_steer_inline_repaint_34569.py -q -rs
```

Result: `13 passed`.

```bash
uv run --extra dev ruff check cli.py tests/cli/test_cli_resume_command.py tests/cli/test_steer_inline_repaint_34569.py
```

Result: `All checks passed!`.

`git diff --check` and `git diff --cached --check` produced no output.

Plugins list usability batch verification:

```bash
uv run --extra dev pytest tests/hermes_cli/test_plugins_cmd_list.py tests/hermes_cli/test_plugins_cmd.py -q -rs
```

Result: `75 passed, 1 warning`.

```bash
uv run --extra dev ruff check hermes_cli/main.py hermes_cli/plugins_cmd.py tests/hermes_cli/test_plugins_cmd_list.py
```

Result: `All checks passed!`.

`git diff --check` and `git diff --cached --check` produced no output.

UI diagnostics and Gmail casing batch verification:

```bash
uv run --extra dev pytest tests/skills/test_google_workspace_api.py -q -rs
```

Result: `16 passed`.

```bash
uv run --extra dev pytest tests/hermes_cli/test_doctor.py -q -rs
```

Result: `59 passed, 1 warning`.

```bash
uv run --extra dev pytest tests/cli/test_cli_approval_ui.py -q -rs
```

Result: `11 passed`.

```bash
uv run --extra dev ruff check cli.py hermes_cli/doctor.py skills/productivity/google-workspace/scripts/google_api.py tests/skills/test_google_workspace_api.py
```

Result: `All checks passed!`.

```bash
uv run --extra dev python -m py_compile cli.py hermes_cli/doctor.py skills/productivity/google-workspace/scripts/google_api.py
```

Result: exit `0`.

```bash
cd web && npm run build
```

Result: failed before build because local `web/node_modules` is absent and
`tsc` was not found; this is an environment/dependency availability failure,
not a TypeScript or CSS compilation result.

`git diff --check` and `git diff --cached --check` produced no output.

Browser runtime and TUI gateway test-isolation batch verification:

```bash
uv run --extra dev pytest tests/tools/test_browser_camofox.py tests/agent/test_codex_ttfb_watchdog.py -q -rs
```

Result: `37 passed, 1 warning`.

```bash
uv run --extra dev pytest tests/tui_gateway/test_goal_command.py tests/tui_gateway/test_protocol.py tests/tui_gateway/test_review_summary_callback.py tests/test_tui_gateway_server.py -q -rs
```

Result after restoring `_methods` from a fixture snapshot and making the
browser-manage launch hint assertion platform-aware: `252 passed, 17 warnings`.

```bash
uv run --extra dev ruff check agent/chat_completion_helpers.py cli.py hermes_cli/config.py tools/browser_camofox.py tests/agent/test_codex_ttfb_watchdog.py tests/tools/test_browser_camofox.py tests/tui_gateway/test_goal_command.py tests/tui_gateway/test_protocol.py tests/tui_gateway/test_review_summary_callback.py tests/test_tui_gateway_server.py
```

Result: `All checks passed!`.

`git diff --check` and `git diff --cached --check` produced no output.

Low-risk documentation cleanup batch verification:

```bash
uv run --extra dev ruff check plugins/browser/browserbase/provider.py tools/browser_tool.py
```

Result: `All checks passed!`.

```bash
uv run --extra dev python -m py_compile plugins/browser/browserbase/provider.py tools/browser_tool.py
```

Result: exit `0`.

`git diff --check` and `git diff --cached --check` produced no output.

Configuration and security documentation batch verification:

```bash
uv run --extra dev ruff check agent/redact.py
```

Result: `All checks passed!`.

```bash
uv run --extra dev python -m py_compile agent/redact.py
```

Result: exit `0`.

```bash
git diff --cached | rg -n "^\\+.*(discord|youtube|youtu\\.be|invite|邀请码|群|福利|备用网址|telegram|t\\.me|join)" -i
```

Result: only platform/domain examples in network-egress and migration-setting
documentation matched; no external video, invite, or promotional destination
was added.

`git diff --check` and `git diff --cached --check` produced no output.

CLI prompt-size and TUI MCP startup batch verification:

```bash
uv run --extra dev pytest tests/hermes_cli/test_prompt_size.py tests/tui_gateway/test_wait_for_mcp_discovery.py -q -rs
```

Result: `11 passed, 1 warning`.

```bash
uv run --extra dev pytest tests/tui_gateway/test_goal_command.py tests/tui_gateway/test_protocol.py tests/tui_gateway/test_review_summary_callback.py tests/test_tui_gateway_server.py -q -rs
```

Result: `252 passed, 17 warnings`.

```bash
uv run --extra dev ruff check hermes_cli/banner.py hermes_cli/main.py hermes_cli/prompt_size.py tests/hermes_cli/test_prompt_size.py tests/tui_gateway/test_wait_for_mcp_discovery.py tui_gateway/entry.py tui_gateway/server.py
```

Result: `All checks passed!`.

```bash
uv run --extra dev python -m py_compile hermes_cli/banner.py hermes_cli/main.py hermes_cli/prompt_size.py tui_gateway/entry.py tui_gateway/server.py
```

Result: exit `0`.

`git diff --check` and `git diff --cached --check` produced no output.

CLI process-title batch verification:

```bash
uv run --extra dev pytest tests/hermes_cli/test_process_title.py -q -rs
```

Result: `3 passed`.

```bash
uv run --extra dev ruff check hermes_cli/main.py tests/hermes_cli/test_process_title.py
```

Result: `All checks passed!`.

```bash
uv run --extra dev python -m py_compile hermes_cli/main.py tests/hermes_cli/test_process_title.py
```

Result: exit `0`.

`git diff --check` produced no output.

Concurrent checkpoint guardrail batch verification:

Red test before implementation:

```bash
uv run --extra dev pytest tests/run_agent/test_run_agent.py -q -rs -k "concurrent_blocked_write_skips_checkpoint or concurrent_blocked_patch_skips_checkpoint or concurrent_blocked_terminal_skips_checkpoint or concurrent_blocked_write_does_not_steal_slot_from_allowed_write"
```

Result before the code change: `4 failed`; each failure showed
`ensure_checkpoint` called before the blocked tool was rejected.

Post-fix focused verification:

```bash
uv run --extra dev pytest tests/run_agent/test_run_agent.py -q -rs -k "concurrent_blocked_write_skips_checkpoint or concurrent_blocked_patch_skips_checkpoint or concurrent_blocked_terminal_skips_checkpoint or concurrent_blocked_write_does_not_steal_slot_from_allowed_write or sequential_blocked_tool_skips_checkpoints_and_callbacks"
```

Result: `5 passed, 345 deselected, 1 warning`.

```bash
uv run --extra dev pytest tests/run_agent/test_run_agent.py -q -rs
```

Result: `350 passed, 1 warning`.

```bash
uv run --extra dev ruff check agent/tool_executor.py tests/run_agent/test_run_agent.py
```

Result: `All checks passed!`.

```bash
uv run --extra dev python -m py_compile agent/tool_executor.py tests/run_agent/test_run_agent.py
```

Result: exit `0`.

`git diff --check` produced no output.

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
