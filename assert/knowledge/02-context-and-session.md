# 上下文与会话

## 四类数据

| 对象 | 回答的问题 | InnoAgent 当前实现 |
|---|---|---|
| Context | 这一次模型应该看到什么 | `ContextBuilder` 编译 system、历史、goal、plan、task、Skill 和工具描述 |
| State | 当前 turn 和目标进行到哪里 | `AgentState` 与 session checkpoint |
| Memory | 哪些跨任务信息值得复用 | Profile/Recall 与阈值触发的受控更新 |
| Evidence | 环境实际返回了什么 | 工具结果、错误和文件内容 |

Context 是有预算的派生视图，不是事实源。会话历史可压缩，任务状态和工具结果不能仅从摘要推断。

## Context 编译

当前上下文按以下顺序组装：

1. 当前用户输入与受控用户记忆。
2. 压缩后的历史摘要和最近完整对话。
3. 当前 plan、tasks 与 reflection feedback。
4. Skill 索引和已激活 Skill 正文。
5. 当前权限下可见的工具描述。

预算策略优先保留最近消息，并为 system prompt 计入 usage。当前 token 估算采用稳定的字符近似，适合本地预算和 UI 展示，但不能替代模型原生 tokenizer。

## 压缩原则

压缩不是简单截断。`ContextCompactor` 将旧消息替换为结构化摘要，并保留最近完整用户 turn，避免从孤立的 assistant/tool 消息开始恢复。摘要至少覆盖：

- 用户目标与约束。
- 已做决策及原因。
- 计划和任务进度。
- 文件、工具结果与验证证据。
- 未解决问题和下一步。

压缩事件记录 `tokens_before`、`tokens_after`、`compression_ratio` 和触发原因。模型摘要失败时使用确定性 fallback，保证会话仍可继续，但 fallback 只提供恢复框架，不代表完整事实。

## JSONL Session

每个 session 使用独立 JSONL 文件：

- 事件按发生顺序追加，便于审计和回放。
- `item.delta` 只服务实时显示，不持久化，避免日志膨胀。
- `state.checkpoint` 是快速恢复缓存；缺失时可从稳定事件重建关键状态。
- rename、approval、steering、compaction 和 turn completion 都有明确事件。

当前文件写入适合单进程本地 CLI。生产环境需要增加原子追加、锁或事务、schema version、损坏隔离和迁移策略。

## 数据边界

- 文件系统和 Git 才是代码真值，session 只保存观察和引用。
- 工具输出是不可信数据，不能提升为 system instruction。
- 压缩摘要必须能回到原事件；删除 session 时也应删除派生摘要。
- 不应把 API Key、审批凭据或大文件正文写入 context 和事件日志。
