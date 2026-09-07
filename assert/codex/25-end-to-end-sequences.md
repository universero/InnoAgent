# 25. 端到端实现时序

本章把分散在各 crate 的实现串成可验证路径，避免只知道模块职责却不知道真实控制流。

## 1. CLI 启动交互会话

```text
codex-cli npm wrapper
  -> platform binary / codex-rs/cli
  -> parse command + CLI overrides
  -> config loader builds layered snapshot
  -> login/auth manager resolves credentials
  -> TUI startup preflight/orchestration
  -> local or remote App Server session
  -> thread/start
  -> ThreadManager::start_thread
  -> ThreadStore::create_thread + Session::spawn
  -> thread/started + session/configured notifications
  -> ChatWidget renders ready state
```

失败边界：binary 选择失败停在 npm wrapper；配置 strict error 停在 runtime 前；认证失败不应创建 thread；store writer 失败不应返回 thread id；UI 连接断开不应自动停止仍被其他客户端订阅的 thread。

## 2. 用户输入到模型首个 token

```text
composer submit
  -> AppEvent / App Server turn/start
  -> TurnRequestProcessor validates input and settings
  -> CodexThread::start_or_steer_turn
  -> Session submission_loop receives Op
  -> create TurnContext + active task
  -> extension turn-start/context contributors
  -> ContextManager normalizes history
  -> ToolRouter freezes visible specs
  -> build_prompt
  -> ModelClientSession::stream
  -> WebSocket or HTTP/SSE
  -> response delta -> EventMsg
  -> App Server notification / exec processor / TUI stream renderer
```

首 token 延迟包含配置/extension/MCP/tool catalog/context assembly 和 transport 建连。预热只能减少可缓存部分，不能绕过 turn-scoped permission 和 tool visibility 计算。

## 3. 模型调用 shell 工具

```text
Responses function call delta
  -> accumulate complete arguments
  -> ToolCallRuntime schedules call
  -> ToolRegistry validates name + payload kind
  -> PreToolUse hooks
  -> unified exec parses/canonicalizes command
  -> exec policy evaluates every segment
  -> approval requirement calculated
  -> optional Guardian review
  -> optional client approval request/response
  -> SandboxManager transforms launch
  -> local process or exec-server remote process
  -> stdout/stderr/status stream
  -> PostToolUse hooks
  -> terminal tool event + rollout append
  -> FunctionCallOutput enters next model request
```

任何阶段拒绝都应形成模型可理解且用户可审计的结果。只有在进程尚未启动前的 block 才能保证无副作用。远端 accepted 后连接失败时不能未经 operation status 查询就重新执行。

## 4. `apply_patch`

```text
function call
  -> parse patch grammar
  -> enumerate affected paths
  -> canonicalize against cwd/writable roots
  -> policy + approval
  -> apply hunks with context checks
  -> compute change summary/diff
  -> emit item completion
  -> turn diff tracker aggregates changes
```

上下文不匹配必须整次失败或明确报告部分状态。路径检查和实际 open/write 之间存在 TOCTOU 风险，底层安全依赖 sandbox/no-follow/受控文件 API，而不是只做字符串 prefix。

## 5. 中断与 Steering

```text
new user input
  -> TurnRequestProcessor checks direct input
  -> start_or_steer_turn observes active task
  -> steer: append input to active turn channel
     or
  -> start: create new active task

interrupt
  -> submission_loop resolves active task
  -> cancel token fires
  -> model stream/tool futures/process receive cancellation
  -> unfinished items receive terminal state
  -> turn aborted event + rollout
  -> session returns idle
```

UI 收到“中断请求已发送”不代表所有外部副作用已经停止。对无法强制取消的远端 operation，需要展示 cancelling/unknown 等中间状态。

## 6. 自动压缩

```text
sampling detects context pressure/error
  -> compact policy and token budget
  -> select local/remote/v2 compact path
  -> preserve critical instructions/tool state
  -> model produces or service returns summary
  -> normalize summary item
  -> append compaction record to rollout
  -> replace model-visible history window
  -> retry sampling with bounded attempts
```

原始 rollout 不因压缩被删除，确保审计和重新构建。图片有独立预算/处理路径；模型切换可能改变 token estimate，因此压缩阈值必须从当前 model info 计算。

## 7. Resume

```text
thread/resume(thread id or rollout path)
  -> ThreadStore resolves live/explicit/indexed path
  -> migrate/recover metadata if necessary
  -> parse rollout and project history
  -> acquire unique live writer
  -> ThreadManager builds Session from history
  -> restore config/app-server metadata
  -> subscribe connection to events
  -> return thread snapshot
```

恢复只重建状态，不能执行历史工具。SQLite path 过期时显式 rollout path 优先；历史加载发生在 metadata observer 启用前，避免把旧 item 计为新活动。

## 8. Fork

```text
thread/fork(source, point)
  -> load source history/page
  -> select prefix and normalize boundary
  -> create new ThreadId and lineage
  -> create new rollout/live writer
  -> spawn independent Session
  -> register child in agent graph when applicable
```

fork 不继承 pending approvals、active processes 或 cancellation token。共享附件可通过引用实现，但删除和 GC 必须考虑多 thread 引用。

## 9. MCP Server 启动与调用

```text
effective config/plugins/extensions
  -> build McpRuntimeInput + RuntimeContext
  -> start required/optional connections
  -> stdio child or HTTP/OAuth transport
  -> initialize/capabilities
  -> paginate tools/resources
  -> sanitize and publish McpConnectionSet
  -> ToolRouter imports model-visible specs
  -> model calls namespaced tool
  -> prepare_call pins binding
  -> trusted access/approval
  -> MCP call -> result/content
  -> unified tool lifecycle
```

配置刷新发布新 runtime snapshot，而不是原地替换旧 turn 的 binding。required server 失败与 optional server 失败应有不同启动影响；OAuth refresh 用锁避免并发刷新。

## 10. Plugin 安装到生效

```text
marketplace add/refresh
  -> validate source and policy
  -> fetch git/npm/remote catalog
  -> cache/version/integrity metadata
  -> install plugin bundle
  -> parse manifest/interface/paths
  -> merge configured + remote installed state
  -> policy/toggle projection
  -> load skills/MCP/hooks/apps/commands
  -> build next extension/runtime snapshot
```

安装成功不等于启用，启用不等于拥有执行权限。插件脚本或 MCP 最终仍需进入统一工具和 sandbox 管道。

## 11. Memory Consolidation

```text
eligible completed rollout
  -> acquire per-item DB lease
  -> Phase 1 parallel extraction
  -> save candidates/status/backoff
  -> acquire global consolidation lock
  -> sync memory workspace
  -> start internal restricted agent
  -> generate and validate diff
  -> update files/index
  -> release lock and publish freshness
```

内部 Agent 无网络、无交互审批，避免后台任务无限等待或外发数据。任何 rollout 文本都按不可信来源处理；最终 memory 仍需 provenance、删除和冲突策略。

## 12. 子 Agent

```text
parent tool call spawn_agent
  -> AgentControl validates role/limits/fork mode
  -> select inherited history and settings
  -> ThreadManager::spawn_subagent
  -> new child thread/session/rollout
  -> agent graph parent-child edge
  -> child events/status routed to parent/UI
  -> wait/send/interrupt operations
  -> completion result returned as tool output
```

父任务取消应传播到子树，但已独立持久化的 child thread 可按策略保留。父子共享根 trace 时仍需独立 thread/turn ids；工具权限按角色收紧，不能只继承父级最大权限。

## 13. Shutdown

```text
client exit / explicit shutdown / channel close
  -> stop accepting new submissions
  -> cancel active turn and pending approvals
  -> terminate or detach process sessions by policy
  -> stop MCP/event/watch/realtime workers
  -> extension session/thread end hooks
  -> flush LiveThread/RolloutRecorder/state DB
  -> close transport and telemetry exporters
```

shutdown 是多阶段协议，不应只依赖 Rust Drop。任何步骤失败都应继续执行剩余清理并汇总错误；否则一个 hook 或 writer 失败会阻止子进程释放。

## 14. 关键源码索引

- [`ThreadManager`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/thread_manager.rs#L226)
- [`Session`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/session.rs#L42)
- [`submission_loop`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/handlers.rs#L529)
- [`run_turn`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/turn.rs#L163)
- [`ModelClientSession::stream`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/client.rs#L2027)
- [`ToolRegistry` dispatch](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/tools/registry.rs#L495)
- [`SandboxManager::transform`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/sandboxing/src/manager.rs#L358)
- [`MessageProcessor` dispatch](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/app-server/src/message_processor.rs#L975)
- [`ThreadStore`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/thread-store/src/store.rs#L68)
- [`ExtensionRegistry`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/ext/extension-api/src/registry.rs#L147)
