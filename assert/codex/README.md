# OpenAI Codex Agent 系统实现研究

本文档集不是按 Rust crate 罗列代码，而是按一个软件工程 Agent 必须解决的系统问题组织。每个模块单独成篇，并在同一篇内贯通职责、数据结构、入口、调用链、状态变化、失败恢复、安全边界、测试证据和设计评价。

## 研究基线

- 上游仓库：`openai/codex`
- 固定提交：`694b6319d3ad2399f6e435760a22d9b9357f0697`
- 源码链接：全部固定到该提交，避免 `main` 漂移
- 研究方法：代码知识图谱调用链、源码静态阅读、测试反证、OpenAI 官方 Codex 文档交叉核验
- 动态验证限制：当前环境没有 `cargo`，因此没有执行 Rust 编译和测试；所有“已验证”均指源码和测试代码静态核验

## Agent 系统模块地图

| 模块 | Agent 系统要解决的问题 | 文档 |
|---|---|---|
| 系统边界 | 进程、crate 和产品表面如何映射为一套 Agent 平台 | [00-system-map-and-methodology.md](00-system-map-and-methodology.md) |
| 运行时控制循环 | 用户输入如何变成一个可取消、可转向、可恢复的 turn | [01-agent-runtime-and-control-loop.md](01-agent-runtime-and-control-loop.md) |
| 事件模型 | 模型、工具、生命周期事件如何定义、关联、流转和兼容 | [02-event-model-and-protocol-flow.md](02-event-model-and-protocol-flow.md) |
| 上下文工程 | 指令、环境、历史和工具结果如何组装、更新、裁剪、压缩 | [03-context-engineering-and-compaction.md](03-context-engineering-and-compaction.md) |
| 模型客户端 | Responses 请求、SSE/WebSocket、重试和错误恢复如何工作 | [04-model-client-streaming-and-retry.md](04-model-client-streaming-and-retry.md) |
| 工具体系 | 工具注册、分发、长输出、长文件、并发和失败如何治理 | [05-tool-system-and-output-governance.md](05-tool-system-and-output-governance.md) |
| 权限与安全 | 审批、exec policy、Guardian、sandbox 如何逐层拦截 | [06-permission-approval-policy-and-guardian.md](06-permission-approval-policy-and-guardian.md) |
| 执行与文件系统 | shell、后台终端、apply_patch 和跨平台隔离如何落地 | [07-sandbox-exec-and-filesystem.md](07-sandbox-exec-and-filesystem.md) |
| 会话与持久化 | rollout、SQLite、resume/fork/rollback 如何保持状态一致 | [08-session-storage-rollout-and-resume.md](08-session-storage-rollout-and-resume.md) |
| 服务与客户端表面 | App Server 如何把 core 暴露给 TUI、exec 和 SDK | [09-app-server-and-client-surfaces.md](09-app-server-and-client-surfaces.md) |
| 扩展系统 | MCP、skills、plugins、hooks、memory 如何注入能力和上下文 | [10-mcp-skills-plugins-hooks-and-memory.md](10-mcp-skills-plugins-hooks-and-memory.md) |
| 多 Agent 与长期任务 | 子 Agent、mailbox、goal、queue、realtime 如何协作 | [11-multi-agent-goal-queue-and-realtime.md](11-multi-agent-goal-queue-and-realtime.md) |
| 平台治理 | 配置、认证、模型目录、可观测性、测试和发布如何兜底 | [12-config-auth-observability-testing.md](12-config-auth-observability-testing.md) |
| 综合评价 | 关键设计取舍、风险和 InnoAgent 的落地顺序 | [13-design-assessment-and-lessons.md](13-design-assessment-and-lessons.md) |

## 一次 turn 的总链路

```text
TUI / exec / SDK / App Server
  -> ThreadManager 定位或创建 Session
  -> Submission 队列串行接收 Op
  -> Session 创建 TurnContext / StepContext
  -> ContextManager + world state + instructions 形成 Prompt
  -> ModelClientSession 通过 Responses API 流式采样
  -> ResponseEvent 转 ResponseItem 和 EventMsg
  -> ToolRouter / ToolRegistry 分发工具
  -> hook -> approval -> exec policy -> Guardian -> sandbox -> handler
  -> 工具结果裁剪后写回历史，再次采样
  -> AgentMessage 或终止条件结束 turn
  -> rollout / SQLite / notifications / telemetry 同步落地
```

## 贯穿全书的四个结论

1. Codex 的核心不是一个 `while tool_call`，而是带有 submission 串行化、turn/step 双层上下文、异步工具 future、审批挂起、事件投影和持久化的状态机。
2. 上下文并不存放在一个字符串中。模型基础指令、权限说明、项目指令、skills、环境、会话历史、运行时 world state 和工具结果分别产生，再在采样前投影为 `Prompt`。
3. 工具安全不是一次判断。工具可见性、参数解析、PreToolUse hook、审批策略、Guardian、exec policy、sandbox transform 和平台内核隔离组成多道门。
4. Codex 对“大输出”有明确治理，但对“同一普通工具反复失败”没有统一的调用级熔断器；现有熔断只覆盖 Guardian 连续拒绝和 Goal 跨 turn 执行失败等特定场景。

## 证据标签

- **源码事实**：可以在固定提交的实现或测试中直接找到。
- **官方语义**：来自 OpenAI 官方 Codex 文档，用于解释公开契约。
- **设计评价**：基于源码事实做出的工程判断，不代表 OpenAI 官方结论。
- **未证实**：源码未找到通用实现，明确写出缺口而不是凭经验补全。

## 推荐阅读顺序

先读 `00 -> 01 -> 02 -> 03 -> 05 -> 06`，这六篇构成 Agent 主干；之后按需要阅读模型传输、执行沙箱、存储、App Server、扩展、多 Agent 和平台治理。

## 官方资料

- [Open source Codex](https://learn.chatgpt.com/docs/open-source.md)：开源实现与产品边界。
- [Codex App Server](https://learn.chatgpt.com/docs/app-server.md)：双向 JSON-RPC、thread/turn 与通知契约。
- [Sandboxing](https://learn.chatgpt.com/docs/sandboxing.md)：平台隔离和审批语义。
- [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md.md)：项目指令发现和层级。
- [MCP](https://learn.chatgpt.com/docs/extend/mcp.md)：外部工具和资源接入。
- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk.md)：嵌入式客户端表面。
- [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode.md)：exec/JSONL 自动化接口。

这些资料用于校验公开语义；实现细节仍以固定提交源码为准。
