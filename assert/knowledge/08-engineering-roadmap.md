# 工程路线图

路线图按风险和依赖排序，不以功能数量排序。原则是先补可信执行与恢复，再扩大自治范围。

## 当前基线

- LangGraph 主图与 Planning、Reflection 子图。
- 文件、搜索、shell、Task、Skill 和 Subagent 工具。
- ask/auto/readonly、一次允许/持久允许/拒绝。
- 只读工具并行，写操作串行。
- JSONL session、上下文压缩、usage 和事件流。
- 全屏 TUI、slash 命令、审批和执行中纠偏。
- 单元测试与模块组合测试。

## P0：可信本地 Coding Agent

- 为写文件和 shell 引入稳定 operation id、请求摘要和重复执行保护。
- 在现有原子写和 SHA-256 冲突检查上增加 patch/diff 与变更预览。
- 在现有超时、POSIX 进程组清理和结构化结果上，增加实时输出、跨平台进程树清理和细粒度取消。
- 增加 Git diff、测试结果和文件存在性等确定性 verifier。
- 为 session schema、事件和 checkpoint 增加版本与迁移测试。

验收：崩溃、重试、拒绝和恢复不会造成重复或静默副作用。

## P1：可恢复运行时

- 将 in-memory checkpointer 替换为持久化后端，并明确 LangGraph checkpoint 与 session event 的单一职责。
- 将审批、定时器和外部回调建模为 durable interrupt/signal。
- 增加取消传播、deadline、retry budget、无进展检测和故障分类。
- 建立 artifact 引用，避免大文件进入状态、事件和 Prompt。

验收：进程退出后可从明确节点恢复，等待不占用 worker，副作用可对账。

## P2：上下文与能力扩展

- 使用模型原生 tokenizer 和分区预算，显式预留输出与工具调用空间。
- 为 Context Item 增加来源、信任、时间和权限元数据。
- 实现工具候选检索和 schema 渐进披露，避免工具数量增长后全量注入。
- 完善 Memory 写入证据、冲突、TTL、更正和删除，替代当前轻量阈值策略。
- 为 Skill 增加版本、来源信任、依赖和测试清单。

验收：压缩、Skill 和工具扩展不会丢失关键约束或扩大权限。

## P3：隔离执行与多 Agent

- 将 shell/代码执行迁移到默认禁网的隔离环境，并按任务分配临时凭据。
- 只有在权限隔离、上下文隔离或并行收益被评测证明后，才扩展 Subagent。
- 引入 delegation contract、独立预算、artifact 交付、结果 verifier 和 fan-in 规则。
- 需要跨进程或跨组织时再考虑 A2A；工具互操作优先采用 MCP adapter。

验收：子 Agent 失败、超时或返回错误结果时，主任务能检测、降级或部分交付。

## P4：生产保障

- 建立固定任务集、恢复/安全/长尾用例和多 trial 评测。
- 接入 OpenTelemetry/OpenInference 风格 trace，并控制敏感内容采集。
- 采用 offline -> shadow -> canary -> rollout 发布门禁。
- 记录模型、Prompt、Workflow、Tool、Policy、Skill 和环境版本。
- 监控 verified task success、P95、unsafe action、duplicate effect 和 cost per success。

## 明确不做

- 不为展示复杂度而引入多 Agent、消息总线或微服务。
- 不把聊天历史、向量库或模型自述当作任务真值。
- 不依赖 Prompt 实现权限、幂等和安全。
- 不在缺少 trace、oracle 和回归集时直接进入训练优化。
