# Feishu Runtime Prune And Upstream Intake Todo

Goal: keep this fork focused on the Feishu-based Hermes runtime while continuing graded intake from official `upstream/main`.

Current low-risk prune batch:

- [x] Remove tracked frontend/TUI/docs-site source trees: `web/`, `ui-tui/`, `tui_gateway/`, `website/`.
- [x] Remove frontend/TUI/docs-site CI workflows and generated local residues.
- [x] Remove wheel package-data for `web_dist`, `tui_dist`, and dashboard plugin static `dist` assets.
- [x] Remove `tui_gateway` from Python package discovery and delete tests that directly target it.
- [x] Remove Docker, Nix, install-script, and PyPI publish dependencies on deleted frontend/TUI/docs-site assets.
- [x] Keep `tools/web_tools.py` and `plugins/web/*`; these are web search/extract tools, not the removed dashboard frontend.
- [x] Keep the optional `web` extra for now, but remove it from default `[all]` and `termux-all` install profiles.

High-risk prune items requiring explicit confirmation:

- [ ] Delete or disable `hermes dashboard` command, `hermes_cli/web_server.py`, dashboard auth providers, and dashboard API tests.
- [ ] Delete Docker s6 dashboard service wiring under `docker/s6-rc.d/dashboard` and `docker/s6-rc.d/user/contents.d/dashboard`.
- [ ] Delete dashboard plugin backend APIs (`plugins/*/dashboard/plugin_api.py`) and related Kanban/Achievements backend tests.
- [ ] Remove remaining CLI `--tui` parser/launcher paths in `hermes_cli/main.py` and dashboard embedded PTY routes.
- [ ] Decide whether model-catalog and skills-index publication should move somewhere other than the removed `website/static/api/*` path. Do not change model defaults or model catalog policy without confirmation.

Upstream intake todo:

- [x] Absorb MCP capability gating (`5affecb44`) and run MCP tool tests.
- [x] Absorb plugin discovery failed-sweep cache fix (`114e26573`) and run plugin tests.
- [x] Absorb bundled web-provider discovery fallback from `93764b930`/`32a73010b` and run web tool/provider tests. Scope note: this guarantees provider registration after a failed plugin sweep; it does not by itself import the larger Parallel free-MCP/keyless runtime from `e0e257171`.
- [x] Absorb context-compaction stale-task handoff fixes (`d5e2fbf24`, `8f8cad7ec`, `acb2954d8`, `6c752ca3a`) and run context compressor/resume tests.
- [x] Absorb terminal persistence guidance/test subset from `ab06ef8ed` and run terminal tests. `96cc7ee1` and the `agent/coding_context.py` part of `ab06ef8ed` are not applicable to the current fork because that coding-context module is absent.
- [x] Absorb Parallel keyless/free-MCP web runtime (`e0e257171`) plus follow-ups (`0a5762c78`, `383d44bc9`, `2ee8c983c`, `7df81d055`) while retaining bundled provider fallback and Feishu default toolset recovery.
- [ ] Consider Discord runtime recovery (`c3464ecf4`) only if non-Feishu platform plugins remain in scope.

Upstream items intentionally deferred:

- [ ] Model directory, model default, custom endpoint onboarding, and model-picker policy changes: preserve model/catalog compatibility. `gpt-5.5` is the current operating channel, not a repository policy freeze.
- [ ] Desktop/Electron/UI/dashboard/profile large changes: conflicts with Feishu-runtime pruning.
- [ ] Nix/npm large lockfile changes tied to removed `web/`, `ui-tui/`, `website/`, or desktop packages.
