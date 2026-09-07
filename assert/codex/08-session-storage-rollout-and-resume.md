# 08. 会话存储、Rollout 与恢复

## 1. 模块职责

持久化模块要在进程退出、客户端断连、版本升级和 thread fork 后恢复 Agent 语义，同时不尝试序列化不可恢复的 runtime 句柄。

## 2. 四种状态表示

- core `Session`：实时对象，持有 `SessionState`、event sender、active turn、input queue、services 等，不可整体序列化。
- `SessionState.history: ContextManager`：当前模型上下文，保存 `ResponseItemEnvelope`，会过滤非 API item 并裁剪工具输出。
- rollout JSONL：canonical append-only log，保存经 policy 选择的 `RolloutItem`，是恢复的权威来源。
- SQLite/thread store 与 App Server `ThreadState`：分别服务跨进程查询投影和在线 UI 投影，不是模型上下文本身。

四者不是副本。内存 Session 负责运行，`ContextManager` 负责下一次模型请求，rollout 负责因果历史，SQLite 和 `ThreadState` 负责产品查询与当前 turn 展示。

核心对象包括 `RolloutRecorder`、`RolloutItem`、`ThreadStore`、`LiveThread`、thread/turn/item records 和 pagination cursor。存储 API 不暴露 Session mutex，而通过不可变记录和投影读取。

## 3. 写入链路

```text
ResponseItem 路径：
用户输入 / model completed item / tool output / context update
  -> Session::record_conversation_items
  -> ContextManager::record_annotated_items
  -> RolloutItem::ResponseItem
  -> persistence policy
  -> JSONL

EventMsg 路径：
turn/item/tool/approval/delta/lifecycle event
  -> Session::send_event[_raw]
  -> RolloutItem::EventMsg 候选
  -> persistence policy 选择或丢弃
  -> JSONL（若保留）
  -> tx_event
  -> App Server 投影
```

`record_prepared_conversation_items` 的顺序是先写内存 history，再尝试持久化 `ResponseItem`，最后发布 `RawResponseItem`。`send_event_raw_with_persistence` 则在发布 event channel 前先尝试持久化候选 `EventMsg`。两条链最终都调用 `LiveThread::append_items`，但只有通过 rollout policy 的记录才真正写入。

`Session::persist_rollout_items` 会记录存储错误但不向上返回，因此这是“先尽力持久化，再发布”的顺序，不是事务提交。磁盘持续失败时，内存历史和客户端可见状态可能领先于 durable rollout。

## 4. rollout

rollout 使用追加式记录，避免每次 turn 重写整个会话。[RolloutLine](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/history/src/lib.rs#L254) 包含 timestamp、单调 ordinal 和 flattened `RolloutItem`。记录类型包括：

- `SessionMeta`、`ResponseItem`、inter-agent communication。
- `Compacted`、`TurnContext`、`WorldState`、`RetainedContext`。
- `TokenUsageRecord`、`SecurityRiskScore`。
- 经 policy 选择的 `EventMsg`。
- paginated history 使用的 `RealtimeItem`。

`RolloutItemWire` 以 `type: snake_case` 和 `payload` 序列化。`ResponseItemEnvelope.metadata` 独立于 provider item 保存，使 resume 后仍能恢复 user input order、继承消息标记和 fallback truncation budget。

### 4.1 持久化 policy

统一入口 [is_persisted_rollout_item](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/rollout/src/policy.rs#L10) 过滤记录：

- `ResponseItem` 保存消息、reasoning、工具调用/输出、配置和 compaction；丢弃临时 `AdditionalTools`、`CompactionTrigger`、`Other`。
- turn started/complete/aborted、token count、goal、rollback、settings applied 始终保存。
- delta、approval request、stream error、raw response、tool begin 等瞬态事件不保存。
- Legacy 模式保存旧 message/reasoning/tool-end 事件；Paginated 模式改存 canonical `ItemCompleted(TurnItem)`。

因此 rollout 不是 event channel 的全量抓包，而是恢复语义明确的 canonical log。

### 4.2 writer 的 durable 语义

`RolloutRecorder` 使用容量 256 的 mpsc command queue，支持 `AddItems`、`Persist`、`Flush`、`Shutdown`。LocalThreadStore 的 live writer 会先按 policy 过滤，再 `record_canonical_items + flush`，保证 append 返回前 JSONL 已 flush。

writer 只从 pending queue 删除成功写入的前缀。第一次 I/O 失败后会丢弃旧 file handle、保留未写后缀、重新打开文件再试一次。Paginated 模式在 JSONL flush 成功后才 materialize SQLite，因此索引可以暂时落后，但不应领先于 canonical log。

## 5. ThreadStore 与 LiveThread

[ThreadStore](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/thread-store/src/store.rs#L68) 提供 thread、turn、item、section、project 和分页查询。`LiveThread` 把活动 session 与持久化 thread 视图连接起来。

store 接口允许不同调用表面共享列表和搜索，不必各自扫描所有 JSONL。

## 6. resume

resume 读取 rollout 和 metadata，逆向识别最新有效 compaction、turn segment 与 rollback，再正向重放幸存记录。重建规则不是“把每个 event 再发一遍”：

- `ResponseItem` 重建 `ContextManager`。
- `InterAgentCommunication` 转换为 model input。
- `Compacted.replacement_history` 成为新的历史基线。
- `ThreadRolledBack` 删除最近 N 个 user turn。
- `TurnContext` 恢复 previous settings/reference context。
- `WorldState` 从 full snapshot 开始应用 merge patch。
- `TurnStarted/Complete/Aborted` 提供分段和终态边界。
- 大多数其他 `EventMsg` 对模型历史无作用。

安装到新 Session 前，历史还会重新执行图片/音频准备，然后通过 `replace_annotated_history` 恢复 `SessionState.history`、review/retained context、world-state baseline 和 auto-compaction window。

它不能恢复：

- 已断开的 SSE/WebSocket；
- 内存中的 tool future；
- pending oneshot approval；
- 未持久化的 UI delta；
- 已丢失的外部进程句柄。

归一化会为不完整 tool call 补可接受输出，避免下一次 Responses 请求违反 call/output 配对。

恢复链路还要重新解析当下配置和模型目录。历史事实沿用旧记录，未来行为使用新 Session resolved config；若模型或工具目录变化，必须生成显式 configuration update，不能假装 prompt 前缀未变。

## 7. fork、archive、delete

- resume：沿用 thread identity 和历史继续。
- fork：复制选定历史到新 thread identity，之后独立发展。
- archive：可恢复的可见性/状态变更。
- delete：破坏性删除，语义不同于 archive。

客户端不能把四者折叠为“打开/关闭会话”。

## 8. rollback 与工作区

thread rollback 只删除最近 N 个用户 turn 的上下文，不回滚磁盘。真正的文件撤销需要版本控制或显式 patch。若 UI 把 rollback 描述成“撤销操作”，会制造危险误解。

## 9. compaction 持久化

compaction 安装后写入 rollout，并发 `ContextCompacted`。恢复时应从 compaction 边界构造当前模型历史，同时保留审计所需旧记录。存储层可保留全量，prompt 投影只使用压缩后窗口。

## 10. 一致性风险

- rollout 已写但 SQLite 未更新：历史可恢复，列表暂时陈旧。
- SQLite 有 thread 但 rollout 缺失：可列出但无法完整 resume。
- 通知先发、持久化后失败：客户端看到不可恢复 item。
- 多连接同时 mutation：必须通过 Session submission 顺序化。

理想顺序是先提交语义记录，再发布可恢复通知；若为低延迟先流式展示，则 completed 必须在持久化后确认。

附件和本地图片不能只保存临时路径。持久化层需要记录可重新定位的 URI、复制策略或明确“恢复后不可用”；否则历史文本可恢复但多模态输入失效。

## 11. 测试与评价

测试覆盖 resume、fork identity、archive/unarchive、分页、rollout parsing、compaction 和 schema compatibility。JSONL 加 SQLite 的组合实用但有双写成本；InnoAgent 应定义单一 authoritative log，再把索引视为可重建投影，并提供一致性修复命令。
