# 02. 事件模型与协议流转

## 1. 模块职责

Agent 运行包含模型 token、reasoning、工具开始/增量/结束、审批、用户输入、配置变化、压缩、错误和 turn 生命周期。事件系统要让这些异步事实可关联、可重放、可投影到不同客户端，同时避免把 provider wire event 直接泄漏为产品协议。

Codex 实际有四层事件模型：

1. provider `ResponseEvent`：SSE/WebSocket 解码后的模型事件。
2. `ResponseItem`：可进入历史、再次发送给模型的语义 item。
3. core `EventMsg`：Agent 运行时发布的领域事件。
4. App Server `ServerNotification` 或 exec JSONL：面向具体客户端的协议投影。

## 2. 输入侧：`Submission` 与 `Op`

[Op](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/protocol/src/protocol.rs#L592) 是客户端对 Session 的命令，不是已经发生的事实。

- turn 控制：`TurnInput`、`RecoverTurn`、`Interrupt`、`SuspendTurnAndShutdown`。
- 设置：`ThreadSettings`、`TurnSettings`、`ReloadUserConfig`。
- 交互回答：`ExecApproval`、`PatchApproval`、`RequestPermissionsResponse`、`UserInputAnswer`、`ResolveElicitation`。
- 上下文：`Compact`、`ThreadRollback`、`SetThreadMemoryMode`。
- 特殊任务：`Review`、`RunUserShellCommand`、realtime、inter-agent communication。

部分 `Op` 包含 oneshot reply，只同步确认“是否受理”；真正进度仍通过事件流。例如 `TurnInput` 的 reply 返回 submission 结果，turn items 则由通知逐步到达。

## 3. 模型侧：`ResponseEvent`

```text
OutputItemAdded(item)
  -> OutputTextDelta / ReasoningSummaryDelta / ToolCallInputDelta ...
  -> OutputItemDone(item)
  -> Completed(response_id, token_usage, end_turn)
```

`OutputItemAdded` 建立 active item，使后续 delta 有归属；`OutputItemDone` 提供完整 item，并可能触发工具；`Completed` 结算一次 sampling 的 usage，但不必然结束 turn。

delta 到达时若没有 active item，代码调用 `error_or_panic`，说明事件顺序是协议不变量。缺失 item id 时，`assign_missing_streamed_response_item_id` 使用活动 item 或 Session 生成稳定 id，避免 started/delta/completed 无法关联。

## 4. 历史侧：`ResponseItem`

[ResponseItem](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/protocol/src/models.rs#L980) 是模型上下文的核心联合类型：

- `Message`：user/developer/assistant 文本、图像等内容。
- `Reasoning`：summary、可选 raw content、encrypted content。
- `FunctionCall` / `FunctionCallOutput`。
- `CustomToolCall` / `CustomToolCallOutput`。
- `LocalShellCall`、web search、image generation、tool search。
- compaction、configuration update 和兼容性 item。

函数参数保留为 JSON 字符串，因为 Responses API 按字符串传输；真正解析发生在 handler 分发前。`call_id` 连接调用与输出，`id` 连接流式 item 生命周期，`turn_id`/metadata 连接会话归属，三类 id 用途不同。

## 5. core 侧：`EventMsg`

[EventMsg](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/protocol/src/protocol.rs#L1356) 按领域而不是 provider 字段组织。

### 5.1 生命周期

`TurnStarted`、`ItemStarted`、`ItemCompleted`、`TurnComplete`、`TurnAborted`、`ShutdownComplete`。

### 5.2 模型输出

`AgentMessageContentDelta`、`ReasoningContentDelta`、`ReasoningRawContentDelta`、`AgentReasoningSectionBreak`、`RawResponseItem`、`RawResponseCompleted`。

### 5.3 工具

`ExecCommandBegin/OutputDelta/End`、`PatchApplyBegin/Updated/End`、`McpToolCallBegin/End`、`WebSearchBegin/End`、`ImageGenerationBegin/End`、`ViewImageToolCall`。

### 5.4 双向交互

`ExecApprovalRequest`、`ApplyPatchApprovalRequest`、`RequestPermissions`、`RequestUserInput`、`ElicitationRequest`、`DynamicToolCallRequest/Response`。

### 5.5 状态与治理

`TokenCount`、`ContextCompacted`、`ThreadRolledBack`、`ThreadSettingsApplied`、`ThreadGoalUpdated`、`ThreadQueueChanged`、`TurnDiff`、`StreamError`、`GuardianAssessment/Warning`。

## 6. 完整事件流

```text
provider SSE frame
  -> codex-api 反序列化为 ResponseEvent
  -> ModelClientSession::stream
  -> try_run_sampling_request reducer
     -> delta: 生成 EventMsg::*Delta
     -> item added: 生成 ItemStarted
     -> item done: 记录 ResponseItem，生成 ItemCompleted，可能启动 tool future
     -> completed: 记录 response_id/usage，标记 follow-up
  -> Session::send_event
  -> rollout recorder / subscribers
  -> App Server bespoke_event_handling
  -> v1/v2 ServerNotification
  -> TUI / SDK / exec JSONL reducer
```

模型事件不是一比一外发。raw assistant text 会经过 parser，将 plan 内容和可见文本分流；review child thread 可以抑制 delta，最后只发结构化 review；token count 延迟到 pending tool 结束，避免 UI 在等待审批时误判进度。

## 7. 三种 completed 不能混淆

- item completed：一个消息、reasoning 或工具调用对象完整。
- response completed：一次模型采样完整，工具可能还没执行。
- turn complete：本轮所有采样、工具、hook、usage 和 diff 均完成。

started 只意味着客户端可创建稳定 UI item，不保证一定 completed。中断时允许未完成 item，客户端要用 `TurnAborted` 关闭加载状态。

## 8. 工具事件与模型历史分叉

工具调用产生两条流：

- 展示流：begin -> output delta -> end，面向 UI 和日志。
- 推理流：`FunctionCall` -> `FunctionCallOutput`，写入 `ContextManager`。

展示流可以保留实时片段和退出状态；推理流按模型 truncation policy 裁剪。因此用户看到的终端输出和模型实际收到的文本不保证相同，调试界面应能区分。

## 9. App Server 投影

App Server 负责把 core event 映射为 v1/v2 通知、补 thread/turn/item 元数据、将审批事件转为 server-to-client request、按连接和 thread subscription 路由、处理 legacy 名称，并对部分事件聚合或抑制。

例如 v1 wire 使用 `task_started`，v2 使用 `turn_started`；core enum 通过 serde rename/alias 保持兼容。协议兼容并不是保留两套运行时。

## 10. 顺序、重复、断连和恢复

### 10.1 顺序

单 Session 发送通道保持观察顺序，但并行 tool future 使不同 call id 的事件交错。客户端只能依赖同一 item 的局部顺序。

### 10.2 重复

稳定 item id 和 call id 允许 reducer 幂等合并。prompt normalization 会为合成 tool output 派生稳定 id，使 retry/resume 不因随机 id 破坏 prompt cache。

### 10.3 断连

实时 delta 丢失不能靠后续 delta 修复。恢复应读取持久化 thread/turn/items，而不是重放 UI buffer。completed item 是语义权威，临时 started 状态不是。

### 10.4 中断

`TurnAborted` 可能先于某些工具终态或没有全部 item completed。客户端保留已完成 item 和已发生副作用，但必须终止该 turn 的 pending 展示。

## 11. 背压

模型 token、命令 stdout 和 MCP progress 都可能高速产生。Codex 在不同层限制命令 delta 数、使用 head/tail buffer、裁剪事件 payload，并由连接队列承担最后的背压。只在 UI 丢帧不能解决上游内存增长。

## 12. 测试证据

- `event_mapping` 测试断言 item started/completed 和 command delta 的 v2 映射。
- turn 测试覆盖 delta 与 active item 的顺序不变量。
- exec JSONL 与 human output 各有 processor 测试，证明同一 core event 可投影为不同表面。
- TUI thread event buffer 测试覆盖 replay notification 与 turn started。

## 13. 设计评价

四层事件模型避免 provider、运行时和产品协议互相绑死。代价是同一事实可能有 raw item、domain event、v1 notification、v2 notification 和 JSONL 五种表示。应以生成式映射清单和协议契约测试约束“新增 EventMsg 必须明确持久化、外发、忽略或降级”，避免静默丢事件。
