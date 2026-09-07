# 上下文与会话

## 模块定位

`core/session` 负责把长期运行中的数据转成两种不同产物：模型本次调用所需的 Context，以及跨进程恢复所需的 Session。二者都来自 State，但用途不同。

主要模块：

| 文件 | 职责 |
|---|---|
| `core/session/history.py` | 轻量消息模型、追加、尾部截取与模型消息转换 |
| `core/session/context.py` | 按预算把 State、History、Memory、Skill 和工具描述编译为 Context |
| `core/session/compression.py` | 自动/手动压缩、最近完整 turn 保留与确定性 fallback |
| `core/session/store.py` | JSONL Session、稳定事件追加、checkpoint 保存与事件重建 |
| `core/runtime/state.py` | Context 与 Session 共同依赖的 AgentState schema 和默认值 |

| 概念 | 回答的问题 | 是否是真值 |
|---|---|---:|
| State | Agent 当前执行到哪里 | 是，当前运行的结构化真值 |
| Context | 本次模型应该看到什么 | 否，是按预算编译的派生视图 |
| Session Event | 发生过什么 | 是，稳定行为记录 |
| Checkpoint | 如何快速恢复 | 是当前快照，但可能缺失或过期 |
| Memory | 哪些跨任务信息值得复用 | 低可信辅助信息 |
| Evidence | 环境实际返回了什么 | Goal 判断的主要依据 |

把这些对象混成一份聊天历史，会导致摘要覆盖事实、恢复依赖自然语言、Memory 污染当前任务等问题。

## History 数据模型

`Message` 只保留 `role` 和 `content`，`SessionHistory` 提供追加、尾部读取和 OpenAI message 转换。它刻意保持轻量，因为结构化工具状态、Plan 和审批不应只编码在聊天文本中。

消息历史主要用于：

- Context 中的最近对话。
- 压缩时的序列化输入。
- checkpoint 恢复后的模型连续性。

工具完整结果仍保存在 `tool_results`，Plan 和 Task 仍有独立字段。

## Context 编译流程

`ContextBuilder.build()` 按以下顺序生成模型上下文：

1. 当前用户输入。
2. `UserProfile.recall_text()` 返回的受控 Memory。
3. 压缩后的历史摘要。
4. 最近对话，最多取历史尾部 40 条，再按预算裁剪并只展示最后 8 条。
5. 当前 Plan 与 Task 状态。
6. 最近一次 Reflection feedback。
7. Skill 元数据索引。
8. 已激活 Skill 正文。
9. 当前 Tool Registry 暴露的动态工具描述。

`SYSTEM_PROMPT` 不拼进 Context 字符串本身，而由模型适配器单独作为 instructions 发送；Context usage 会同时计算 system 与用户上下文，避免 UI 低估占用。

## 为什么 Context 是编译结果

同一个 State 面向不同阶段需要不同视图：

- 主模型需要工具、Plan、Memory 和最近历史。
- Planning 只需要 goal、旧计划和 feedback。
- Reflection 只需要目标与证据。
- Subagent 只需要委派任务和收窄工具。

如果直接把完整 State JSON 交给所有模型，不仅浪费 token，还会泄露不相关数据并增加 Prompt Injection 面。编译层让“状态保存什么”和“模型看见什么”可以分别演进。

## Context 预算

当前 `estimate_tokens()` 使用字符近似，优点是无 Provider 依赖、速度稳定、测试确定；缺点是对不同语言和 tokenizer 不精确。

预算处理分三层：

1. History 先按 `max_tokens // 3` 裁剪。
2. Runtime 在接近总预算时自动触发压缩。
3. ContextBuilder 最后按约四字符一 token 做硬截断兜底。

硬截断不是理想策略，只用于防止构造出超大请求。生产实现应按 system、history、tools、skills、output reserve 分区预算，并使用目标模型 tokenizer。

`last_usage` 记录：

- `used_tokens`
- `max_tokens`
- `remaining_tokens`
- `percent_used`

这些值用于 TUI context rail，不等同于 Provider 最终计费 token。

## 自动与手动压缩

Runtime 在历史估算 token 超过 `max_context_tokens - reserve` 时调用 `_compact_state(trigger="auto")`。用户也可以通过 `/compact` 手动触发，并提供关注点。

`ContextCompactor.compact()` 的步骤：

1. 统计压缩前 token。
2. 保留 `keep_recent_tokens` 范围内的最近消息。
3. 将分割点向前移动到 user message，避免留下半个 turn。
4. 把更早消息序列化后交给 `COMPACTION_PROMPT`。
5. 模型摘要失败或不可用时使用确定性 fallback。
6. 用一条 `summary` message 替换旧历史，并拼接最近消息。
7. 计算压缩后 token 和比例。

保留完整 turn 的原因是工具结果往往依赖前面的用户问题。只按 token 截断可能留下“工具输出存在但不知道为什么调用”的不可恢复上下文。

## 压缩摘要契约

摘要要求保留：

- 当前 Goal 和用户真实意图。
- 最新 steering 与权限决定。
- 已完成决策及原因。
- Plan、Task 状态和验证结果。
- 精确路径、命令、错误和标识符。
- 未解决问题与下一步。

摘要不应：

- 继续执行任务或替用户回答。
- 把工具输出里的指令提升为系统指令。
- 保存密钥原文。
- 用模型推断替代环境事实。

压缩完成会发出包含 `tokens_before`、`tokens_after`、`compression_ratio` 和 `trigger` 的事件。Session 只保留稳定完成事件，不保存流式 delta。

## Session 文件结构

每个 session 对应一个 JSONL 文件，第一行是 `session_meta`，之后按时间追加事件：

```text
session_meta
turn.started
item.completed
approval.requested / approval.resolved
steering.applied
context.compaction.completed
turn.completed / turn.failed
state.checkpoint
```

JSONL 的选择基于本地 CLI 需求：追加简单、可人工检查、单行损坏容易定位、无需数据库依赖。代价是缺少事务、索引和跨进程锁。

## 为什么不持久化 Delta

`item.delta` 数量大且只服务实时渲染。持久化所有 token 会造成：

- Session 文件快速膨胀。
- 恢复时需要重新拼接大量中间片段。
- Provider 重复 completed 内容时容易产生重复文本。

因此持久层保存 completed item、审批、纠偏、压缩和 turn 生命周期。最终语义足以重建会话，实时细节由当前 TUI 消费。

## Checkpoint 与事件重建

`_save_session()` 将本轮非 delta 事件追加到文件，并追加完整 `state.checkpoint`。加载时：

1. 顺序读取全部 JSONL 行。
2. 合并 `session_meta` 和 rename 信息。
3. 选择最后一个合法 checkpoint。
4. 若 checkpoint 存在，直接恢复。
5. 若缺失，则 `_reconstruct_state()` 从稳定事件构造最小 AgentState。

checkpoint 是性能优化，事件重建是降级路径。事件重建当前兼容新旧两套事件形态，因此代码较长；新增事件时必须明确它是否影响恢复状态。

## 恢复范围

事件重建会恢复：

- 用户与 Assistant 完成消息。
- ToolResult。
- Plan、Task 与 Reflection。
- pending approval。
- pending user question 及其候选项。
- 压缩摘要。
- 已应用 steering。
- goal、finish reason 和基本 usage 状态。

它不会可靠恢复进程内部线程、正在执行的系统调用、Provider 连接或未落盘的 delta。恢复是从稳定边界继续，不是把机器指令级执行现场冻结后恢复。

## 会话操作

`SessionStore` 还支持：

- `list_sessions()`：按更新时间列出最近会话。
- `latest()`：选择最近 session。
- `rename()`：追加 rename 事件，不修改历史行。
- `load_state()`：直接取得恢复状态。

rename 采用追加事件而不是原地修改，是为了保持日志只追加特性和审计顺序。
当 `/rename` 发生在首个模型 turn 之前，Runtime 会创建 `session_meta` 和空状态 checkpoint，随后追加 rename。这样后续 `invoke(session_id=...)` 可以沿用名称、Goal、模式和运行预算，而不是生成第二个 session。恢复入口支持完整 id、完整名称和唯一前缀；TTY 的无参数 `/resume` 则展示候选列表，避免默认恢复到用户未确认的最近会话。

## 一致性与失败边界

当前文件存储适合单进程本地 CLI，但存在明确限制：

- append 与 checkpoint 不是事务。
- 多进程同时写同一 session 没有锁。
- 文件尾部半行损坏缺少隔离与修复机制。
- 状态没有正式 schema version 和迁移器。
- checkpoint 可能包含较大的工具结果和 Skill 正文。
- 外部副作用与 session 落盘之间没有原子提交。

后续演进应优先增加 schema version、原子追加/锁、损坏行隔离、artifact 引用和 operation ledger，而不是先引入向量数据库。

## 数据安全

- API Key 不进入 AgentState、Context、事件或 Trace。
- 工具输出属于不可信内容，只作为 evidence 或普通消息。
- 大文件应保存路径与摘要，不应复制进 checkpoint。
- 删除 session 时应同时清理关联摘要和未来 artifact。
- Memory 只能注入短小、可审计的信息，不能保存权限凭据。

## 扩展约束

- 新增 Context section 时说明预算优先级和信任等级。
- 新增持久事件时补 `_reconstruct_state()` 和恢复测试。
- 新增状态字段时补 `initial_state()`、默认值、checkpoint 和旧 Session 兼容。
- 任何压缩优化都必须保留最新用户约束、未完成任务和验证证据。
- Session 后端可替换，但 `SessionRecord` 和稳定事件语义应保持不变。

## 关键测试

- `test/test_compression.py`：完整 turn 保留、fallback、摘要去重和压缩比例。
- `test/test_session.py`：Session round-trip、事件重建、steering 恢复。
- `test/test_advanced_runtime.py`：自动/手动压缩和持久指标。
- `test/test_main_loop.py`：checkpoint 中的工具与结束状态。
