# 质量与安全

## 测试分层

Agent 的最终文本正确，不代表链路正确。InnoAgent 的测试应持续覆盖四层：

| 层级 | 重点 |
|---|---|
| 组件 | schema、路径、权限、压缩、Skill 解析 |
| 节点 | Planning、Reflection、模型流、工具 guardrail |
| 组合 | LangGraph 路由、并行工具、审批恢复、steering |
| 端到端 | session 恢复、TUI 输入、真实 PTY 与非 TTY 启动 |

关键断言不是“返回了一段文字”，而是状态、事件顺序、允许/禁止动作、文件效果和 finish reason 均符合契约。

## 评测原则

- 环境真值和确定性检查优先于 LLM Judge。
- Goal completion 同时检查结果、过程权限和副作用。
- 高风险失败不能被平均成功率掩盖。
- 模型具有随机性，正式评测应记录配置并执行多次 trial。
- 成本以成功任务为分母，关注 token、工具调用、反思轮数和人工审批。

当前单元测试是工程回归基线，不等于能力评测。后续应建立固定 coding tasks、故障注入、安全用例和 baseline/candidate 对比。

## 安全模型

所有模型输出、网页、文件、Tool Result、Skill 和 Subagent 返回都视为不可信输入。安全依赖代码边界，而不是提示词自律：

- 路径在执行前解析并限制到 workspace。
- 权限模式和持久规则由 Runtime 决策。
- deny 优先于 allow。
- 写操作与 shell 不并行。
- 子智能体只获得收窄后的只读能力。
- 会话和日志不保存 API Key 等凭据。

当前 shell 直接运行在本机，是最重要的剩余风险。生产环境需要容器或 MicroVM、默认禁网、资源限额、短期凭据、依赖供应链扫描和可撤销 kill switch。

## 可观测性

事件已经提供最小可观测骨架：session、turn、stage、call、approval、usage、compaction 和 finish reason。生产化应继续增加：

- 稳定 trace/run/task/operation id 与因果关系。
- 模型、Prompt、工具、Policy 和环境版本向量。
- P50/P95 延迟、失败率、循环率、接管率和 cost per success。
- 内容分级、脱敏、采样和保留策略。
- First Bad Event 与可导出的 debug bundle。

Trace 用于解释过程，不能替代文件、Git、测试或外部业务系统的真实结果。
