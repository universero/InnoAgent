# 01. 仓库架构与模块边界

## 形态

主体是大型 Rust workspace，辅以 npm CLI 包装、TypeScript SDK、Python SDK、Python pinned runtime、Bazel/Cargo/pnpm 构建与发布脚本。`core`、`tui`、`app-server` 是最大代码聚集区，说明架构虽高度拆 crate，但主要编排复杂度仍集中在少数模块。

公开领域类型在 [`protocol`](https://github.com/openai/codex/tree/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/protocol)，稳定核心 API 在 [`core-api`](https://github.com/openai/codex/tree/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core-api)，运行时在 [`core`](https://github.com/openai/codex/tree/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core)。这个方向避免 UI 类型渗入核心，但 App Server protocol、内部 protocol、SDK generated artifacts 仍形成多份契约。

## 逻辑分层

1. **表面层**：`cli` 解析命令，`tui` 负责交互，`exec` 负责非交互流，`app-server` 面向 IDE/SDK/其他宿主。
2. **协议层**：`protocol` 表达内部事件与操作，`app-server-protocol` 表达 JSON-RPC v1/v2，协议宏和 schema 生成支撑跨语言同步。
3. **运行时层**：`ThreadManager` 管 thread 生命周期，`Session` 管单 thread 状态，submission loop 串行消费 `Op`，turn loop 驱动模型和工具。
4. **能力层**：模型 provider、工具、MCP、skills、plugins、hooks、memories、connectors、多 Agent。
5. **控制层**：approval、exec policy、Guardian、sandbox、secret/redaction、process hardening、network proxy。
6. **状态层**：rollout JSONL 是事件事实来源，SQLite 提供查询投影，thread store 抽象本地/内存后端，trace bundle 服务调试。
7. **基础设施层**：HTTP/WebSocket、OTel、analytics、diagnostics、build-info 和大量 utilities。

## 依赖规则的实际效果

核心层不应依赖具体 UI；表面通过 `ThreadManager` 和 `Op/Event` 交互。App Server processors 把外部参数映射为 core 设置和操作，例如 `turn/start` 最终调用 `start_or_steer_turn`，而不是复制 Agent loop。存储通过 [`ThreadStore`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/thread-store/src/store.rs#L1) trait 隔离；扩展通过 typed registry 汇入运行时，而不是让每个扩展直接修改 Session。

## 架构热点

- `core` 仍拥有大量策略、状态和编排，边界不是微内核。
- [`handle_initialized_client_request`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/app-server/src/message_processor.rs#L975) 约 700 行、分支超过百个，是协议能力增长的中心成本。
- `run_turn`、sampling loop、工具 dispatch 同样是认知复杂度热点。
- 大量小 crate 强化编译边界和复用，但提高版本联动、feature gating 和发布维护成本。
