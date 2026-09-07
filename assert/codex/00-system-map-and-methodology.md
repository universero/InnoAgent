# 00. 系统地图与研究方法

## 1. 为什么不能按 crate 理解 Codex

固定版本中 Rust workspace 包含大量 crate，但一个 Agent 能力通常跨越多个 crate。例如“执行命令”同时涉及：`core/tools` 的工具路由、`core/exec_policy` 的规则判断、`sandboxing` 的策略转换、平台 sandbox crate、`protocol` 的事件、`app-server` 的通知映射以及 TUI 的交互审批。按目录逐个介绍会把一个安全状态机切成碎片。

本研究采用“系统模块”作为一级边界：每个模块必须回答它解决什么 Agent 问题、拥有哪类状态、由谁调用、向谁发事件、如何失败、怎样恢复、测试证明了什么。

## 2. 逻辑分层

### 2.1 产品表面层

- CLI 负责参数解析和选择 TUI、非交互 exec、App Server、MCP server 等运行形态。
- TUI 负责交互、流式渲染、审批弹窗和 thread 切换，不拥有 Agent 真状态。
- `exec` 把同一事件流转换为 human-readable 或 JSONL，适合 CI/自动化。
- TypeScript/Python SDK 通过 App Server 协议嵌入 Codex，而不是另写 Agent loop。

### 2.2 服务协议层

- `app-server-protocol` 定义双向 JSON-RPC 请求、响应和通知。
- `app-server` 维护连接、初始化协商、request id、thread subscription，并把 RPC 映射为 core `Op`。
- 协议层允许客户端只消费结构化生命周期，不直接依赖 Rust 内部对象。

### 2.3 Agent 核心层

- `ThreadManager` 管理 thread 到 `Session` 的关系。
- `Session` 持有配置、服务、运行中 turn、pending approval、事件发送器和历史。
- submission loop 串行处理会改变 session/thread 状态的 `Op`。
- `run_turn` 负责一次用户 turn；`run_sampling_request` 负责一次模型采样；一次 turn 可以包含多次采样和多次工具执行。

### 2.4 能力层

- 模型：Responses 请求、SSE/WebSocket、provider、认证、重试。
- 工具：内置工具、MCP、extension tool、dynamic tool、并行调度。
- 安全：审批策略、exec policy、Guardian、sandbox、敏感路径保护。
- 上下文：指令发现、world state、历史归一化、工具输出裁剪、compaction。
- 扩展：skills、plugins、hooks、memory、MCP、subagent。

### 2.5 状态与治理层

- rollout JSONL 是可重放的会话记录。
- SQLite/thread store 是查询、列表和元数据投影。
- OTel、analytics、trace 和结构化事件负责观测。
- config/auth/model catalog 决定运行能力但不应污染 turn 状态机。

## 3. 关键进程拓扑

```text
用户界面进程
  | JSON-RPC 或进程内 channel
  v
App Server / Core host
  | HTTP SSE 或 WebSocket
  v
Responses API provider

Core host
  | spawn / stdin / stdout
  v
exec-server / shell child
  | platform policy
  v
Seatbelt / bubblewrap+seccomp / Windows sandbox

Core host
  | MCP JSON-RPC
  v
外部 MCP servers
```

MCP server 的工具不是 Codex 本地 shell sandbox 自动保护的对象。Codex 可以在路由和审批层约束是否调用，但 MCP server 自己执行什么、能访问什么，最终仍由该 server 的进程权限和实现决定。

## 4. 源码入口索引

- Thread 创建与恢复：[`thread_manager.rs`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/thread_manager.rs)
- Session 主体：[`session/session.rs`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/session.rs)
- submission loop：[`handlers.rs#L529`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/handlers.rs#L529)
- turn 主循环：[`turn.rs#L163`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/turn.rs#L163)
- 工具 registry：[`registry.rs#L495`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/tools/registry.rs#L495)
- 协议枚举：[`protocol.rs#L592`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/protocol/src/protocol.rs#L592)
- App Server 分发：[`message_processor.rs#L975`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/app-server/src/message_processor.rs#L975)
- rollout/thread store：[`store.rs#L68`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/thread-store/src/store.rs#L68)

## 5. 研究方法

### 5.1 从状态变化出发

对每个模块先找“谁拥有状态”，再看 mutation 入口。例如审批不能只看弹窗事件，必须同时检查 pending oneshot 的注册、事件发出、响应匹配、断连和取消清理。

### 5.2 从调用链交叉验证

定义存在不代表运行中使用。本文用调用关系确认 `run_turn -> run_sampling_request -> try_run_sampling_request -> tool future -> record_conversation_items` 等链路，并用测试名称验证边界条件。

### 5.3 区分三种容量限制

- 传输/展示限制：防止 UI 或 JSONL 被单次输出淹没。
- history item 限制：工具结果进入模型历史前裁剪。
- context window 限制：整个 prompt 接近模型窗口时压缩。

这三者发生在不同时间，不能统称“截断”。

### 5.4 用反证寻找不存在的机制

针对“重复调用同一工具”检索了 tool loop、duplicate call、consecutive failure、same tool 等实现。找到的是重复注册拒绝、Guardian 拒绝熔断、Goal 跨 turn 失败计数和 prompt 中的行为提示，没有找到通用的 `(tool_name, args)` 指纹计数或普通工具连续失败熔断。因此文档把这一项标为未实现，而不是推测存在。

## 6. 验收标准

每个模块文档至少包含：

1. 该模块解决的 Agent 系统问题。
2. 核心对象及其状态所有权。
3. 从入口到结果的完整链路。
4. 正常、取消、失败、恢复路径。
5. 容量、安全或一致性边界。
6. 固定提交源码链接和相关测试证据。
7. 对 InnoAgent 的可执行建议。

## 7. 已知限制

- 没有运行 Rust 测试，不能声称动态行为已验证。
- 部分能力受 feature、后端或第一方环境控制，开源代码存在但默认配置未必启用。
- OpenAI 托管后端内部实现不在仓库中；本文只分析客户端可见协议和本地 harness。
- 图谱和静态搜索无法证明所有运行时排列，但足以确认核心调用链和“未发现通用机制”的范围。
