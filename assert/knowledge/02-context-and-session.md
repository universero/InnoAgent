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

ContextBuilder 在发起请求前仍使用 `estimate_tokens()` 做裁剪和越界保护，因为此时 Provider 尚未返回 usage。模型响应完成后，Runtime 立即用 Responses API 的 `input_tokens` 替换 UI 中的实际 context 占用；字符估算只保留为 `estimated_tokens` 和下一次请求的 projected usage，不再冒充实际计费数据。

预算处理分三层：

1. History 先按 `max_tokens // 3` 裁剪。
2. Runtime 以最近一次 API 实测 input tokens 为基线，加上之后新增消息的估算量，得到 projected usage；达到配置阈值时自动触发压缩。
3. ContextBuilder 将 system、完整工具 JSON Schema、结构化消息和 Runtime context 统一计入预算，最后按约四字符一 token 做硬截断兜底。

硬截断不是理想策略，只用于防止构造出超大请求。生产实现应按 system、history、tools、skills、output reserve 分区预算，并使用目标模型 tokenizer。

`context_usage` 记录：

- `used_tokens`
- `max_tokens`
- `remaining_tokens`
- `percent_used`
- `estimated_tokens` 和 `projected_tokens`
- `threshold_tokens` 和 `threshold_percent`
- `source`，取值为 `response_api` 或 `estimated`

其中 `source=response_api` 的 `used_tokens` 来自最近一次主 Agent 响应；Planning、Reflection、Compaction 和 Subagent 的请求只进入 session usage，不覆盖主上下文占用。

Session `usage` 逐个累加历史 `response.completed.usage`，包含 `requests`、`input_tokens`、`output_tokens`、`cached_tokens`、`reasoning_tokens` 和 `total_tokens`。`cached_tokens` 属于 input 子集，`reasoning_tokens` 属于 output 子集，因此不会重复加到 total。事件重建会重新累加这些响应；正常 turn 的 checkpoint 和 `turn.completed` 则保存累计快照。

## 自动与手动压缩

Runtime 在 projected usage 达到 `max_context_tokens * compact_threshold` 时调用 `_compact_state(trigger="auto")`。用户也可以通过 `/compact [focus]` 手动触发；该命令直接调用 Runtime 压缩当前持久 Session，不会向主 Agent 发送一条伪用户消息。

运行参数通过 `/context` 管理：

| 命令 | 含义 |
|---|---|
| `/context` | 显示 API 实测占用、projected usage、阈值和累计 usage |
| `/context max 128k` | 设置 context window |
| `/context threshold 80%` | 按比例设置自动压缩点 |
| `/context threshold 100k` | 按绝对 token 设置自动压缩点 |
| `/context keep 12k` | 设置压缩后保留的最近完整历史预算 |
| `/context reset` | 恢复默认参数 |

配置保存在 `.innoagent/config.yaml` 中，运行时上下文参数和模型参数统一持久化。修改后 Runtime 会同步更新 `ContextBuilder.max_tokens` 与 `ContextCompactor.keep_recent_tokens`，无需重启。

`ContextCompactor.compact()` 的步骤：

1. 统计压缩前 token。
2. 保留 `keep_recent_tokens` 范围内的最近消息。
3. 将分割点向前移动到 user message，避免留下半个 turn。
4. 把更早消息序列化后交给 `COMPACTION_PROMPT`。
5. 模型摘要失败或不可用时使用确定性 fallback。
6. 用一条 `summary` message 替换旧历史，并拼接最近消息。
7. 计算压缩后 token 和比例。

保留完整 turn 的原因是工具结果往往依赖前面的用户问题。只按 token 截断可能留下“工具输出存在但不知道为什么调用”的不可恢复上下文。

`Message` 会保留 assistant 的 `tool_calls` 以及 tool message 的 `tool_call_id`、`name`。ContextBuilder 把裁剪后的结构写入 `last_messages`，Runtime 再保存为 `_model_messages`；Responses adapter 只发送这组消息和 `_runtime_context`，不会因为 `state.messages` 存在就绕过预算。压缩前后的序列化同样保留这些字段，因此 function call 与 function call output 始终可以配对。

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

压缩完成会发出包含 `tokens_before`、`tokens_after`、`compression_ratio`、`trigger` 和压缩后 `context_usage` 的事件。Session 只保留稳定完成事件，不保存流式 delta。

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

`approval.requested` 的 payload 保存完整 `ApprovalRequest`，包括 request id、calls、deferred calls、reason 和结构化 options。事件重放直接恢复 pending approval 及其后续顺序；旧版字符串 options 与旧 `needs_confirmation` 事件会先升级为当前内存结构。升级只重建待处理意图，真正恢复执行时仍重新进行当前版本的 schema 与 Guardrail 校验。

## Checkpoint 与事件重建

`_emit()` 在每个稳定非 delta 事件产生时立即追加 JSONL；`_save_session()` 只追加完整 `state.checkpoint`。因此模型解析异常或 turn 中途崩溃时，已经展示的 completed、tool、approval 和 `turn.failed` 事件不会全部滞留在内存。加载时：

1. 顺序读取全部 JSONL 行。
2. 合并 `session_meta` 和 rename 信息。
3. 选择最后一个合法 checkpoint。
4. 从 checkpoint 恢复后，继续重放它之后的稳定事件。
5. 若缺失 checkpoint，则 `_reconstruct_state()` 从全部稳定事件构造最小 AgentState。

checkpoint 是性能优化，事件重建是降级路径。事件重建当前兼容新旧两套事件形态，因此代码较长；新增事件时必须明确它是否影响恢复状态。

状态重建和界面历史回放不能混为一谈。`SessionStore.load()` 使用 checkpoint 加后续事件恢复最新 AgentState，目标是让 Runtime 能继续执行；TUI `/resume` 则读取 JSONL 中的稳定事件，筛选最近 20 个 turn、最多 300 个可见事件，按原顺序重新写入 terminal scrollback，目标是让用户看到之前发生过什么。UI 回放不重新执行工具，也不修改恢复后的 State。

历史 `item.completed:message/reasoning` 可能带有 `streamed=true`，表示它在原进程中已经通过 delta 显示。但 delta 按设计不持久化，因此 UI 回放会在副本上把该标记改为 `false`，再渲染 completed 正文；原始事件不修改。`session_meta`、checkpoint、`response.completed` 等 bookkeeping 不进入 transcript，避免暴露内部状态或重复最终响应。

`turn.started` 同时保存 `user_input`、`goal` 和 `goal_restarted`。重放遇到 Goal 切换或显式重启时，必须先清除旧 Plan、Task、Reflection、工具结果和错误，再应用新 turn；否则缺失 checkpoint 时会把旧目标证据错误地归入新目标。

## 恢复范围

事件重建会恢复：

- 用户与 Assistant 完成消息。
- ToolResult。
- Plan、Task 与 Reflection。
- pending approval。
- pending user question 及其候选项。
- 压缩摘要。
- 已应用 steering。
- goal、finish reason、usage 和 context usage。

它不会可靠恢复进程内部线程、正在执行的系统调用、Provider 连接或未落盘的 delta。恢复是从稳定边界继续，不是把机器指令级执行现场冻结后恢复。

## 会话操作

`SessionStore` 还支持：

- `list_sessions()`：按更新时间列出最近会话。
- `latest()`：选择最近 session。
- `rename()`：追加 rename 事件，不修改历史行。
- `load_state()`：直接取得恢复状态。

rename 采用追加事件而不是原地修改，是为了保持日志只追加特性和审计顺序。
当 `/rename` 发生在首个模型 turn 之前，Runtime 会创建 `session_meta` 和空状态 checkpoint，随后追加 rename。这样后续 `invoke(session_id=...)` 可以沿用名称、Goal、模式和运行预算，而不是生成第二个 session。恢复入口支持完整 id、完整名称和唯一前缀；TTY 的无参数 `/resume` 则展示候选列表，避免默认恢复到用户未确认的最近会话。

Session 候选的主标签使用名称或 id，说明列只展示本地更新时间和最近一次用户输入，并对长输入截断。这样不会重复 id，也不会把可能长期不变的 Goal 误当作会话最近内容；最近输入优先读取 `state.user_input`，缺失时回退到最后一条 user message。

模型输入分为 Runtime context 与 conversation input。前者只包含记忆、压缩摘要、计划、Skill 等运行信息；后者保留 user/assistant 消息，以及通过同一 `call_id` 配对的 `function_call` 和 `function_call_output`。工具结果如果只被拼成普通文本，模型无法可靠确认调用已经完成，容易再次调用同一工具。通用模型仍可使用扁平上下文，Responses API 客户端则优先发送结构化输入。当前用户消息已经存在于 conversation 时，不再额外重复拼接。

活动 Goal 属于稳定 Runtime context，每轮以“当前目标”单独注入，不能只依赖较早的用户消息。`/goal <text>` 还会把同一文本作为当前 turn 的用户消息，因此首轮意图清晰，后续工具循环和恢复运行也不会丢失目标。

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
