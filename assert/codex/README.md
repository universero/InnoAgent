# OpenAI Codex 开源实现研究

本文档集研究 OpenAI `openai/codex` 在固定提交上的实现，而不是依据产品宣传反推架构。结论分为三类：**源码事实**、**官方公开语义**、**设计评价/推断**。源码链接全部固定到提交 `694b6319d3ad2399f6e435760a22d9b9357f0697`，避免 `main` 漂移。

## 核心结论

Codex 不是“CLI 加一个 Agent loop”，而是以 Rust workspace 为主体的多表面 Agent 运行平台：`core` 管理 thread/session/turn，`protocol` 定义领域事件，`app-server` 暴露双向 JSON-RPC，TUI、非交互 exec、SDK 和远端能力复用同一批核心抽象。模型侧统一使用 Responses 语义；工具侧通过 registry、生命周期 hook、审批、exec policy、sandbox 和 Guardian 形成分层控制；会话侧同时维护 rollout JSONL、SQLite 投影与调试 trace。

最值得 InnoAgent 借鉴的是协议与表面分离、单线程提交循环、显式工具生命周期、fail-closed 审批、存储中立接口、事件化可观测性。最不应照搬的是 126 个左右 crate 的组织规模、巨型中央分发函数、旧新权限模型并存，以及 JSONL/SQLite/trace 三套状态表示带来的同步成本。

## 阅读导航

| 文档 | 主题 |
|---|---|
| [00-baseline-methodology.md](00-baseline-methodology.md) | 基线、方法、证据等级、限制 |
| [01-repository-architecture.md](01-repository-architecture.md) | 仓库结构、依赖方向、模块边界 |
| [02-entrypoints-process-topology.md](02-entrypoints-process-topology.md) | CLI/TUI/exec/App Server 入口与进程拓扑 |
| [03-core-runtime-turn-loop.md](03-core-runtime-turn-loop.md) | ThreadManager、Session、submission loop、turn loop |
| [04-model-context-compaction.md](04-model-context-compaction.md) | Responses、Prompt、上下文、压缩、重试 |
| [05-tools-exec-approvals-sandbox.md](05-tools-exec-approvals-sandbox.md) | 工具路由、命令执行、审批、策略与隔离 |
| [06-config-auth-models.md](06-config-auth-models.md) | 配置层、认证、provider 与本地模型 |
| [07-protocol-app-server.md](07-protocol-app-server.md) | 协议版本、JSON-RPC、线程与 turn API |
| [08-storage-rollout-resume.md](08-storage-rollout-resume.md) | rollout、SQLite、恢复、历史与附件 |
| [09-cli-tui-exec-sdk.md](09-cli-tui-exec-sdk.md) | 各交互表面和 TypeScript/Python SDK |
| [10-extensions-mcp-skills-plugins-hooks-memory.md](10-extensions-mcp-skills-plugins-hooks-memory.md) | Extension、MCP、Skills、Plugin、Hook、Memory |
| [11-remote-cloud-realtime-multi-agent.md](11-remote-cloud-realtime-multi-agent.md) | Cloud、环境、实时语音、工作树与多 Agent |
| [12-testing-build-release-observability.md](12-testing-build-release-observability.md) | 测试、构建、发布、遥测与诊断 |
| [13-design-assessment-innoagent-lessons.md](13-design-assessment-innoagent-lessons.md) | 设计评价与 InnoAgent 落地路线 |
| [14-crate-catalog.md](14-crate-catalog.md) | Rust crate 与非 Rust package 完整目录 |

## 覆盖矩阵

| 能力域 | 主要实现 | 深入文档 | 覆盖 |
|---|---|---|---|
| 会话/线程/turn | `core`, `protocol`, `thread-store` | 03, 08 | 已核验调用链 |
| 模型请求/流式传输 | `core/client`, `codex-api`, `codex-client` | 04 | 已核验 fallback 与重试 |
| 工具/执行/补丁 | `core/tools`, `exec-server`, `apply-patch` | 05 | 已核验生命周期与审批 |
| 沙箱/权限 | `sandboxing`, platform sandbox crates | 05 | 已核验三平台与 fail-closed |
| 配置/认证/provider | `config`, `login`, `model-provider*` | 06 | 已核验优先级和分层 |
| App Server/协议 | `app-server*`, `app-server-protocol*` | 07 | 已核验 request dispatch |
| 持久化/恢复 | `rollout`, `state`, `thread-store` | 08 | 已核验多表示模型 |
| CLI/TUI/SDK | `cli`, `tui`, `exec`, `sdk/*` | 02, 09 | 已核验 SDK 差异 |
| MCP/扩展/plugin/skill | `ext/*`, `codex-mcp`, `core-plugins` | 10 | 已核验贡献点 |
| 多 Agent/云/实时 | `agent-*`, `cloud-*`, `realtime-webrtc` | 11 | 已核验边界与入口 |
| 测试/发布/遥测 | tests, Bazel/Cargo/pnpm, `otel*` | 12 | 静态盘点 |
| 全部 crate | `codex-rs/**/Cargo.toml` | 14 | 逐项归类 |

## 一句话架构图

`CLI/TUI/exec/SDK/App Server -> ThreadManager -> Session submission loop -> run_turn -> Responses stream -> ToolRouter -> hooks/approval/policy/sandbox -> events + rollout/thread store + telemetry`。
