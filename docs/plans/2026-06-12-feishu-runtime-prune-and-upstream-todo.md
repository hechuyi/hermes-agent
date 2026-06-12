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

- [ ] Absorb MCP capability gating (`5affecb44`) and run MCP tool tests.
- [ ] Absorb plugin discovery failed-sweep cache fix (`114e26573`) and run plugin tests.
- [ ] Absorb keyless web-search default fallback (`93764b930`, `32a73010b`) and run web tool/provider tests.
- [ ] Absorb context-compaction stale-task handoff fixes (`d5e2fbf24`, `8f8cad7ec`, `acb2954d8`, `6c752ca3a`) and run context compressor/resume tests.
- [ ] Absorb coding-context terminal persistence fixes (`96cc7ee1`, `ab06ef8ed`) and run coding context plus terminal tests.
- [ ] Consider Discord runtime recovery (`c3464ecf4`) only if non-Feishu platform plugins remain in scope.

Upstream items intentionally deferred:

- [ ] Model directory, model default, custom endpoint onboarding, and model-picker policy changes: blocked by the current "5.5 only" constraint.
- [ ] Desktop/Electron/UI/dashboard/profile large changes: conflicts with Feishu-runtime pruning.
- [ ] Nix/npm large lockfile changes tied to removed `web/`, `ui-tui/`, `website/`, or desktop packages.
