# Skills 与 Subagent

## 先区分能力形态

| 形态 | 是否自主决策 | 是否独立上下文 | 适用场景 |
|---|---:|---:|---|
| Tool | 否 | 否 | 确定性读写或外部能力 |
| Skill | 局部 | 否 | 可复用指导、领域流程和工具用法 |
| Subagent | 是 | 是 | 可独立调查、需要上下文隔离的有界子任务 |
| Workflow node | 通常否 | 共享状态 | 固定校验、转换、审批和路由 |

不要因为任务能拆分就创建 Agent。单 Agent、Skill 或确定性节点能解决时，它们更容易测试和治理。

## Skill 渐进披露

启动时只向模型暴露 Skill 名称、描述和路径；调用 `skill` 工具或 `/skill` 后才加载正文。这样避免把全部领域知识塞入每轮上下文。

搜索顺序以仓库为先、用户目录为后，因此项目 Skill 可以覆盖同名全局 Skill：

```text
<repo>/.innoagent/skills
<repo>/.agents/skills
~/.innoagent/skills
~/.agents/skills
```

Skill 是不可信扩展内容。加载时应保持 system policy、权限和工具 schema 的优先级，不能让 Skill 自行扩大能力范围。

## Subagent 委派

当前 Subagent 是主 Agent 的有界只读能力：

- 输入显式 task、role 和 allowed tools。
- 不继承父 Agent 的消息历史，只接收最小任务上下文。
- 工具集合收窄到 `read`、`ls`、`grep`。
- 不能调用 `subagent`，防止递归扇出。
- 最多执行固定轮数，usage 合并回父 session。
- 工具事件标记为 `subagent` stage，便于 TUI 和审计区分。

这属于 agent-as-tool，而不是完整多 Agent 平台。它的价值是上下文隔离和只读调查，不是模拟“角色团队”。

## 扩展条件

只有出现以下需求时才应升级为独立多 Agent：

- 不同权限或凭据必须硬隔离。
- 子任务需要独立生命周期、暂停恢复或远程执行。
- 子任务可独立验收，且并行收益能够覆盖通信与合并成本。
- 不同模型、环境或团队拥有明确责任边界。

升级后需要补齐 delegation contract、预算传递、artifact 所有权、取消传播、结果 verifier 和 fan-in 冲突处理。
