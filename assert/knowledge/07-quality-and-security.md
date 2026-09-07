# 质量、安全与可观测性

## 质量目标

Agent 质量不能只看最终文字是否“像正确答案”。InnoAgent 的工程质量需要同时验证：

- 状态转换是否正确。
- 工具参数和权限是否受控。
- 副作用是否真实发生且未重复。
- 事件顺序是否可解释。
- Goal 是否由证据完成。
- Session 是否能从稳定边界恢复。
- TUI 是否在并发输出时仍可操作。

## 测试分层

| 层级 | 关注点 | 当前测试 |
|---|---|---|
| Schema/组件 | Pydantic、路径、Permission、Skill、Memory | `test_tools`、`test_permissions`、`test_skills`、`test_memory` |
| 节点 | Planning、Reflection、压缩、模型流 | `test_planning`、`test_reflection`、`test_compression`、`test_streaming_model` |
| 组合 | LangGraph 路由、审批恢复、并行、steering | `test_main_loop`、`test_parallel_tools`、`test_advanced_runtime` |
| 交互 | Slash、TUI、非 TTY、事件渲染 | `test_cli` |
| 持久化 | JSONL、checkpoint、事件重建 | `test_session` |
| 配置 | 项目、环境、全局优先级 | `test_model_config` |

组件测试定位局部错误，组合测试验证模块之间的契约。Agent 系统最常见的回归发生在边界，例如工具返回结构改变但 Runtime 未更新，因此不能只测试单个类。

## 测试应断言什么

优先断言：

- `finish_reason` 和下一跳。
- 文件最终内容、权限位和哈希冲突。
- ToolResult status、metadata 和 warnings。
- approval pending 与恢复后的调用。
- 事件类型、stage、call id 和顺序。
- Plan/Task/Reflection 的结构化字段。
- context 压缩前后 token 与最近 turn 保留。
- Subagent 看不到父历史且不能使用写工具。

不应只断言：

- 返回字符串非空。
- 模型说“完成了”。
- 某个内部方法被调用，但环境效果未验证。

## FakeModel 设计

测试使用可预测模型代替真实网络调用。FakeModel 应能表达：

- 普通文本完成。
- 单个和多个 tool calls。
- 流式文本和 reasoning delta。
- 工具参数分片。
- usage。
- 模型错误。
- 重复调用和边界 steering。

这样测试的是 Runtime 协议，不依赖特定 Provider 的在线稳定性。

## 当前可观测组件

`observe/metrics.py` 提供 `RunMetrics`，可累计 iteration、tool、planning、reflection、guardrail block 和 error。

`observe/traces.py` 提供 `TraceStore`，可保存带 UTC 时间和 monotonic elapsed 的事件，并可选追加到 JSONL。

重要现状：这两个模块目前是基础组件，尚未作为默认 sink 接入 `EventDrivenAgent._emit()`。主运行时已经产生足够的 `AgentEvent`，但正式指标聚合、Trace exporter 和 CLI debug bundle 仍需后续集成。文档不能把它描述为完整观测平台。

## 推荐的事件到指标映射

| 事件 | 指标 |
|---|---|
| `turn.started/completed/failed` | turn latency、success、finish reason |
| `item.completed:tool_result` | tool latency、status、block/error rate |
| `approval.requested/resolved` | approval rate、decision、wait time |
| `context.compaction.completed` | compression ratio、frequency |
| `steering.queued/applied` | intervention rate、application delay |
| `item.completed:reflection` | reflection count、goal completion |
| `response.completed` | token、model latency、reasoning usage |

指标必须按 session/turn/stage/call 建立因果关系，不能只做全局计数。

## 安全信任模型

以下内容全部视为不可信：

- 模型输出。
- 仓库文件和注释。
- 网页与外部 API 内容。
- ToolResult 文本。
- Skill 正文。
- Subagent 报告。
- 恢复的旧 Session 文本。

不可信意味着它们可以提供数据和建议，但不能修改系统权限、绕过 Guardrail 或改变用户真实目标。

## 纵深防御

### Prompt 层

系统 Prompt 明确禁止绕过权限、伪造工具结果和执行破坏性命令。它减少误行为，但可被模型错误影响，因此不作为唯一保护。

### Schema 层

严格 Pydantic schema 拒绝未知参数、非法范围和越权工具名，防止自然语言约束与真实输入不一致。

### Guardrail 层

路径、模式、审批、持久 deny 和 Plan 前置条件由代码决定。模型无法通过修改参数字段关闭这些策略。

### 执行层

只读工具并行，写操作串行；write 使用原子替换和哈希冲突检测；Shell 有超时和 POSIX 进程组清理。

### 验证层

Goal completion 应由测试、文件状态和结构化结果支持。Reflection 不能把模型自述当作唯一证据。

## 主要剩余风险

### Shell 不是沙箱

Shell 运行在宿主机，可访问当前用户权限范围内的网络、文件和进程。cwd 限制不能阻止命令使用绝对路径。生产环境应使用默认禁网的容器或 MicroVM，并设置 CPU、内存、文件和进程限制。

### Session 非事务

工具副作用与 checkpoint 落盘没有原子关系，崩溃后可能出现 unknown effect。需要 operation ledger 和对账，而不是自动重放。

### Prompt Injection

文件、Skill 和工具输出可能包含恶意指令。当前系统通过 Prompt 分层和代码权限降低风险，但尚无内容来源标记、污点传播或敏感操作二次 verifier。

### 凭据存储

模型 API Key 可写入项目或用户配置 JSON。当前没有 OS keychain 集成和文件权限强化，项目配置还可能被误提交。应依赖 `.gitignore`、环境变量或后续 keychain backend。

### Memory 污染

当前启发式 Memory 会记录最近请求和短回答，缺少显式用户确认、TTL 和冲突解决。Memory 默认只是低信任上下文，不应影响权限和事实判断。

## 评测设计

单元测试保证工程回归，不等于 Agent 能力评测。正式评测集应包含：

- 多文件修改与回归测试任务。
- 模糊需求下的澄清能力。
- 工具失败、超时和部分成功。
- Prompt Injection 和路径逃逸。
- 审批拒绝与 allow always。
- Session 崩溃恢复。
- Context 压缩后约束保持。
- Goal 未完成时的 Reflection 回流。
- 用户执行中纠偏。
- 长任务的成本与无进展循环。

每个任务至少记录：结果正确性、过程合规性、副作用、人工接管、token、延迟和 finish reason。高风险失败必须单独统计，不能被平均成功率掩盖。

## 发布门禁

建议采用：

```text
unit/integration
-> offline fixed tasks
-> shadow
-> canary
-> staged rollout
```

模型、Prompt、Workflow、Tool、Policy、Skill 和环境都应带版本。只记录模型名不足以重现实验。

## Debug Bundle

未来可导出的调试包应包含：

- 脱敏后的 session/turn id。
- 模型与配置版本，不含 API Key。
- 稳定事件序列。
- ToolResult metadata 和 exit code。
- Plan、Task、Reflection。
- Context usage 和 compaction 记录。
- Git diff 与测试摘要。
- First Bad Event 定位。

不应默认包含完整文件正文、Secret、reasoning 原文或无限制工具输出。

## 扩展约束

- 新功能至少增加组件测试和一个跨模块测试。
- 修复安全问题时增加能证明旧行为失败的回归测试。
- 观测 sink 只能消费事件，不改变 Runtime 决策。
- 日志字段必须经过脱敏和大小限制。
- 性能优化不能牺牲事件顺序、权限或恢复语义。
- LLM Judge 只能补充开放式质量判断，不能替代确定性 oracle。
