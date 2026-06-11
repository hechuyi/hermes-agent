# Official main intake audit

> Date: 2026-06-11
> Working branch: `fix/live-gateway-hermes-tools`
> Local fork main: `origin/main` at `5921d667855880b0aa2083a50f001748aed52f3e`
> Official main ref: `upstream/main` at `4d22b8293374fd9eaeac75e1f607b20ddea3a1b3`
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

Results:

- `origin/main..upstream/main`: 1228 commits
- `HEAD..upstream/main`: 1493 commits
- `HEAD...upstream/main` right-only after patch-equivalence filtering: 1450 commits

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
