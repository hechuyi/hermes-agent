# Hermes gateway-event/preflight internalization audit

> Date: 2026-06-06
> Repository: `hermes-agent-live-gateway`
> Branch: `fix/live-gateway-hermes-tools`
> Runtime fix: `2255058e9bc16250d6562d3c349e10787ec28f09`
> Audit refresh: Base maintenance and final verification evidence added on 2026-06-06

## 审计范围

本记录用于闭环本轮 Hermes live gateway 的 `gateway-event` / `preflight`
内化工作。技术目标是：在自有 Hermes fork 内化旧 external
`hermes-tools gateway-event/preflight` 能力，使 live gateway 不再在运行路径上外挂
`hermes-tools`。本记录只沉淀工程事实、风险判断、修复链路、审查结论、部署证据和剩余边界；
不更新任务卡片，也不触碰 `rtoc-nixos-infra`。本轮运维 Base 维护已作为外部台账事实另行完成，
记录见下文“多维表格维护证据”。

本轮变更的直接运行边界是 live gateway 的事件账本和 preflight。`task` /
`status-card` internalization 仍按用户指令暂停，不纳入本轮完成条件；多维表格维护已完成，
因此不再构成本轮 goal 的阻断项。

## 目标与根因

旧路径把 gateway event ledger 与 preflight 能力放在 external `hermes-tools`
一侧，live gateway 的启动前检查和事件幂等、入站去重、投递生命周期、Feishu ack、
stale pending scan、session route guard 等关键语义由外部工具链承载。这个边界在开发期可用，
但在 live service 上形成了不必要的运行时耦合：服务能否完成自检不应取决于额外的
`hermes-tools` 可执行文件、shell wrapper 或 legacy state 目录是否仍然可用。

本轮内化的根因不是单个 systemd 单元参数错误，而是能力归属错误：live gateway
自身已经拥有处理事件和维护 durable state 的业务上下文，preflight 也应直接验证同一套内部
ledger contract。否则启动检查可能只证明外部适配器或临时 probe 可用，不能证明 live
运行路径真正可用。

## Hermes 修复链路

Hermes 仓库本轮相关提交如下，均位于当前分支历史：

| Commit | 作用 |
| --- | --- |
| `9709cdb34` | internalize gateway event ledger |
| `373255bac` | harden internal gateway event ledger |
| `c2b286964` | make feishu inbound ledger authoritative |
| `b6b8f3536` | internalize session route guard |
| `bdd957a8f` | harden gateway event ledger state |
| `5a488ab28` | expose internal event preflight |
| `2255058e9bc16250d6562d3c349e10787ec28f09` | validate live ledger during preflight |

这些提交把旧 `hermes-tools gateway-event/preflight` 的 live-gateway 关键能力收敛到
Hermes 内部：`gateway.gateway_event_ledger` 成为内部 JSON ledger 实现，
`gateway.hermes_tools_gateway_event` 仅保留兼容 facade，不再暴露 external binary
adapter；CLI 侧新增 `hermes gateway preflight`，支持 `--state-dir`、`--lock-timeout`
和 `--json`，默认状态目录优先级为 `HERMES_GATEWAY_EVENT_STATE_DIR`，随后才是 legacy
`HERMES_TOOLS_STATE_DIR`。

实现后的 preflight 覆盖 `state_dir_writable`、`feishu_inbound`、
`delivery_lifecycle`、`feishu_ack`、`stale_pending_scan` 和 `session_guard`。
`task_status` 与 `status_card` 事件在当前内化范围外 fail closed，测试明确验证
`status_card_request_descriptor` 不属于本轮 preflight checks。

## Important 修复项

子代理源码审查发现一个 Important：原 `preflight_gateway_event()` 只验证临时
probe ledger。也就是说，preflight 会在 `state_dir` 下创建临时目录并跑内部事件流，
但不会读取和验证真实 `state_dir/gateway_event_ledger.json`。在真实 live ledger
已经损坏、schema 不兼容或内容不是有效状态时，preflight 仍可能返回 `ok=true`。

该问题会削弱启动前检查的证明力：systemd `ExecStartPre` 看似通过，实际 gateway
进程进入 live state 后仍可能在读取 durable ledger 时失败。修复提交
`2255058e9bc16250d6562d3c349e10787ec28f09` 在 preflight 中增加了真实 live ledger
校验：在持有 state lock 后读取 `_state_path(state_dir)`，让 schema 错误归入稳定失败类别。
新增回归测试覆盖损坏 live ledger 场景，断言 preflight fail closed，返回
`gateway_event_state_schema_invalid` / `gateway event state schema invalid`，并且 diagnostics
不泄露 ledger 中的敏感值。

## NixOS 部署链路

NixOS 侧本轮相关提交如下；本 worker 未修改该仓库，仅记录已完成部署链路事实：

| Commit | 作用 |
| --- | --- |
| `321657b` | quarantine legacy gateway tools |
| `0d63f5a` | pin gateway to internal preflight fork |
| `0e01917` | run preflight without shell wrapper |
| `1be59629be554e96c69cca3f7a1676b319c3efc4` | pin gateway preflight validation |

这些提交把 live gateway 的启动前检查固定到内部 preflight fork，并移除了 shell wrapper
运行方式对判断链路的影响。legacy gateway tools 被隔离，但未作为本轮 live path 的必要组件。

## 远端运行态证据

本轮验收记录的远端 `hermes-gateway.service` 证据为：

- systemd 状态为 active/running，result 为 success。
- `ExecStartPre=/var/lib/hermes/.local/bin/hermes gateway preflight --state-dir /var/lib/hermes/workspace/gateway-event-state --json`。
- `ExecStart=/var/lib/hermes/.local/bin/hermes gateway`。
- uv `direct_url` 的 `commit_id` / `requested_revision` 均为 `2255058e9bc16250d6562d3c349e10787ec28f09`。
- 进程环境包含 `HERMES_GATEWAY_EVENT_STATE_DIR=/var/lib/hermes/workspace/gateway-event-state`。
- 进程环境不包含 `HERMES_TOOLS_STATE_DIR`。
- active bin 目录中没有 `hermes-tools*`。

这些证据共同说明 live gateway 当前启动前检查和主进程均走
`/var/lib/hermes/.local/bin/hermes`，状态目录由 `HERMES_GATEWAY_EVENT_STATE_DIR`
显式指定，运行时没有依赖 `HERMES_TOOLS_STATE_DIR`，active bin 也没有外置
`hermes-tools` 可执行文件残留在运行路径上。

## 多维表格维护证据

运维 Base `Hermes 运维全生命周期台账`
(`L4dxb1V3vaMyn9sJLH0cLdQinah`) 已补齐本轮 gateway-event/preflight 内化的窄域记录。
回读检查使用 `lark-cli base +record-list --as user`，按业务键筛选，三次查询均为
`has_more=false`，每个业务键恰好命中一条记录：

| 表 | 业务键 | Record ID | 结论 |
| --- | --- | --- | --- |
| `03 执行任务 决策阻断` | `ACT-032` | `recvlKyjhij8TP` | 记录 Hermes gateway-event/preflight 内化与 live gateway 部署收束 |
| `04 验证验收` | `VAL-20260606-HERMES-GATEWAY-EVENT-PREFLIGHT` | `recvlKyqfTQBDo` | 记录本地专项测试、ruff、远端 service 与 runtime pin 验证 |
| `05 变更发布记录` | `CHG-20260606-HERMES-GW-EVENT-PREFLIGHT` | `recvlKyx0yfyDU` | 记录 NixOS live pin、部署状态、验证结果和回滚边界 |

这些记录没有把 `task` / `status-card` internalization 写成本轮已完成内容；相关文字仅作为
“用户范围内暂停”或“不要混写”的边界说明出现。Base 子代理复核三张目标表内按
`gateway-event` 扫描未发现明显重复；本轮未更新状态卡、长任务状态卡或任务卡片表。

## 本地验证

最终本地验证命令：

```bash
uv run pytest tests/hermes_cli/test_gateway_service.py::TestGatewayEventPreflight tests/gateway/test_gateway_event_ledger.py tests/gateway/test_hermes_tools_gateway_event.py tests/gateway/test_feishu_gateway_event_apply.py tests/gateway/test_run_progress_topics.py -q -rs
```

结果为 `123 passed, 10 skipped, 2 warnings`。10 个 skipped 全部是
`status-card/task card internalization intentionally deferred by user scope`，
与本轮明确暂停 `task` / `status-card` internalization 的范围一致。

最终 ruff 验证命令：

```bash
uv run ruff check gateway/gateway_event_ledger.py gateway/gateway_event_contract.py gateway/hermes_tools_gateway_event.py tests/hermes_cli/test_gateway_service.py tests/gateway/test_gateway_event_ledger.py tests/gateway/test_hermes_tools_gateway_event.py tests/gateway/test_feishu_gateway_event_apply.py tests/gateway/test_run_progress_topics.py
```

结果为 `All checks passed!`。

## 审查结论

子代理审查结论可归纳为四点：

第一，远端运行态没有 Critical 或 Important。service active/running/result success，
`ExecStartPre` 和 `ExecStart` 均指向内部 Hermes binary，uv pin 指向本轮验证提交，
环境变量和 active bin 目录均未显示 live path 依赖 `hermes-tools`。

第二，源码审查最初发现 preflight 不校验真实 ledger 的 Important。该问题已由
`2255058e9bc16250d6562d3c349e10787ec28f09` 修复，并通过回归测试锁定。

第三，修复后的规格复审和代码质量复审均无 Critical 或 Important。preflight 现在既验证
临时 probe 事件流，也验证真实 durable ledger 的可读性和 schema contract；失败路径使用稳定
failure class，不把未知、损坏或不兼容状态降级为成功。

第四，legacy 残留审查不建议立即清理。残留项不在 live 运行路径，但仍可能影响配置卫生和人工排障判断。
真正清理应先处理声明源和安装脚本迁移逻辑，避免手工删除造成后续 rebuild、安装或迁移脚本重新引入不一致状态。

第五，Base 维护复核无 Critical 或 Important。`ACT-032`、
`VAL-20260606-HERMES-GATEWAY-EVENT-PREFLIGHT` 和
`CHG-20260606-HERMES-GW-EVENT-PREFLIGHT` 均唯一命中，内容限定在 Hermes
gateway-event/preflight 内化与 live gateway 部署验收，不把状态卡或任务卡片内化纳入本轮完成范围。

## 剩余边界

`task` / `status-card` internalization 按用户指令暂停；因此这些项不是本轮
Hermes live gateway preflight 内化的完成条件，也不能被本记录描述为已完成。多维表格维护已经按
上述三条 Base 记录完成，不再是当前 goal 的剩余阻断项。

legacy `hermes-tools` state、quarantine、source-pin 等残留不在当前 live 运行路径。
它们的主要风险是配置卫生风险和人工排障混淆风险，而不是已证实的 live service 阻断风险。
不建议现在手动立即删除；清理应以声明源和安装脚本迁移逻辑为入口，先明确哪些配置仍由 NixOS
声明、哪些由安装脚本或 uv pin 生成，再通过可复现变更移除。

当前可闭环的是 Hermes gateway-event/preflight 内化、live service 运行证据、Important
修复、Base 维护和审查记录本身。未完成的 `task` / `status-card` internalization 属于用户明确暂停的
后续范围，不应反向阻断本轮 Hermes live gateway preflight 内化收束。
