# InnoAgent 实施计划

本文档是 InnoAgent 通用 Agent 的落地指导文件。目标是在现有目录骨架和 README 描述的极简状态机基础上，用 LangGraph 实现一个可运行、可观察、可扩展的 Agent。

## 0. 目标架构：事件驱动 Agent

本节是当前实现必须遵循的核心架构。旧的“ModelDecision + nodes 类 + graph.invoke 集中输出”方案仅作为历史参考，后续实现以本节为准。

### 0.1 总体数据流

```text
LLM Responses API
  │
  │ 将 API 响应事件直接转换为 InnoAgent 内部事件
  ▼
事件管线
  ├──► View / Renderer：按事件类型实时渲染
  ├──► Agent 核心链路：组装 delta，形成完整事件后执行
  └──► Session：感知事件并负责存储、恢复、压缩
```

核心原则：

- LLM 客户端只负责调用外部 API，并把 Responses API 事件转换为内部事件。
- 每个 API 产生的 delta 都立即向上传递。
- 事件向上后同时分发给渲染层和 Agent 核心链路。
- Agent 核心链路负责把 delta 组装成完整事件，再决定下一步动作。
- 工具执行成功后，Agent 核心链路也向渲染层发出 `tool_result` 事件。
- Session 是事件管线的感知者；delta 事件不落盘，只落完整事件和压缩事件。
- 不引入 `ModelDecision`、`RuntimeNodes` 这类中间对象；Agent 自己维护一个简单的 ReAct 图。

### 0.2 内部事件定义

事件统一放在 `core/event/` 目录下。

事件基础字段：

```text
type         事件类型
is_delta     是否为增量事件
usage        该事件对应的 token 消耗
stage        阶段：main / plan / reflect
finish_reason 结束原因；仅 finish 事件必填
```

事件类型：

| type | 说明 | is_delta 使用方式 |
| --- | --- | --- |
| `reasoning` | 模型思维链内容 | 流式阶段为 `true`，组装后为 `false` |
| `text` | 模型最终文本内容 | 流式阶段为 `true`，组装后为 `false` |
| `tool_call` | 工具调用开始/声明 | delta 阶段只描述调用目标 |
| `tool_call_argument` | 工具调用参数增量 | delta 阶段为 `true`，组装后合并到 tool_call |
| `tool_result` | 工具执行结果 | 由 Agent 核心链路发出，`is_delta=false` |
| `user_confirm` | Agent 向用户询问，可提供选项或让用户自由回答 | `is_delta=false` |
| `permission_confirm` | 权限确认，确认通过后写入当前目录配置 | `is_delta=false` |
| `compact_start` | 上下文压缩开始 | `is_delta=false` |
| `compacting` | 上下文压缩进行中，需带进度 | `is_delta=false`，但可多次发出 |
| `compact_end` | 上下文压缩完成 | `is_delta=false` |
| `finish` | 一个 response item 或整体响应结束 | `is_delta=false`，带 `finish_reason` |

### 0.3 LLM 响应事件构造

`core/llm/` 中客户端负责：

- 调用 Responses API。
- 解析 SSE 事件。
- 将以下 Responses API 事件转换为内部 delta 事件：
  - `response.reasoning_text.delta` -> `reasoning`
  - `response.output_text.delta` -> `text`
  - `response.output_item.added` + `function_call` -> `tool_call`
  - `response.function_call_arguments.delta` -> `tool_call_argument`
- 解析 `response.completed` / `response.incomplete` / `response.failed`。

API 直接产生的这些内部事件全部为 `is_delta=true`。

### 0.4 事件管线分叉

事件向上后同时进入：

1. View / Renderer
   - `reasoning`、`text` 实时流式渲染。
   - `tool_call`、`tool_result` 结构化渲染。
   - `compact_start`、`compacting`、`compact_end` 渲染压缩进度。

2. Agent 核心链路
   - 接收 delta 事件。
   - 将 `reasoning` / `text` 增量拼接。
   - 将 `tool_call` 和 `tool_call_argument` 组装为完整工具调用。
   - 遇到对应的 `finish` 后，结束当前 item 的组装。
   - 根据完整事件继续执行工具、计划或反思。

### 0.5 完整事件

Agent 核心链路组装出的完整事件仍然是同一批类型：

```text
reasoning
text
tool_call
tool_result
user_confirm
permission_confirm
compact_start
compacting
compact_end
finish
```

区别是：

- `is_delta=false`
- `tool_call` 包含完整参数
- `finish` 包含 `finish_reason`

### 0.6 Agent 与图的关系

- 不再使用 `RuntimeNodes` 这类节点类。
- `core/agent/` 中一个 Agent 类维护一个简单的 ReAct 图。
- 图是 Session 的一个字段。
- 渲染层收到的事件也是 Session 的事件管线中传递的事件。
- Agent 核心链路负责：
  - 组装 delta
  - 执行工具
  - 发出工具结果事件
  - 处理压缩和反思

### 0.7 Session 存储与恢复

Session 使用 JSONL。

存储规则：

- delta 事件不落盘。
- 完整事件落盘。
- 压缩相关事件落盘。
- 每个 JSON 对象是一个完整事件。

CLI 初始化 session 或恢复 session 时：

- 读取 JSONL 历史事件。
- 从历史事件重建 session state。
- 从 `compact_start`、`compacting`、`compact_end` 中重建压缩进度。

### 0.8 配置目录

模型配置放到 `core/config/` 下单独管理。

建议结构：

```text
core/
├── agent/
├── config/
│   ├── model.py
│   └── runtime.py
├── event/
│   ├── base.py
│   ├── llm.py
│   ├── tool.py
│   ├── user.py
│   ├── permission.py
│   └── compact.py
├── llm/
├── memory/
├── planning/
├── reflection/
├── session/
├── tool/
└── view/
```

### 0.9 用户确认与权限确认

确认分为两类，不能混用：

1. `user_confirm`
   - 表示 Agent 主动向用户询问。
   - 可以带选项，也可以让用户自由回答。
   - 用户回答后作为新的 `user_input` 继续 Agent 循环。

2. `permission_confirm`
   - 表示权限确认，不是普通业务询问。
   - 通常在写文件、批量操作、敏感操作前触发。
   - 用户批准后，权限结果记录到当前项目目录配置中。
   - 记录位置：`<project>/.innoagent/permissions.json`。
   - 用户拒绝后，不执行对应工具，`finish_reason` 记为 `denied`。

权限配置由 `core/config/permission.py` 管理，不写入全局配置。

## 1. 目标与范围

### 1.1 目标

完成一个以主 Agent 执行循环为核心的通用 Agent，具备以下能力：

- 使用 LangGraph 组织 `Execute -> Tool Use / Planning / Reflection -> Continue Execute -> Goal Met?` 状态机。
- 主 Agent 始终负责执行，Planning 和 Reflection 作为可选工具或额外环节挂载到主图中。
- 支持基础工具：`Plan`、`Task`、`Write`、`Read`、`Ls`、`Grep`。
- 对工具调用统一接入 Guardrail，尤其是写工具强制“先读后写”，文件操作根据运行模式决定是否需要用户确认。
- 维护简单用户画像，在对话轮次和用户输入 token 数超过阈值后触发异步记忆更新。
- 当用户设置了 goal 时，模型在不再调用工具并准备结束前进入 Reflection，判断目标是否完成；未完成则返回 feedback，驱动继续执行或调整计划。

### 1.2 非目标

第一阶段不追求以下内容：

- 复杂 RAG、多向量库、长期记忆图谱。
- 任意多 Agent 编排和分布式执行。
- 完整权限系统或沙箱文件系统。
- 模型提供商无关的复杂调度层；先聚焦 LangGraph 和单一可替换的模型接口。

## 2. 核心设计原则

1. **主 Agent 统一执行**：所有工具调用、Plan 变更、Reflection 反馈都回到主循环继续执行，避免 Planning 或 Reflection 独立“接管”主流程。
2. **状态显式化**：计划、任务、工具调用结果、反射反馈、goal 状态全部进入 `AgentState`，便于 checkpoint、回放和观察。
3. **工具通过装饰器接入 Guardrail**：工具本身只描述业务能力，Guardrail 通过装饰器包裹执行前、执行后逻辑。
4. **写操作可解释、可恢复**：写前必须读，所有写工具记录目标路径、前置读取结果、变更意图和最终结果。
5. **工具描述是动态上下文**：Plan 产生的计划和 Task 完成状态不仅存在内存中，也反映到工具描述或工具返回结果中，使 Supervisor 在后续轮次能看到当前进度。
6. **Reflection 只反馈，不直接重写计划**：Reflection 输出自然语言 feedback；是否继续执行、如何修改计划由主 Agent 决定，必要时调用 Planning 工具调整。

## 3. 目录结构

沿用并补全现有目录：

```text
InnoAgent/
├── README.md
├── plan.md
├── core/
│   ├── runtime/
│   │   ├── state.py              # AgentState、节点状态、Checkpoint 辅助
│   │   ├── graph.py              # 主 LangGraph 图构建与编译
│   │   ├── nodes.py              # Execute、Continue、Goal Check 等主图节点
│   │   └── config.py             # 模型、阈值、运行模式等配置
│   ├── session/
│   │   ├── context.py            # 最终上下文组装：Query + Session + Tools + Memory
│   │   ├── history.py            # 对话历史管理
│   │   └── compression.py        # token 压缩、截断、摘要策略
│   ├── memory/
│   │   ├── profile.py            # 用户画像数据结构
│   │   ├── recall.py             # 记忆召回
│   │   ├── update.py             # 画像更新任务
│   │   └── thresholds.py         # 轮次、token 阈值与触发策略
│   ├── planning/
│   │   ├── graph.py              # Planning 子图
│   │   ├── planner.py            # 计划生成、修改、校验逻辑
│   │   ├── tasks.py              # Task 状态维护
│   │   └── schemas.py            # Plan/Task 数据模型
│   ├── reflection/
│   │   ├── graph.py              # Reflection 子图
│   │   ├── evaluator.py          # 目标完成判断
│   │   ├── feedback.py           # feedback 生成
│   │   └── schemas.py            # Reflection 数据模型
│   ├── tool/
│   │   ├── base.py               # BaseTool、ToolResult
│   │   ├── registry.py           # 工具注册表与动态描述
│   │   ├── decorators.py         # guardrail、requires_read、confirmation 装饰器
│   │   ├── search.py             # Tool Search，工具较多时按需检索
│   │   ├── plan_tool.py          # Plan
│   │   ├── task_tool.py          # Task
│   │   ├── write_tool.py         # Write
│   │   ├── read_tool.py          # Read
│   │   ├── ls_tool.py            # Ls
│   │   └── grep_tool.py          # Grep
│   └── guardrails/
│       ├── base.py               # Guardrail 接口
│       ├── file_guard.py         # 路径、确认、写前读
│       ├── plan_guard.py         # 计划完整性校验
│       └── policy.py             # 运行模式策略
├── observe/
│   ├── traces.py                 # 节点、工具、LLM 调用追踪
│   └── metrics.py                # 指标统计
├── view/
│   ├── cli.py                    # 面向用户的交互式 CLI 入口
│   ├── commands.py               # slash 命令解析与处理
│   ├── render.py                 # 过程展示、计划/任务/工具结果渲染
│   └── resume.py                 # session 恢复与 resume 逻辑
├── assert/
│   └── doc/
│       └── agentloop.png
└── test/
    ├── test_tools.py
    ├── test_planning.py
    ├── test_reflection.py
    ├── test_memory.py
    └── test_main_loop.py
```

## 4. LangGraph 整体架构

### 4.1 主图

主图节点与 README 中状态机对应：

```text
                         START
                           │
                           ▼
                    ┌─────────────┐
                    │   Execute   │◄─────────────────────┐
                    └──────┬──────┘                      │
                           │                             │
              ┌────────────┼────────────┐                │
              │            │            │                │
              ▼            ▼            ▼                │
           Tool Use      Planning    Reflection           │
              │            │            │                │
              └────────────┴────────────┘                │
                           │                             │
                           ▼                             │
                    ┌─────────────┐                      │
                    │   Continue  │──────────────────────┘
                    │   Execute    │
                    └──────┬──────┘
                           │
                           ▼
                      Goal Met?
                       /     \
                     Yes      No
                     /         \
                    ▼           └──────────────► Execute
                   END
```

LangGraph 图结构建议：

```text
START
  -> Execute
  -> RouteExecution
      - ToolCall      -> ToolUse
      - PlanCall      -> PlanningSubgraph
      - Finished      -> ReflectionSubgraph (仅 goal 存在)
      - Finished      -> GoalCheck (无 goal)
  -> ToolUse / PlanningSubgraph / ReflectionSubgraph
  -> ContinueExecute
  -> Execute
  -> GoalCheck
      - Complete      -> END
      - Incomplete    -> Execute
```

### 4.2 节点职责

| 节点 | 职责 | 关键输出 |
| --- | --- | --- |
| `Execute` | 组装上下文，调用模型，产出下一步意图 | `next_action`、`response`、`tool_calls`、`finished` |
| `RouteExecution` | 判断调用工具、进入 Planning、进入 Reflection 或结束 | 路由标签 |
| `ToolUse` | 查找工具、执行 Guardrail、运行工具、写入结果 | `tool_results` |
| `PlanningSubgraph` | 生成或修改 Plan，创建 Task，维护完成状态 | `plan`、`tasks`、`tool_description_updates` |
| `ReflectionSubgraph` | 判断 goal 是否完成，生成 feedback | `reflection`、`goal_complete` |
| `ContinueExecute` | 汇总本轮结果，更新 session，决定继续或结束 | `updated_context` |
| `GoalCheck` | 无 goal 时直接结束；有 goal 时检查是否完成 | `goal_complete` |

## 5. 核心状态设计

`core/runtime/state.py` 定义 LangGraph 使用的 `AgentState`。建议采用 `TypedDict` 或 `Pydantic` 模型，并通过 reducer 处理可追加字段。

### 5.1 建议字段

```python
class AgentState(TypedDict):
    # 会话与输入
    session_id: str
    user_input: str
    context: str
    history: list[Message]

    # 执行
    next_action: Literal["tool_use", "planning", "reflection", "finish"]
    finished: bool
    response: str
    tool_calls: list[ToolCall]
    tool_results: list[ToolResult]

    # Goal 与 Reflection
    goal: str | None
    goal_complete: bool
    reflection: ReflectionResult | None

    # Planning
    plan: Plan | None
    tasks: list[Task]
    plan_mode: bool

    # Memory
    memory_profile: UserProfile
    memory_updated: bool

    # 控制
    iteration: int
    max_iterations: int
    errors: list[AgentError]
```

### 5.2 状态更新规则

- `tool_results`、`errors`、`history` 使用可追加 reducer，避免覆盖历史。
- `plan`、`tasks` 使用“合并式 reducer”：新任务按 `task_id` 更新，不重复创建。
- `iteration` 每次回到 `Execute` 前递增，超过 `max_iterations` 时进入安全结束并给出未完成说明。
- `goal_complete` 只在 Reflection 或 GoalCheck 中写入，避免中间执行错误误判完成。

## 6. 主循环实现

### 6.1 Execute 节点

`Execute` 每次执行时：

1. 从 Session 取最近历史。
2. 从 Memory 召回用户画像和偏好。
3. 组装当前 `plan`、`tasks`、`tool_results`、`reflection`。
4. 调用 LLM，要求输出结构化的下一步意图。
5. 根据输出设置 `next_action`：
   - 需要调用工具：`tool_use`
   - 需要创建或修改计划：`planning`
   - 模型认为已经完成且存在 goal：`reflection`
   - 无 goal 且模型完成：`finish`

LLM 输出建议使用 JSON 或 tool-call 格式，至少包含：

```json
{
  "action": "tool_use | planning | finish",
  "tool_calls": [],
  "message": "本轮自然语言说明",
  "needs_user_confirmation": false
}
```

### 6.2 RouteExecution 节点

路由逻辑：

```text
next_action == tool_use        -> ToolUse
next_action == planning        -> PlanningSubgraph
next_action == finish and goal -> ReflectionSubgraph
next_action == finish          -> GoalCheck
```

如果模型输出了非法 `next_action`，进入错误节点，记录错误后返回 `Execute`，并注入“请返回合法下一步动作”的反馈。

### 6.3 ContinueExecute 节点

`ContinueExecute` 负责：

- 将工具结果、计划变更或反射反馈格式化成模型可读的消息。
- 更新 session 历史。
- 检查是否触发 Memory 异步更新。
- 递增 `iteration`。
- 若超过最大迭代数，生成终止消息并设置结束标记。

## 7. Planning 模块

Planning 是独立的 LangGraph 子图，位于 `core/planning`。它不替代主 Agent，而是被主 Agent 通过 `Plan` 工具或 `PlanningSubgraph` 调用。

### 7.1 进入方式

进入 Planning 的入口有：

1. 模型直接输出 `next_action == planning`。
2. 模型调用 `Plan` 工具。
3. Reflection 判定目标未完成，且模型选择调用 `Plan` 工具修改计划。

进入后设置 `plan_mode = true`，规划完成后回到 `ContinueExecute`。

### 7.2 Planning 子图

```text
PlanningStart
  -> LoadCurrentState
  -> CheckPlanExists
      - 无计划  -> CreatePlan
      - 有计划  -> UpdatePlan
  -> GenerateTasks
  -> ValidatePlan
  -> PublishPlanAndTasks
  -> PlanningEnd
```

节点说明：

| 节点 | 说明 |
| --- | --- |
| `LoadCurrentState` | 读取 goal、已有 Plan、Task、执行反馈和上下文 |
| `CheckPlanExists` | 判断是新建还是修改 |
| `CreatePlan` | 根据 goal 生成分阶段计划 |
| `UpdatePlan` | 根据 reflection feedback、工具结果或用户输入调整计划 |
| `GenerateTasks` | 将计划拆解为可跟踪 Task |
| `ValidatePlan` | 校验计划是否完整、可执行、无循环依赖 |
| `PublishPlanAndTasks` | 写回 state，并更新 Plan/Task 工具描述 |

### 7.3 Plan 数据模型

```python
class Plan(BaseModel):
    plan_id: str
    goal: str
    status: Literal["draft", "active", "revised", "completed", "abandoned"]
    steps: list[PlanStep]
    rationale: str
    revision: int
    created_at: datetime
    updated_at: datetime

class PlanStep(BaseModel):
    step_id: str
    title: str
    description: str
    depends_on: list[str]
    status: Literal["pending", "in_progress", "done", "blocked", "skipped"]
    result: str | None

class Task(BaseModel):
    task_id: str
    plan_id: str
    title: str
    status: Literal["pending", "in_progress", "done", "blocked"]
    assignee: str
    result: str | None
```

### 7.4 Plan 与 Task 的工具描述维护

这是关键设计：计划与任务状态不仅保存在 state，还要体现在工具描述中，让主 Agent 在后续调用时能看到进度。

实现方式：

- `PlanTool` 的 `description` 动态包含当前 plan 摘要、revision、active step 数量。
- `TaskTool` 的 `description` 动态包含 `pending`、`in_progress`、`done` 的任务数量及最近任务标题。
- `registry.get_tool_schema()` 在每次组装上下文时读取最新 plan/tasks，生成带状态的工具描述。
- 如果工具描述过长，只暴露统计信息和最近变更，完整计划通过 `Read` 或状态查看工具读取。

例如：

```text
Task Tool:
Create or complete a task in the active plan.
Current plan: build a CLI agent skeleton (rev 2)
Tasks: 3 pending, 1 in_progress, 2 done
Latest pending: implement reflection subgraph
```

### 7.5 计划校验规则

- 每个计划必须有明确 goal。
- step 的 `depends_on` 必须引用存在的 step，且不能形成循环。
- 一个 plan 至少有 1 个可执行 step。
- 修改计划时 `revision + 1`，保留历史修订记录。
- 当所有 task 完成时，plan status 标记为 `completed`。

## 8. Reflection 模块

Reflection 是独立子图，位于 `core/reflection`。它在用户设置了 goal 且模型不再调用 tool 而准备 finish 时触发。

### 8.1 触发条件

同时满足：

1. `state.goal` 非空。
2. `next_action == finish`。
3. 本轮没有未处理的 `tool_calls`。

如果无 goal，直接进入 `GoalCheck` 结束，不进入 Reflection。

### 8.2 Reflection 子图

```text
ReflectionStart
  -> EvaluateGoal
  -> CheckComplete
      - Complete     -> MarkComplete
      - Incomplete   -> GenerateFeedback
  -> ReflectionEnd
```

节点说明：

| 节点 | 说明 |
| --- | --- |
| `EvaluateGoal` | 基于最终回复、plan、tasks、tool_results、历史判断目标是否完成 |
| `CheckComplete` | 根据评估结果分流 |
| `MarkComplete` | 设置 `goal_complete = true`，汇总完成结果 |
| `GenerateFeedback` | 生成 feedback，指出缺口、下一步建议和是否应调整 plan |

### 8.3 完成判断

完成判断至少包含：

- plan 是否全部 `done`。
- 用户 goal 中的约束是否全部满足。
- 是否存在未解决的工具错误。
- 模型最终回复是否直接回答了 goal。

判断结果建议为：

```json
{
  "complete": false,
  "confidence": 0.62,
  "missing_conditions": ["文件尚未创建", "测试未运行"],
  "evidence": ["Write 工具成功返回", "Task-3 仍为 pending"]
}
```

### 8.4 Feedback 生成

未完成时，feedback 应包含：

- 当前离 goal 还有哪些差距。
- 已完成的内容和可复用结果。
- 明确建议下一步：继续执行、调用具体工具、调整计划或向用户提问。
- 若计划已不适配，提示“建议调用 Plan 工具更新计划”。

feedback 作为新的用户消息或系统消息注入 `Execute`，让主 Agent 基于反馈继续工作。

### 8.5 防死循环

- 每次 Reflection 递增 `reflection_count`。
- 若连续 N 次 reflection 未完成且没有新的有效工具结果，则停止自动循环，输出最终未完成报告，并请求用户明确下一步。
- 若连续 M 次 reflection 都建议调整计划，则降低自动调整权重，避免反复重写计划而不执行。

## 9. Memory 模块

Memory 位于 `core/memory`，第一阶段只维护“简单用户画像”，不引入复杂记忆体系。

### 9.1 用户画像

```python
class UserProfile(BaseModel):
    user_id: str
    preferences: dict[str, str]
    facts: list[UserFact]
    recent_topics: list[str]
    working_style: str | None
    last_updated_at: datetime
```

画像内容至少包括：

- 用户偏好：语言、格式、简洁程度、是否需要解释。
- 长期事实：项目名、常用技术栈、目标目录、命名习惯。
- 近期话题：用于上下文召回和会话连贯性。
- 工作风格：偏好直接执行、谨慎执行或逐步确认。

### 9.2 召回

每次 `Execute` 前：

1. 使用 user_id 获取画像。
2. 抽取与当前 query 相关的 facts/preferences。
3. 注入到最终 context 中。
4. 控制召回内容长度，避免挤占主任务 token。

### 9.3 触发异步更新

触发条件：

```text
对话轮次 >= turn_threshold
或
用户累计输入 token 数 >= input_token_threshold
```

触发后执行：

1. 启动异步任务 `MemoryUpdateTask`。
2. 从当前 session 提取本轮及最近历史。
3. 调用模型生成画像更新候选：新增事实、修正偏好、更新近期话题。
4. 将候选写回 `UserProfile`。
5. 更新 `last_updated_at` 和累计计数。

异步任务不应阻塞主循环。可以使用：

- `asyncio.create_task`。
- 独立后台队列 + worker。
- LangGraph node 结束后通过 `after_node` 或外部 scheduler 提交任务。

第一阶段建议用 `asyncio.create_task` 加持久化落盘，保证简单和可追踪；后续可替换为队列。

### 9.4 计数重置

异步更新完成后：

- 重置对话轮次计数。
- 重置累计用户输入 token 数。
- 保留画像版本号，便于审计。

## 10. Tool Use 模块

Tool Use 位于 `core/tool`，提供基础工具类型、注册表、Tool Search 和 Guardrail 接入。

### 10.1 BaseTool

```python
class BaseTool:
    name: str
    description: str
    input_schema: type[BaseModel]
    requires_confirmation: bool
    mode: ToolMode

    async def execute(self, tool_input: ToolInput, context: ToolContext) -> ToolResult:
        ...
```

工具执行统一返回：

```python
class ToolResult(BaseModel):
    tool_name: str
    status: Literal["success", "error", "needs_confirmation", "blocked"]
    output: str
    data: Any
    warnings: list[str]
    metadata: dict[str, Any]
```

### 10.2 装饰器接入 Guardrail

装饰器分三层：

```text
@tool
@guardrail
@requires_read
def write_file(...):
    ...
```

建议职责：

| 装饰器 | 职责 |
| --- | --- |
| `@tool` | 注册到 registry，读取 schema、名称、描述 |
| `@guardrail` | 执行统一 guardrail 链，处理确认和阻断 |
| `@requires_read` | 写工具执行前强制读取目标文件或目录 |

Guardrail 链在工具真正执行前运行，并支持短路返回 `needs_confirmation` 或 `blocked`。

### 10.3 写工具强制写前读

对 `Write` 和任何声明为写操作的扩展工具：

1. 解析目标路径。
2. 如果目标存在，先调用 `Read` 读取当前内容。
3. 如果目标不存在，读取父目录内容以确认写入位置合理。
4. 将读取结果放入 `ToolResult.metadata["pre_read"]`。
5. 只有读取成功或明确确认后才执行写操作。

写前读的意义：

- 防止覆盖用户未察觉的内容。
- 为后续 diff、回滚、审计提供基础。
- 让 Guardrail 能基于原内容判断风险。

### 10.4 文件操作确认策略

根据运行模式决定是否要求用户确认：

| 模式 | Write 行为 | 其他文件操作 |
| --- | --- | --- |
| `auto` | 直接写入，但记录写前读和变更摘要 | 直接执行 |
| `confirm` | 写入前要求用户确认 | 敏感或批量操作前确认 |
| `readonly` | 禁止写，返回 blocked | 只读操作允许 |

确认策略由 `core/guardrails/policy.py` 统一判断，不散落在工具中。

### 10.5 Tool Search

当工具总量较多时，不向模型一次性披露所有工具。主循环只暴露 `tool_search`，让模型先检索相关工具，再获取具体 schema。

第一阶段工具较少，可以直接披露 6 个基础工具，但保留 Tool Search 接口：

```python
def search_tools(query: str, current_state: AgentState) -> list[ToolDescriptor]:
    ...
```

## 11. 基础工具定义

### 11.1 Plan

- 名称：`plan`
- 用途：创建或修改计划。
- 输入：goal、可选的 plan_id、期望修改内容。
- 输出：plan 摘要、tasks 摘要、当前进度。
- 特殊行为：调用后进入 Planning 子图，并触发动态工具描述更新。

### 11.2 Task

- 名称：`task`
- 用途：创建任务或将任务标记为进行中、完成、阻塞。
- 输入：`task_id`、`status`、`result` 或新建任务字段。
- 输出：更新后的 task 状态和计划进度。
- 特殊行为：Task 完成状态会反映到工具描述，供后续轮次读取。

### 11.3 Write

- 名称：`write`
- 用途：创建或覆盖文件。
- 输入：`path`、`content`、可选 `mode`。
- Guardrail：写前读；根据运行模式判断是否需要确认。
- 输出：是否写入成功、写入前后摘要、是否需要用户确认。

### 11.4 Read

- 名称：`read`
- 用途：读取文件内容。
- 输入：`path`、可选 `start_line`、`end_line`。
- 输出：文件内容或指定行范围，附带文件大小、行数等元数据。

### 11.5 Ls

- 名称：`ls`
- 用途：列出目录内容。
- 输入：`path`、可选 `recursive`、`depth`、过滤规则。
- 输出：文件、目录、权限、大小等列表。

### 11.6 Grep

- 名称：`grep`
- 用途：搜索文件内容。
- 输入：`pattern`、`path`、可选 `glob`、`ignore_case`。
- 输出：匹配文件路径、行号和匹配片段。
- 说明：内部优先使用 `rg`，不可用时回退到 `grep`。

## 12. Guardrail 模块

Guardrail 位于 `core/guardrails`，是工具执行前、执行后的统一策略层。

### 12.1 接口

```python
class Guardrail:
    name: str

    async def before(self, tool: BaseTool, tool_input: ToolInput, context: ToolContext) -> GuardrailDecision:
        ...

    async def after(self, tool: BaseTool, result: ToolResult, context: ToolContext) -> ToolResult:
        ...
```

### 12.2 Guardrail 链

执行顺序：

1. `PlanGuard`：计划修改是否合法。
2. `PathGuard`：路径是否在工作区内，避免越权访问。
3. `WriteBeforeReadGuard`：写工具是否已完成写前读。
4. `ConfirmationGuard`：是否需要用户确认。
5. `ResultGuard`：结果是否包含敏感信息、错误是否可恢复。

任一 Guardrail 返回 `blocked` 时，工具不执行，结果以 `blocked` 返回并记录原因。

### 12.3 与工具的边界

- 工具只关心业务逻辑。
- Guardrail 关心安全、确认、写前读等横切策略。
- 工具不应自行调用用户确认；确认统一由 Guardrail 触发并写入 `ToolResult.status == needs_confirmation`。

## 13. Session、Runtime、Observe、View

### 13.1 Session

`core/session` 负责组装最终上下文：

```text
User Input
  + Memory Recall
  + Session History
  + Tool Descriptions
  + Plan/Task State
  + Reflection Feedback
  => Final Context
```

需要控制：

- 最大上下文 token。
- 历史摘要和截断策略。
- 工具 schema 只披露必要部分。
- 每个 session 的 checkpoint 恢复。

### 13.2 Runtime

`core/runtime` 负责：

- 定义 `AgentState`。
- 构建和编译 LangGraph 主图。
- 注入模型调用器。
- 管理 checkpoint 和错误恢复。

### 13.3 Observe

`observe` 负责记录：

- 每个 LangGraph 节点输入/输出摘要。
- LLM 调用参数、耗时、token 使用。
- 工具调用、Guardrail 决策、写前读结果。
- Planning/Reflection 的中间结果。

### 13.4 View：面向用户的 CLI

`view` 是提供给最终用户使用的交互式命令行界面，不是内部调试面板。它负责把 Agent 的执行过程、计划状态、任务进度和工具调用结果用可读的方式呈现给用户，并支持交互式命令和会话恢复。

#### 13.4.1 CLI 交互模型

CLI 采用 REPL 模式：

```text
InnoAgent> 请创建一个 app.py
```

用户可以：

- 直接输入自然语言任务。
- 输入 slash 命令切换模式或查看状态。
- 在 Agent 运行期间看到节点、工具、Guardrail 和 Reflection 的过程展示。
- 使用 `resume` 恢复之前的会话继续执行。

#### 13.4.2 slash 命令

第一阶段提供以下命令：

| 命令 | 作用 |
| --- | --- |
| `/help` | 显示可用命令 |
| `/goal <text>` | 设置或修改当前 goal |
| `/plan` | 查看当前 Plan 和 Task 进度 |
| `/tools` | 查看当前可用工具及动态描述 |
| `/status` | 查看当前 Agent 状态摘要 |
| `/mode auto` | 切换为自动执行模式 |
| `/mode confirm` | 切换为写操作需确认模式 |
| `/mode readonly` | 切换为只读模式 |
| `/resume [session_id]` | 恢复最近或指定 session |
| `/sessions` | 列出可恢复的 session |
| `/stop` | 停止当前任务 |
| `/clear` | 清空当前对话历史 |
| `/quit` | 退出 CLI |

命令处理位于 `view/commands.py`。slash 命令不应直接修改 Agent 内部状态，而是通过统一的 `CLIContext` 或 `AgentRuntime` API 调用 runtime 和 session。

#### 13.4.3 过程展示

CLI 需要将内部执行轨迹转成用户友好的过程展示，而不是打印原始 state。`view/render.py` 负责：

- **执行阶段**：显示当前处于 `Execute`、`ToolUse`、`Planning` 或 `Reflection`。
- **模型动作**：显示模型准备调用哪个工具，或准备结束。
- **工具调用**：显示工具名、参数摘要、执行状态和结果摘要。
- **Guardrail 决策**：显示是否通过、是否需要确认、是否被阻断及原因。
- **计划变化**：显示新建/修改的 plan revision、新增 task、任务状态变化。
- **Reflection**：显示目标是否完成，或展示 feedback 摘要。

示例过程展示：

```text
[execute] 正在理解目标...
[planning] 已创建计划 rev1，共 2 个 step
[task] task-1 Write app.py -> pending
[tool] write(path="app.py") -> success
[guardrail] write-before-read: parent_dir=./ (ok)
[task] task-1 -> done
[reflect] goal 未完成：还需验证文件内容
[tool] read(path="app.py") -> success
[reflect] goal 已完成
```

#### 13.4.4 resume 与 session 恢复

`view/resume.py` 支持：

- 通过 LangGraph checkpoint 恢复已存在的 session。
- 通过 `/resume [session_id]` 继续最近或指定会话。
- 恢复后展示简短的历史摘要和当前 Plan/Task 状态。
- 如果 session 不存在，给出可恢复 session 列表。

恢复流程：

```text
/resume [session_id]
  -> 查询可用 session
  -> 加载 session 和 checkpoint
  -> 展示历史摘要
  -> 展示当前 goal、plan、task、reflection 状态
  -> 进入 REPL，继续输入
```

#### 13.4.5 显示原则

- 默认不打印完整 state、完整 prompt 或完整 LLM 响应，只显示用户需要的关键结果。
- 长文件内容只显示摘要或指定范围。
- 计划、任务、工具调用等结构优先用紧凑文本展示。
- 所有需要用户确认的操作必须清晰展示原因，不使用隐式确认。
- 错误和阻断信息需要说明后续可执行的动作。

## 14. 完整运行流程示例

场景：用户设置 goal“在项目根目录创建 `app.py`，打印 Hello, InnoAgent”。

1. **User Input 进入**
   - Memory 召回用户偏好，例如“文件写入后要简洁说明”。
   - Session 组装上下文。

2. **Execute**
   - 模型识别目标涉及计划，调用 `Plan` 工具。

3. **Planning 子图**
   - 创建 Plan：
     - step 1：创建 app.py
     - step 2：验证文件内容
   - 创建 Task：
     - task-1：Write app.py
     - task-2：Read app.py 验证
   - 更新 Plan/Task 工具描述，显示 2 pending。

4. **ToolUse**
   - 模型调用 `Write`。
   - Guardrail 执行写前读：目标不存在，读取父目录。
   - 根据运行模式写入文件。

5. **ToolUse**
   - 模型调用 `Task`，标记 task-1 完成。

6. **ToolUse**
   - 模型调用 `Read`，读取 app.py，确认内容正确。
   - 调用 `Task`，标记 task-2 完成。

7. **Execute -> finish**
   - 因存在 goal，进入 Reflection。

8. **Reflection 子图**
   - 判断 plan 全部完成、文件存在、内容正确。
   - 设置 `goal_complete = true`。

9. **END**
   - 返回最终回复：文件已创建，内容已验证。

## 15. 错误处理与恢复

### 15.1 工具错误

- 工具失败返回 `ToolResult(status="error")`。
- 错误内容写入 state.errors，并作为反馈交给 `Execute`。
- 连续同一工具错误 3 次，停止自动重试并返回用户，给出原因。

### 15.2 模型输出错误

- `next_action` 非法或 JSON 无法解析时，返回 `Execute` 并注入纠正提示。
- 连续解析失败时，使用更严格的 system prompt 或降低输出要求。

### 15.3 Guardrail 阻断

- 记录阻断原因，不执行工具。
- 若为 `needs_confirmation`，向用户请求确认，不自动继续。
- 若为 `blocked`，返回主循环，由模型选择替代方案或结束。

### 15.4 超时与循环

- 设置 `max_iterations`，默认建议 20。
- 达到上限后生成未完成摘要，避免无限循环。
- 每次 Reflection 递增计数，并设置独立上限。

## 16. 实施阶段

### 阶段 0：骨架与状态

- 完成 `core/runtime/state.py`。
- 建立 Pydantic 数据模型：Message、ToolCall、ToolResult、Plan、Task、UserProfile。
- 建立主图空节点，确认 LangGraph 图可编译。

验收：

- 空输入可运行到 END。
- `AgentState` 字段完整。

### 阶段 1：基础工具与 Guardrail

- 实现 `Read`、`Ls`、`Grep`、`Write`。
- 实现 BaseTool、registry、decorator。
- 实现写前读和确认策略。
- 编写工具单测。

验收：

- 6 个基础工具均可独立执行。
- Write 不经过写前读会被拦截。
- `confirm` 模式下写文件要求确认。

### 阶段 2：主执行循环

- 实现 Execute、RouteExecution、ToolUse、ContinueExecute、GoalCheck。
- 接入 LLM，支持自然语言任务完成简单文件操作。
- 加入最大迭代和错误处理。

验收：

- 无 goal 时执行工具后正常结束。
- 工具结果能正确回传给模型。

### 阶段 3：Planning

- 实现 Planning 子图。
- 实现 Plan、Task 工具。
- 实现计划校验和动态工具描述。

验收：

- 设置 goal 后能创建计划。
- 任务完成状态能反映到工具描述。
- 计划依赖循环能被拦截。

### 阶段 4：Reflection

- 实现 Reflection 子图。
- 实现目标完成判断和 feedback 生成。
- 接入主循环防死循环策略。

验收：

- 目标完成后正确结束。
- 目标未完成时生成有效 feedback 并继续执行。
- 连续失败不会无限循环。

### 阶段 5：Memory

- 实现 UserProfile 和召回。
- 实现异步画像更新任务。
- 实现轮次和 token 阈值触发。

验收：

- 首次执行能召回默认画像。
- 超过阈值后异步更新画像。
- 主循环不被记忆更新阻塞。

### 阶段 6：Session、Observe、CLI 与测试

- 完成上下文组装和 token 控制。
- 接入 trace 和 metrics。
- 实现交互式 CLI、slash 命令、过程展示和 resume。
- 补齐端到端测试。

验收：

- 完整场景通过测试。
- 轨迹可回溯 Guardrail 决策和工具执行结果。
- 用户可通过 CLI 完成自然语言任务、切换模式和恢复会话。

## 17. 测试计划

### 17.1 单元测试

- `test_tools.py`：各工具输入输出、错误路径、写前读、确认策略。
- `test_planning.py`：新建、修改、任务状态、依赖校验。
- `test_reflection.py`：完成判断、feedback 生成、防循环。
- `test_memory.py`：阈值触发、画像更新、计数重置。

### 17.2 图测试

- 使用 LangGraph 的图遍历或小规模模型桩测试路由。
- 验证 `ToolUse -> ContinueExecute -> Execute` 循环。
- 验证 `PlanningSubgraph` 和 `ReflectionSubgraph` 挂载正确。

### 17.3 端到端测试

场景至少包括：

1. 无 goal：读取文件并回答。
2. 无 goal：写入文件后正常结束。
3. 有 goal：创建计划、执行工具、Reflection 判定完成。
4. 有 goal：任务未完成，Reflection 返回 feedback，主循环继续。
5. 危险写操作：confirm 模式要求用户确认。
6. 连续失败：达到最大迭代后安全停止。

## 18. 关键决策与风险

### 18.1 关键决策

| 决策点 | 选择 | 原因 |
| --- | --- | --- |
| 主图组织方式 | 单主图 + Planning/Reflection 子图 | 符合 README，主 Agent 始终执行 |
| Plan/Task 状态 | 同步写入 state，动态更新工具描述 | 让模型能直接感知进度，不依赖额外查询 |
| Memory 更新 | 阈值触发 + 异步任务 | 减少主循环延迟，避免每轮更新 |
| Guardrail | 装饰器统一接入 | 工具实现简洁，策略集中管理 |
| 文件确认 | 按运行模式配置 | 兼顾自动执行和安全确认 |

### 18.2 主要风险

- **上下文膨胀**：计划、工具结果、历史可能快速增长。通过摘要、动态工具描述和 token 预算缓解。
- **Reflection 判断不准确**：使用证据和结构化输出，设置 confidence 阈值。
- **无限循环**：用 iteration、reflection_count、连续错误计数限制。
- **异步 Memory 与 checkpoint 一致性问题**：画像更新采用版本号，失败可重试，避免主状态强依赖异步结果。
- **工具描述动态变化导致模型行为不稳定**：保持 schema 稳定，只更新 description 中的状态摘要。

## 19. Definition of Done

InnoAgent 第一阶段完成的判定标准：

1. LangGraph 主图能完整运行 README 中的状态机。
2. 6 个基础工具均可通过主 Agent 自然语言调用。
3. Plan 工具能创建/修改计划，Task 状态能在工具描述中反映。
4. 写工具执行前必定发生读操作，且确认策略生效。
5. 存在 goal 时，模型准备结束前进入 Reflection。
6. Reflection 能在目标完成时结束，在未完成时返回可执行 feedback。
7. Memory 能在轮次或输入 token 阈值触发异步画像更新。
8. 主循环具备最大迭代和防死循环能力。
9. 核心模块有单元测试，关键路径有端到端测试。
10. 执行过程可通过 observe 追踪，并通过 CLI 向用户展示。
11. CLI 支持 slash 命令、过程展示和 `resume` 会话恢复。
