# 02. 事件模型、协议流转与 Session 入库

## 1. 模块职责与结论

Codex 的“事件”不是一个 enum 从模型端一路透传到 UI，而是多层协议之间的转换。理解这部分时必须区分命令、provider 流事件、模型语义记录、core 领域事件、客户端通知和持久化记录，否则很容易得出“所有事件都会进入上下文”或“delta 会被完整重放”这类错误结论。

源码中的实际分层是：

| 层 | 核心类型 | 方向 | 主要用途 | 是否直接进入模型历史 |
| --- | --- | --- | --- | --- |
| 客户端命令 | `Submission { id, op, trace, ... }` / `Op` | client -> core | 发起 turn、审批、配置、回滚、关闭等操作 | 否 |
| provider 流 | `ResponseEvent` | model provider -> core | 表达一次 Responses 流中的 added、delta、done、completed | 否 |
| 模型语义记录 | `ResponseItem` / `ResponseItemEnvelope` | core 内部 | 构成下一次请求的 conversation history | 是，经过过滤与裁剪后 |
| core 领域事件 | `Event { id, msg: EventMsg }` | core -> consumer | 表达 turn/item/tool/审批/错误等运行时事实 | 不直接进入 `ContextManager` |
| 产品协议 | `ServerNotification` 等 | app-server -> client | v1/v2 通知、server request、JSONL/TUI 投影 | 否 |
| 持久化日志 | `RolloutLine { timestamp, ordinal, item }` | core -> JSONL/store | 恢复、分页历史、审计和索引投影 | 恢复时按记录类型重建 |

最关键的三个结论：

1. `EventMsg` 和 `ResponseItem` 是两条相关但不同的数据流。前者面向运行观察，后者面向模型上下文。
2. `send_event_raw` 会把领域事件放入“候选持久化”链，但 rollout policy 会过滤瞬态事件；不是所有事件都会落盘。
3. Session 恢复主要消费 `ResponseItem`、compaction、`TurnContext`、`WorldState` 和少量 turn 边界事件，而不是把所有 UI event 原样重放成 prompt。

## 2. 输入协议：`Submission`、`Op` 与命令队列

### 2.1 `Submission` 是带因果信息的命令信封

[Submission](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/protocol/src/protocol.rs#L192) 包含：

- `id`：本次提交的关联 id。core 发出的 `Event.id` 通常复用 turn/submission id。
- `op`：真正的操作枚举 `Op`。
- `trace`：跨异步边界传播的 W3C trace context。
- `parent_turn_id`：直接触发该提交的父 turn，主要用于 agent 间通信。
- `root_turn_id`：整条因果链的根 turn。

这说明协议相关性不是只有一个 request id。普通客户端主要依赖 submission/turn id，多 Agent 链路还需要 parent/root turn id，观测系统则依赖 trace context。

### 2.2 `Op` 表示意图，不表示已发生事实

[Op](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/protocol/src/protocol.rs#L592) 是 `#[non_exhaustive]` enum，允许未来增加操作。当前操作可以按职责分为：

- turn：`TurnInput`、`RecoverTurn`、`Interrupt`、`SuspendTurnAndShutdown`。
- 设置：`ThreadSettings`、`TurnSettings`、`ReloadUserConfig`、`SetThreadMemoryMode`。
- 人机交互回复：`ExecApproval`、`PatchApproval`、`RequestPermissionsResponse`、`UserInputAnswer`、`DynamicToolResponse`、`ResolveElicitation`。
- 历史控制：`Compact`、`ThreadRollback`。
- 特殊任务：`Review`、`RunUserShellCommand`、`InterAgentCommunication`。
- realtime：start/audio/text/speech/list voices/close。
- 生命周期：`CleanBackgroundTerminals`、`Shutdown`。

`TurnInput`、`RecoverTurn`、`TurnSettings`、`SuspendTurnAndShutdown` 自带 oneshot reply。这个 reply 只确认命令的同步处理结果，并不承载完整运行过程；后续进度仍通过 event channel 发布。

### 2.3 命令如何进入 Session

[SessionIo](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/mod.rs#L392) 暴露两个方向相反的 async channel。submission channel 容量为 512，event channel 则是 unbounded：

```text
client
  -> SessionIo.tx_sub: Sender<Submission>
  -> submission_loop(rx_sub)
  -> handler / task / active turn

core producer
  -> Session.tx_event: Sender<Event>
  -> SessionIo.rx_event: Receiver<Event>
  -> CodexThread::next_event
  -> app-server / TUI / SDK
```

`SessionIo::submit_with_id` 在调用方没有提供 trace 时捕获当前 span 的 W3C 上下文，然后异步发送到 `tx_sub`。通道关闭会转换为 `InternalAgentDied`，因此“提交成功”首先意味着命令成功入队。

[submission_loop](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/handlers.rs#L529) 串行 `recv` 每个 `Submission`，按 `Op` 分发到 turn input、审批、MCP、compact、rollback、review、realtime 等 handler。这里有三点设计意义：

1. thread settings 与 turn start 走同一队列，可保留调用者观察到的顺序。
2. approval answer 不新建独立事件循环，而是唤醒挂在 Session 中的 pending waiter。
3. submission channel 意外关闭时仍执行 runtime teardown、thread-stop lifecycle 和 rollout shutdown，避免只处理显式 `Shutdown`。

## 3. Provider 协议：`ResponseEvent` 是采样流状态机输入

[ResponseEvent](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/codex-api/src/common.rs#L102) 是 SSE/WebSocket 解码后的内部 provider 事件，当前包括：

- response 级：`Created`、`Completed`。
- item 级：`OutputItemAdded`、`OutputItemDone`。
- 文本与参数增量：`OutputTextDelta`、`ToolCallInputDelta`。
- reasoning 增量：`ReasoningSummaryDelta`、`ReasoningSummaryDone`、`ReasoningContentDelta`、`ReasoningSummaryPartAdded`。
- provider 元信息：`ServerModel`、`ModelVerifications`、`TurnModerationMetadata`、`SafetyBuffering`、`ServerReasoningIncluded`、`RateLimits`、`ModelsEtag`。

典型顺序是：

```text
Created(response_id?)
  -> OutputItemAdded(partial item)
  -> zero or more text/reasoning/tool-argument deltas
  -> OutputItemDone(complete ResponseItem)
  -> zero or more additional items
  -> Completed(response_id, usage, end_turn)
```

### 3.1 reducer 的活动状态

[try_run_sampling_request](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/turn.rs#L2308) 在一次采样内维护：

- `active_item`：当前正在流式构造的 `TurnItem`。
- `active_item_is_streaming_to_client`：扩展 contributor 存在时可暂缓直接流式发布。
- `active_tool_argument_diff_consumer`：消费 custom tool 参数 diff，并可产生专用领域事件。
- `in_flight`：已完成 tool-call item 启动的工具 future，使用 `FuturesOrdered` 收集。
- `needs_follow_up`：模型是否需要在工具结果后继续采样。
- `assistant_message_stream_parsers`：把原始 assistant 文本切分为可见文本、plan 等语义。
- `should_emit_token_count` / `should_emit_turn_diff`：推迟到工具 future 排空后发布。

`OutputItemAdded` 会先补齐缺失 item id，再把非工具 item 转成 `TurnItem` 并发 `ItemStarted`。`OutputTextDelta` 和 reasoning delta 必须依附 `active_item`；没有 active item 会触发 `error_or_panic`，这是一条明确的 provider 顺序不变量。

`ToolCallInputDelta` 不直接写历史。它只交给与 active call id 匹配的 diff consumer，用于产生诸如 patch 预览之类的展示事件。最终、完整、可执行的参数仍以 `OutputItemDone(ResponseItem)` 为准。

### 3.2 `Completed` 不等于 turn 完成

`ResponseEvent::Completed` 只表示一次 sampling response 结束。处理逻辑会：

1. 记录 response id、usage 和 usage metadata。
2. 写入独立的 `RolloutItem::TokenUsageRecord`。
3. 根据 `end_turn == Some(false)` 设置 `needs_follow_up`。
4. 先等待本轮已经启动的 tool futures，再发布 token count 和 turn diff。

因此必须区分：

- item completed：一个 response item 已完整。
- response completed：一次 provider sampling 已完整。
- turn complete：sampling、工具、hook、follow-up 和收尾状态全部结束。

## 4. 模型语义层：`ResponseItem` 与 `TurnItem`

[ResponseItem](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/protocol/src/models.rs#L980) 是 Responses API 语义记录，也是 Session 模型历史的基础：

- 消息：`Message`、`AgentMessage`。
- reasoning：`Reasoning`。
- 工具：`FunctionCall`、`FunctionCallOutput`、`CustomToolCall`、`CustomToolCallOutput`、`ToolSearchCall/Output`、`LocalShellCall`、`WebSearchCall`、`ImageGenerationCall`。
- 上下文控制：`ConfigurationUpdate`、`Compaction`、`ContextCompaction`。
- 临时控制：`AdditionalTools`、`CompactionTrigger`、`Other`。

`ResponseItemEnvelope` 在 item 外附加 harness 私有 metadata，例如客户端 authored 标记、fallback truncation token limit、user input acceptance order、是否为继承的父 Agent 消息。这样不会污染 provider item schema，又能让 resume 后保留相同的裁剪和授权语义。

`TurnItem` 则是面向产品 UI 的规范化 item。一个完整 `ResponseItem` 可以经 `parse_turn_item` 或 contributor 转成 `TurnItem`，再发布 `ItemStarted` / `ItemCompleted`。两者不等价：

- `ResponseItem` 强调“下次模型调用需要什么”。
- `TurnItem` 强调“客户端如何展示一个 turn 中的消息、命令、文件变更、MCP 调用或计划”。
- 某些 `ResponseItem` 不生成 `TurnItem`；某些 UI `TurnItem` 也没有一一对应的 provider item。

## 5. Core 协议：`Event` 与 `EventMsg`

[Event](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/protocol/src/protocol.rs#L1338) 只是 envelope：

```rust
pub struct Event {
    pub id: String,
    pub msg: EventMsg,
}
```

`id` 是关联 submission/turn 的外层 id。具体事件内部通常还有 `thread_id`、`turn_id`、`item_id`、`call_id`：

- `thread_id` 定位长期会话。
- `turn_id` 定位一次用户任务。
- `item_id` 关联 started/delta/completed UI item。
- `call_id` 关联模型 tool call 与 tool output，也用于审批 waiter。
- `Event.id` 兼容旧消费方并关联产生该事件的 submission。

[EventMsg](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/protocol/src/protocol.rs#L1356) 使用 internally tagged serde：wire JSON 以 `type: snake_case` 区分变体。注释明确要求 payload 不使用 optional 顶层变体，以免破坏 extension codegen。

### 5.1 生命周期与 durable boundary

- thread/session：`SessionConfigured`、`ShutdownComplete`、`EnvironmentConnected/Disconnected`。
- turn：`TurnStarted`、`TurnComplete`、`TurnAborted`。
- item：`ItemStarted`、`ItemCompleted`。
- hook：`HookStarted`、`HookCompleted`。

`TurnStarted` 的 v1 序列化名仍是 `task_started`，同时接受 `turn_started` alias；`TurnComplete` 同理。这是 wire compatibility，不代表 core 内维护两套生命周期。

### 5.2 模型与流式输出

- 完整/调试：`RawResponseItem`、`RawResponseCompleted`。
- assistant：`AgentMessage`、`AgentMessageContentDelta`。
- reasoning：`AgentReasoning`、`AgentReasoningRawContent`、`ReasoningContentDelta`、`ReasoningRawContentDelta`、`AgentReasoningSectionBreak`。
- plan：`PlanUpdate`、`PlanDelta`。
- provider 状态：`SafetyBuffering`、`ModelReroute`、`ModelVerification`、`TurnModerationMetadata`。

### 5.3 工具与副作用

- command：`ExecCommandBegin`、`ExecCommandOutputDelta`、`TerminalInteraction`、`ExecCommandEnd`。
- patch：`PatchApplyBegin`、`PatchApplyUpdated`、`PatchApplyEnd`。
- MCP：`McpStartupUpdate/Complete`、`McpToolCallBegin/End`。
- hosted tools：`WebSearchBegin/End`、`ImageGenerationBegin/End`、`ViewImageToolCall`。
- dynamic/collab：`DynamicToolCallRequest/Response`、各类 `Collab*Begin/End`、`SubAgentActivity`。

### 5.4 双向等待与治理

- 审批/输入：`ExecApprovalRequest`、`ApplyPatchApprovalRequest`、`RequestPermissions`、`RequestUserInput`、`ElicitationRequest`。
- 状态：`TokenCount`、`ThreadGoalUpdated`、`ThreadQueueChanged`、`ThreadSettingsApplied`、`ThreadRolledBack`、`ContextCompacted`、`TurnDiff`。
- 错误与治理：`Error`、`Warning`、`StreamError`、`GuardianAssessment`、`GuardianWarning`、`AuthRecoveryStarted/Completed`、`DeprecationNotice`。
- realtime：started、SDP、realtime payload、closed、list voices response。

## 6. Core 事件发送链的准确顺序

[Session::send_event](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/mod.rs#L2087) 并非简单 `tx.send`，顺序如下：

```text
EventMsg
  -> 若 Error 影响 turn 状态，写 turn_context.terminal_error
  -> rollout trace 记录 codex turn/tool-call 观测
  -> 包装 Event { id: turn_context.sub_id, msg }
  -> 可选 Guardian analytics
  -> send_event_raw(event)
  -> 通知父 Agent terminal turn
  -> 可选镜像文本到 realtime
  -> 清理 realtime handoff 状态
  -> 从 canonical item event 派生 legacy events，再逐个 send_event_raw
```

[send_event_raw_with_persistence](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/mod.rs#L2385) 又执行：

```text
Event
  -> realtime history reducer 判断是否观察
  -> MCP runtime.observe_event
  -> 必要时先写 realtime 派生事实
  -> 包装 RolloutItem::EventMsg，交给 rollout policy
  -> rollout trace.record_protocol_event
  -> 必要时后写 realtime 派生事实
  -> deliver_event_raw
       -> 更新 AgentStatus watch
       -> tx_event.send(Event)
```

“before/after realtime effects”与 canonical event 在同一锁保护的顺序中处理，避免 realtime transcript 与事件顺序分叉。注释同时禁止这里获取 `SessionState` 或 `ActiveTurn` 锁，因为调用者可能已经持有它们。

`deliver_event_raw` 对 `TurnStarted/Complete/Aborted`、`Error`、`ShutdownComplete` 更新 `watch::Sender<AgentStatus>`，随后发送到 `tx_event`。event receiver 关闭时只记录 debug 日志，运行时不会因为没有 UI 订阅者而崩溃。

## 7. Canonical 事件与 legacy 事件

`ItemStarted`、`ItemCompleted` 和三类 delta 是 canonical 事件。发送 canonical event 后，[as_legacy_events](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/protocol/src/legacy_events.rs#L633) 可再派生旧协议事件，例如：

- `ItemStarted(CommandExecution)` -> `ExecCommandBegin`。
- `ItemCompleted(CommandExecution)` -> `ExecCommandEnd`。
- `ItemCompleted(AgentMessage)` -> `AgentMessage`。
- canonical content delta -> legacy message/reasoning delta。

这意味着同一事实可能在 event channel 中出现 canonical 和 legacy 两种表达。消费者不能把它们当作两个独立动作；App Server 的职责之一就是按 API 版本选择、聚合或抑制这些表达。

## 8. `ResponseItem` 如何进入 Session 内存历史

### 8.1 写入入口

完整模型 item、用户输入、工具输出、hook 追加上下文等最终调用 [record_conversation_items](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/mod.rs#L3361)：

```text
ResponseItem[]
  -> prepare_conversation_items_for_history
       -> 补 item/turn id
       -> 准备本地图片/音频等内容
  -> ResponseItemEnvelope[]
  -> record_prepared_conversation_items
       -> SessionState.history.record_annotated_items
       -> RolloutItem::ResponseItem[]
       -> persist_rollout_items
       -> RawResponseItem events
```

顺序很重要：先更新内存历史，再尝试持久化语义 item，最后发送 `RawResponseItem` 调试事件。`RawResponseItem` 自身被 rollout policy 丢弃，但其承载的底层 `ResponseItem` 已经作为独立 rollout record 进入持久化链。

### 8.2 `ContextManager` 的过滤和裁剪

[record_items_with_metadata](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/context_manager/history.rs#L335) 对每项执行：

1. `is_api_message` 过滤不应进入模型 API 的记录。
2. 克隆 item 与 harness metadata。
3. 对 `FunctionCallOutput` / `CustomToolCallOutput` 按模型 truncation policy 裁剪；metadata 可覆盖 fallback token limit，默认再乘 `1.2` 给序列化开销留余量。
4. 非 contextual user message 可同步进入 review history。
5. 追加到 `ContextManager.items`。
6. 用户授权消息按配置进入 bounded retained context，并记录 completeness 与 acceptance order。

这里还有一个容易忽略的细节：`record_annotated_items` 会克隆 envelope 后裁剪内存副本，而 `record_prepared_conversation_items` 随后持久化的是原始 prepared envelope。也就是说 rollout 可以保留更完整的工具输出，当前 Session history 保存裁剪版；resume 时再用持久化 metadata 和当前模型 policy 重做裁剪。

### 8.3 完整模型 item 的处理分叉

[handle_output_item_done](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/stream_events_utils.rs#L290) 的三条路径：

- 合法 tool call：立即记录完整 call `ResponseItem`，再启动 tool future；工具 output 完成后以另一条 `ResponseItem` 回写历史，并要求 follow-up sampling。
- 普通 message/reasoning：先 finalize 为 `TurnItem`，补发缺失的 started，发布 completed，然后记录原始 `ResponseItem`。
- 可恢复的 tool-call 解析错误：先记录错误的 call item，再合成 `FunctionCallOutput` 错误消息写入历史，让模型有机会自纠。
- fatal tool-call 错误：直接返回 `CodexErr::Fatal`，不伪造成功输出。

用户输入走专门的 `record_user_prompt_and_emit_turn_item`：先把 `UserInput` 转为 `ResponseItem::Message` 并带 acceptance order 写入历史，再从原始 `UserInput` 构造 UI `TurnItem::UserMessage`，这样 `text_elements` 等 UI-only 信息不会因 `ResponseItem` 的简化表示而丢失。

## 9. EventMsg 如何进入 rollout：候选、策略与落盘

### 9.1 候选持久化不是最终落盘

默认 `send_event_raw` 使用 `persist = true`，把 `event.msg.clone()` 包成 `RolloutItem::EventMsg`，再调用 `persist_rollout_items`。但 [is_persisted_rollout_item](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/rollout/src/policy.rs#L10) 才是最终 policy gate。

`send_event_raw_without_materializing_rollout` 用于不希望仅因某个事件创建新 rollout 文件的场景：如果文件已经 materialized 则照常持久化；尚未 materialize 时只发布事件。

### 9.2 `EventMsg` 持久化矩阵

[should_persist_event_msg](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/rollout/src/policy.rs#L94) 的规则不是“completed 都保存”，而是按恢复价值选择：

| 类别 | Legacy | Paginated | 原因 |
| --- | --- | --- | --- |
| `TurnStarted/Complete/Aborted` | 保存 | 保存 | turn 边界、终态和恢复分段 |
| `TokenCount`、goal、rollback、settings applied | 保存 | 保存 | durable thread 状态 |
| `ItemCompleted` | 少量例外 | 全部保存 | paginated history 直接以 `TurnItem` 构建产品历史 |
| 旧 `UserMessage/AgentMessage/Reasoning` | 保存 | 丢弃 | paginated 模式已有 canonical `ItemCompleted` |
| 旧 tool end、review mode、context compacted | 保存 | 丢弃 | legacy 历史兼容 |
| started、delta、approval/request、stream error | 丢弃 | 丢弃 | 瞬态、不可作为恢复权威 |
| raw response、model reroute、safety、moderation | 丢弃 | 丢弃 | 调试或当前连接展示状态 |
| `ExecCommandEnd` | 丢弃 | 丢弃 | canonical item/response history承担 durable 语义 |

Legacy 模式中 `ItemCompleted` 只额外保存没有无损 `ResponseItem`/legacy 等价物的 `FunctionCallOutput`、`Plan`、sleep extension，以及 completed sub-agent activity。这样避免同一事实重复占用 rollout。

### 9.3 `ResponseItem` 持久化矩阵

[should_persist_response_item](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/rollout/src/policy.rs#L44) 保存消息、reasoning、工具调用/输出、web/image call、configuration update 和 compaction；丢弃 `AdditionalTools`、`CompactionTrigger`、`Other`。后者是临时控制信号，不应污染可恢复历史。

`RolloutItem` 还可保存 `SessionMeta`、inter-agent communication、`Compacted`、`TurnContext`、`TokenUsageRecord`、`WorldState`、`SecurityRiskScore`、`RetainedContext` 和 paginated 模式下的 `RealtimeItem`。

### 9.4 JSONL 写入与一致性边界

[RolloutLine](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/history/src/lib.rs#L254) 是一条 JSONL：`timestamp + ordinal + flattened RolloutItem`。`RolloutItemWire` 使用 `type: snake_case` 与 `payload`，`ResponseItem` metadata 独立保存。

写入链如下：

```text
Session::persist_rollout_items
  -> LiveThread::append_items
  -> LocalThreadStore::append_items
  -> write_and_project
       -> policy retain
       -> RolloutRecorder::record_canonical_items
       -> RolloutRecorder::flush
       -> paginated 模式再 materialize_to_sqlite
       -> metadata projection
```

`write_and_project` 先 flush canonical JSONL，再投影 SQLite，因此 SQLite 可以落后于 JSONL，但不能领先于 canonical history。`RolloutRecorder` 使用容量 256 的 mpsc writer queue；队列满时发送 future 会让出执行权，形成背压。

writer 把 item 暂存在 `pending_items`，逐项写入并递增 ordinal。只有成功写入的前缀才从 pending queue 删除。I/O 失败会关闭 file handle、保留未写后缀、重新打开并重试一次；`flush/persist/shutdown` 都有 oneshot ack。

需要注意，`Session::persist_rollout_items` 捕获并记录错误，不把错误返回给调用者。事件路径因此是“先尽力 durable，再通知”，而不是跨 JSONL、SQLite、event channel 的原子事务。持续磁盘错误时客户端仍可能看到不能恢复的事件。

## 10. App Server 如何消费与投影

[ensure_listener_task_running](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/app-server/src/request_processors/thread_lifecycle.rs#L216) 为每个活动 thread 启动 listener：

1. `conversation.next_event()` 从 `SessionIo.rx_event` 读取 core `Event`。
2. turn cost worker 观察 usage 相关事件。
3. `ThreadState::track_current_turn_event` 更新 started time、最后 final answer、当前 turn history 和 terminal turn id。
4. 未启用 experimental raw events 时过滤 `RawResponseItem/RawResponseCompleted`。
5. 获取订阅该 thread 的 connection ids。
6. 调用 `apply_bespoke_event_handling` 映射 v1/v2 notification 或 server-to-client request。
7. `ShutdownComplete` 还会释放 shutdown drain waiter。

`ThreadState` 是 App Server 的在线投影，不是 core Session 的模型历史。它保存 pending interrupts/rollback、turn summary、listener generation、当前 turn history 和订阅协调状态，允许新订阅者获得当前 turn 快照。

## 11. Resume 时哪些记录真正重建 Session

[reconstruct_history_from_rollout](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/rollout_reconstruction.rs#L133) 先从后向前扫描，以 compaction checkpoint、turn segment 与 rollback 找出幸存后缀，再正向重放：

- `ResponseItem`：重新进入 `ContextManager`。
- `InterAgentCommunication`：转换为 model input item。
- `Compacted`：安装 replacement history，或对旧格式重建 compacted history。
- `ThreadRolledBack`：删除最近 N 个 user turns。
- `TurnContext`：恢复上一轮模型/settings/reference baseline。
- `WorldState`：从 full snapshot 开始应用 merge patch。
- `RetainedContext`：恢复受控保留的用户上下文。
- `TurnStarted/Complete/Aborted`：用于逆向分段、判断 rollback 应丢弃哪些 turn。
- 其他 `EventMsg`：通常不进入模型历史。

随后 [apply_rollout_reconstruction](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/mod.rs#L1553) 对历史重新做图片/音频准备，调用 `replace_annotated_history` 安装到 `SessionState.history`，恢复 review/retained context、world-state baseline、auto-compaction window 和 previous turn settings。

这说明 rollout 是统一的可恢复事实日志，但不同 reducer 读取不同子集：模型上下文 reducer、App Server history builder、SQLite projector、analytics reducer 并不共享完全相同的投影视图。

## 12. 三个端到端示例

### 12.1 Assistant 文本

```text
OutputItemAdded(Message)
  -> ItemStarted(AgentMessage)
OutputTextDelta
  -> AgentMessageContentDelta      # 瞬态，不落盘
OutputItemDone(Message)
  -> ItemCompleted(AgentMessage)   # paginated 模式落盘
  -> ResponseItem::Message         # 两种模式都落盘并进入 ContextManager
  -> RawResponseItem               # 可选外发，EventMsg 本身不落盘
Completed
  -> RawResponseCompleted          # 不落盘
  -> TokenUsageRecord              # 落盘
```

### 12.2 Shell 工具

```text
OutputItemDone(FunctionCall)
  -> ResponseItem::FunctionCall 入历史/rollout
  -> 启动 tool future
  -> ItemStarted(CommandExecution)
  -> ExecCommandBegin legacy event
  -> ExecCommandOutputDelta*        # 瞬态
  -> ItemCompleted(CommandExecution)
  -> ExecCommandEnd legacy event
  -> ResponseItem::FunctionCallOutput 入历史/rollout
  -> follow-up sampling
```

展示输出和模型 output 不保证相同：UI 可看到流式 terminal delta，模型历史只保存经过 output policy 和 token truncation 的工具结果。

### 12.3 审批等待

```text
tool policy 判定需要审批
  -> EventMsg::ExecApprovalRequest  # 发往客户端，不落 rollout
  -> pending approval waiter 保存在运行时
client Op::ExecApproval
  -> submission_loop
  -> notify_approval 唤醒 waiter
  -> 执行或拒绝
  -> 完整 tool output 进入 ResponseItem 历史
```

pending oneshot waiter 不可恢复，所以进程崩溃后不能靠 rollout 继续原审批；恢复只能依靠已提交的 durable item 与 turn boundary。

## 13. 顺序、重复、背压和失败边界

### 13.1 顺序

单 Session 的 `tx_event` 保持发送顺序，但并行 tool future 允许不同 call id 的事件交错。客户端只应依赖同一 `turn_id + item_id/call_id` 的局部生命周期，不应假设所有工具严格串行。

### 13.2 重复与兼容

canonical item event 之后可能紧跟 legacy event；item/call id 是去重和合并依据。流式 done item 缺失 id 时优先继承 active item id，否则生成新 id，从而保证同一次 started/delta/completed 生命周期可关联；这不等价于跨独立 retry 保证相同 id。

### 13.3 背压

- submission channel 是容量 512 的 bounded channel，满时发送方等待；event channel 是 unbounded，不提供同样的生产端背压，因此更依赖上游限制 delta 数量和 payload 大小。
- rollout writer 是 bounded 256 queue，并通过 flush barrier 等待 durable write。
- stdout/token delta 还需在工具层限制数量和 payload 大小，不能只依赖最终 socket 队列。

### 13.4 中断与不完整生命周期

`TurnAborted` 可以在部分 item 尚未 completed 时出现。客户端必须关闭 pending 展示，但保留已经完成的 item 和已经发生的外部副作用。恢复器只使用已落盘记录，不尝试恢复断开的 SSE、tool future、外部进程句柄或 approval waiter。

## 14. 测试证据与设计评价

值得持续关注的测试包括：

- `record_conversation_items_stamps_missing_turn_id_and_preserves_existing_turn_id`：验证 history id 归属。
- `record_response_item_and_emit_turn_item_emits_hook_prompt_lifecycle`：验证 raw item 与 started/completed 的事件序列。
- rollout policy tests：验证 `ItemCompleted`、review mode 等在 Legacy/Paginated 下的差异。
- writer recovery tests：验证写失败后保留 pending suffix，并在下一 barrier 重试。
- App Server event mapping tests：验证 core item 到 v1/v2 notification 与 request 的投影。
- reconstruction tests：验证 compaction、rollback、world-state、turn context 的恢复顺序。

整体设计的优点是把 provider wire、模型上下文、运行时事件和产品 API 解耦，并用统一 rollout 承载多个投影。代价是同一事实可能同时存在 `ResponseItem`、`TurnItem`、canonical `EventMsg`、legacy `EventMsg` 和 `ServerNotification`。新增协议事件时必须同时回答四个问题：是否进入模型历史、是否落 rollout、如何被 App Server 投影、resume 时由哪个 reducer 消费；只增加 enum 变体是不完整的实现。
