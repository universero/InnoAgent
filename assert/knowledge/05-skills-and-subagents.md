# Skills 与 Subagent

## 能力形态

InnoAgent 将可扩展能力分为四类，避免把所有功能都包装成 Agent：

| 形态 | 自主决策 | 独立上下文 | 典型用途 |
|---|---:|---:|---|
| Tool | 否 | 否 | 文件、Shell、状态更新等确定性动作 |
| Skill | 局部指导 | 否 | 领域流程、规范和工具使用方法 |
| Subagent | 是 | 是 | 有界、可独立验证的只读调查 |
| Workflow node | 通常否 | 共享 State | 路由、审批、校验、压缩和状态转换 |

选择原则是使用满足需求的最小能力形态。Skill 能解决的问题不创建 Subagent，确定性节点能解决的问题不交给模型。

主要模块：

| 文件 | 职责 |
|---|---|
| `core/skill/loader.py` | Skill 目录发现、frontmatter 解析、索引和正文加载 |
| `core/tool/skill_tool.py` | 把按名称激活 Skill 暴露为主 Agent 工具 |
| `core/agent/subagent.py` | 只读子循环、上下文隔离、预算累计和返回契约 |
| `core/tool/subagent_tool.py` | Subagent 参数 schema、allowlist 和工具入口 |
| `core/prompts.py` | Skill 信任规则与 `SUBAGENT_PROMPT` |

## Skill 模块

Skill 的主体实现位于 `core/skill/loader.py` 和 `core/tool/skill_tool.py`。

### 目录优先级

`SkillLoader.roots` 按以下顺序搜索：

```text
<workspace>/.innoagent/skills
<workspace>/.agents/skills
~/.innoagent/skills
~/.agents/skills
```

`discover()` 使用 first-match precedence，同名 Skill 只保留最先发现的一份。因此项目级 Skill 可以覆盖用户级 Skill，但用户目录不会意外覆盖仓库约束。

### Skill 文件格式

每个 Skill 使用 `SKILL.md`，最小 frontmatter 为：

```yaml
---
name: code-review
description: Review code for correctness and regressions.
---
```

当前解析器只支持简单 `key: value` 标量，不依赖完整 YAML 库。`name` 必须匹配小写 kebab-case，description 必填。不合法文件在发现阶段被忽略，而不是让启动失败。

选择轻量 frontmatter 解析器的原因是当前只需要两个字段，避免为极小协议引入大型依赖。若未来增加嵌套依赖、版本约束或多行字段，应切换到正式 YAML parser，而不是继续扩写手工解析。

## 渐进披露

启动时 `prompt_index()` 只注入 Skill 的 name、description 和 path。模型或用户显式调用 `skill` 后，才通过 `load()` 读取正文。

```mermaid
flowchart LR
    Discover[扫描 SKILL.md] --> Meta[只解析 metadata]
    Meta --> Index[注入 Skill 索引]
    Index --> Select[模型或用户选择]
    Select --> Load[加载正文]
    Load --> State[写入 active_skills]
    State --> Context[下一轮 Context 注入]
```

渐进披露解决两个问题：

- 避免所有 Skill 正文占用每轮 token。
- 降低无关 Skill 中指令干扰当前任务的概率。

`max_skill_tokens` 默认限制单个 Skill 正文，超出后截断并标记。它保护 Context 预算，但可能切断关键步骤，因此大型 Skill 应拆成索引和按需资源，而不是依赖截断。

## Skill 激活链路

1. `SkillTool` 校验精确名称。
2. `SkillLoader.load()` 重新发现并读取匹配文件。
3. ToolResult 的 output 只返回“已激活”。
4. 完整 Skill 放入 `data.active_skill`。
5. Runtime `_apply_tool_result()` 按名称去重后写入 `state.active_skills`。
6. 下一次 `ContextBuilder` 将正文作为独立 section 注入。

正文不同时放入 tool output，是为了避免同一内容在消息历史和 active Skill 区重复两次。

CLI `/skill <name>` 走 Runtime 的主动激活接口，最终仍使用同一种 State 结构，不维护第二份 UI 专用状态。

## Skill 信任边界

Skill 是仓库或用户提供的文本，不是系统策略。它可以指导：

- 任务步骤和质量标准。
- 何时使用已有工具。
- 输出格式和领域约定。

它不能：

- 绕过 PermissionStore 或 Guardrail。
- 扩大 Subagent 工具集。
- 覆盖用户当前请求。
- 将外部文本声明为可信系统消息。
- 要求记录或泄露凭据。

Prompt 中显式声明 Skill 为不可信指导，是纵深防御；真正约束仍由代码权限完成。

## Subagent 定位

当前 Subagent 是 agent-as-tool，而不是多 Agent 平台。它适合：

- 在隔离上下文中调查一个明确问题。
- 并行架构未来可用时执行独立只读分析。
- 避免把大量探索结果塞进父 Agent 历史。

不适合：

- 修改文件或执行 Shell。
- 需要父会话全部隐含上下文的任务。
- 与主 Agent 共享可变计划并同步写入。
- 无边界的“帮我把剩余工作做完”。

## Subagent 输入契约

`SubagentInput` 包含：

- `task`：自包含任务和预期交付物。
- `role`：简洁专业角色，默认 researcher。
- `allowed_tools`：只允许 `read`、`ls`、`grep`。

Pydantic schema 已限制 allowed tools，`SubagentRunner` 又与 `READ_ONLY_TOOLS` 和当前 Registry 求交集。双层检查避免未来 schema 或调用方变化导致权限扩张。

## 隔离执行

`SubagentRunner.run()` 创建局部状态：

```text
user_input
messages: 仅委派任务
tool_results
errors
```

它不复制父 Agent 消息历史、Plan、Memory、激活 Skill 或审批状态。Subagent Prompt 只包含 role、task 和固定只读规则。Runner 将这部分写入独立 `_runtime_context`，并把局部结构化历史写入 `_model_messages`；Responses adapter 因此不会遗漏 `SUBAGENT_PROMPT` 或退回父会话历史。

这种隔离的目的不是保密边界，而是：

- 控制 token。
- 减少父任务中的无关指令。
- 强迫委派者提供完整任务描述。
- 让返回结果更容易独立评审。

## 子循环

Subagent 最多运行 6 轮：

1. 调用共享 `ModelStreamConsumer`，stage 标记为 `subagent`。
2. 没有工具调用时返回模型 summary 或最后工具输出。
3. 工具超出 allowlist 时立即返回 error。
4. 合法只读调用交给 Registry，ToolContext 强制 `readonly`。
5. ToolResult 写入局部消息，再进入下一轮。
6. 超过轮数返回明确 iteration limit error。

usage 在子循环内部累计后放入返回数据，由父 Runtime 合并到 session usage。

## 为什么当前不并行执行 Subagent

虽然 Subagent 工具本身只读，但它复用父 Runtime 的模型客户端、ModelStreamConsumer 和事件 emit 通道。并发调用可能造成 Provider 客户端、流式事件和 stage 展示交错，因此 `parallel_safe=False`。

未来要并行，至少需要：

- 每个 Subagent 独立模型流消费者。
- 独立 run/subagent id。
- 并发预算与取消传播。
- 事件 fan-in 排序规则。
- 结果 artifact 与冲突处理。

不能仅因为工具集只读就认定整个 Subagent 执行器线程安全。

## 返回契约

Subagent 返回：

- `summary`：父 Agent 主要消费的结论。
- `tool_results`：支持结论的只读证据。
- `usage`：模型 token 汇总。
- `error`：模型失败、越权或轮数上限。

父 Agent 应把 Subagent 输出视为二手报告。涉及目标完成、文件事实或安全判断时，应由主 Agent 或确定性 verifier 复核。

## 何时升级为多 Agent 平台

只有以下需求被实际证明时才升级：

- 不同凭据或权限必须硬隔离。
- 子任务需要独立暂停、恢复和远程生命周期。
- 独立任务可并行验收，收益覆盖通信与合并成本。
- 不同模型或团队拥有明确 artifact 所有权。

升级后必须增加 delegation contract、预算传递、独立 checkpoint、取消传播、结果 verifier、fan-in 和冲突解决。仅增加“角色名称”和消息互聊不构成可靠多 Agent 架构。

## 扩展约束

- Skill 新增字段时同步发现、索引、加载和测试。
- 激活正文必须有 token 上限，不得重复注入。
- Subagent 默认最小上下文和最小工具集。
- 子 Agent 结果不能直接更新父 Plan 或标记 Goal 完成。
- 任何写能力升级都必须经过独立权限和 artifact 设计。

## 关键测试

- `test/test_skills.py`：元数据发现、项目覆盖、正文按需加载和去重注入。
- `test/test_advanced_runtime.py`：Subagent 隔离、只读能力和 usage 合并。
- `test/test_tools.py`：Skill 名称和 Subagent allowlist schema。
