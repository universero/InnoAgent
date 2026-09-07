# 事件与 TUI

## 模块定位

Runtime、Session 和界面通过稳定事件解耦。Runtime 不调用 prompt_toolkit 控件，TUI 也不读取 Runtime 私有字段推断过程。

主要模块：

| 文件 | 职责 |
|---|---|
| `core/event/events.py` | `AgentEvent` 统一事件模型 |
| `core/agent/model_stream.py` | Provider 事件归一化为模型批次 |
| `view/cli.py` | 输入路由、Slash 命令、后台执行和审批 |
| `view/commands.py` | Slash 命令解析 |
| `view/terminal.py` | 内联 PromptSession、异步输入调度、线程安全输出和输入状态 |
| `view/tui_render.py` | 事件到活动、流式片段和语义块的纯转换 |
| `view/tui_theme.py` | 输入区与输出区分离的语义样式 |
| `view/render.py` | 非 TTY 文本渲染与兼容事件渲染 |
| `view/resume.py` | Session 列表与恢复后的简洁状态摘要 |

## 事件模型

`AgentEvent` 的公共字段包括：

- 身份：`event_id`、`session_id`、`turn_id`。
- 时间：UTC ISO `timestamp`。
- 阶段：`main`、`plan`、`reflect`、`compact`、`subagent`。
- Item：`item_id`、`item_type`、`call_id`。
- 内容：`delta`、`payload`、`content`。
- 工具：`tool_name`、`arguments`、`result`。
- 资源：`usage`、`progress`。
- 控制：`finish_reason`、`options`。

兼容字段保留旧 adapter 和历史 session 的读取能力。新代码优先使用 `type + stage + item_type + payload`。

## 生命周期事件

```text
turn.started
item.started
item.delta
item.completed
response.completed / response.failed
approval.requested / approval.resolved
steering.queued / steering.applied
steering.stop_requested / steering.stopped
context.compaction.started / context.compaction.completed
turn.completed / turn.failed
```

采用 turn/item/call 结构的原因是它能同时表达文本、推理、工具和阶段结果，而不需要为每个 Provider 发明一套事件。TUI 和 Session 面向内部协议，不直接依赖 OpenAI SSE 字段。

## 流式归一化

```mermaid
flowchart LR
    Provider[Responses SSE] --> Adapter[OpenAICompatibleModel]
    Adapter --> Events[AgentEvent stream]
    Events --> Consumer[ModelStreamConsumer]
    Consumer --> Batch[ModelBatch]
    Events --> TUI[TUI live display]
    Batch --> Runtime[LangGraph routing]
```

`ModelStreamConsumer` 同时完成两件事：

- 将每个规范化事件立即 emit 给 UI。
- 聚合出 Runtime 可确定消费的 `ModelBatch`。

聚合规则：

- message delta 按顺序拼接。
- reasoning delta 只用于展示，不混入最终回答。
- tool call 按 `call_id` 聚合名称和分片参数。
- completed tool call 可补齐最终 arguments。
- `response.completed` 汇总 usage。
- `response.failed` 转成 batch error。

Runtime 因此不用理解 SSE，也不依赖 Provider 是否将参数拆成多个 chunk。

## Delta 与 Completed 去重

Provider 可能同时发送文本 delta 和最终 completed message。Adapter/Consumer 优先使用已拼接 delta；completed 内容只在没有 delta 时补齐。事件 payload 标记 streamed 内容，Renderer 避免再次输出整段文本。

这是 UI 正确性的关键不变量：实时显示和最终持久化都需要，但不能在 transcript 中重复两次。

## CLI 输入路由

`InnoAgentCLI` 支持两种运行方式：

- TTY：启动保留原生 scrollback 的内联 `TerminalIO`。
- 非 TTY、测试或脚本：使用普通 input/output 循环。

TUI 输入由 `_dispatch_tui_input()` 按状态路由：

1. Runtime busy：普通文本和 `/steer` 进入 steering；`/stop` 请求安全停止。
2. Pending approval：输入解析为 allow once、always 或 deny。
3. 空闲 Slash 命令：进入控制命令处理。
4. 空闲普通文本：启动新 turn。

运行中不允许执行任意 Slash 配置命令，因为它们可能与正在使用的模型、Session 或状态竞争。只有 steering 和 stop 属于 live input。

## Slash 命令职责

| 命令 | 控制对象 |
|---|---|
| `/goal` | 设置或清除当前 Goal |
| `/plan`、`/tasks` | 查看当前计划和任务 |
| `/tools` | 查看动态工具描述 |
| `/skills`、`/skill` | 发现和激活 Skill |
| `/status`、`/context` | 查看运行状态与上下文预算 |
| `/permissions` | 查看仓库持久规则 |
| `/compact` | 手动压缩当前 Session |
| `/approve` | 非 TUI 场景解决 pending approval |
| `/steer`、`/stop` | 运行中纠偏与停止 |
| `/mode` | 切换 ask、auto、readonly |
| `/model` | 更新模型和 reasoning effort |
| `/resume`、`/sessions`、`/rename` | Session 管理 |
| `/new`、`/clear`、`/quit` | 生命周期与界面控制 |

Slash 命令是控制输入，不追加为普通用户消息。否则模型会把“切换模式”误解为业务任务，Session 重放也无法区分控制行为。

`view/resume.py` 将恢复选择与 CLI 主类分开：`pick_session()` 优先按 session id 加载，失败后再按名称匹配；未指定目标时选择最近会话。`resume_summary()` 只展示名称、Goal 和 Plan 状态，不复制历史正文。这样恢复入口保持确定性，也避免会话列表和启动提示意外泄露大量上下文。

## TUI 结构

```mermaid
flowchart TB
    Events[AgentEvent] --> Scrollback[原生终端 scrollback]
    Input[PromptSession 输入框] --> Dispatch[异步输入分发]
    Snapshot[稳定状态快照] --> Toolbar[底部状态栏]
    Approval[审批状态] --> InputMode[approve / steer / normal]
```

`TerminalIO` 使用一个长生命周期 `PromptSession`，但每次提交后立即开始下一次 `prompt_async()`。它不使用 `full_screen=True`，不进入 alternate screen，也不维护可聚焦的只读 transcript 控件。

这样设计直接解决三个问题：

- 输出进入终端原生 scrollback，用户可以按终端习惯选择、复制和搜索。
- `mouse_support=False`，prompt_toolkit 不会截获鼠标并把焦点切到输出面板。
- 输入提交后马上重新显示 Prompt；长任务在后台运行时，下一条输入可作为 steering。

### 滚动输出

用户消息、Agent 文本、reasoning、工具调用、工具结果、Plan、Reflection、压缩和 steering 被渲染为简洁语义块。工具输出通过 `compact_body()` 限制行数和字符数，防止单次命令淹没终端。

输出区不设置全局背景色，只给标题使用少量语义色。这样既避免固定黄黑主题，也尊重用户自己的终端主题。

### 输入与底栏

Prompt 使用浅色局部背景和三种输入语义：

- 空闲：`›`，接受任务或 Slash 命令。
- 执行中：`steer ›`，接受纠偏或 `/stop`。
- 等待审批：`approve ›`，接受 1、2、3 或文字选项。

底栏从稳定 state snapshot 读取并展示：

- model 和 reasoning effort。
- 当前工作区和权限模式。
- 累计 token 与 context 百分比。
- Goal 是否存在与 Task 完成数。
- Ready、Thinking、Planning、Reflecting 或 Approval required。

信息保持单行，窄终端由 prompt_toolkit 自然裁剪；详细 Goal、Plan 和 Task 通过 Slash 命令查看，避免重新引入常驻侧栏。

### 审批显示

Pending approval 会向 scrollback 写入一次工具名、参数摘要和三个固定选择。随后 Prompt 切换为审批模式。审批 UI 不自行执行工具，只把决策传回 Runtime；相同审批状态不会重复打印。

## 线程模型

模型和工具可能阻塞，不能运行在 prompt_toolkit 输入循环。CLI 通过 `asyncio.to_thread()` 执行同步 Runtime，`TerminalIO` 继续等待下一次输入。

Runtime 事件可能来自工作线程，因此终端层先写入 `SimpleQueue`，再用事件循环中的 `_flush_async()` 合并约 10ms 内的输出，并通过 `run_in_terminal()` 暂停、打印和恢复 Prompt。这样保证：

- 后台线程不直接操作 PromptSession。
- 相邻 delta 可以批量输出，减少重绘闪烁。
- 输入框在流式输出时仍可响应。
- 审批和 steering 状态切换集中完成。

## 语义化展示

`present_event()` 是纯函数，将事件转成 `EventPresentation`：

- `activity`：Header 当前活动。
- `stream`：追加到当前流式块。
- `block`：添加一个带 tone、title、body 的稳定块。

Theme 将 Prompt 与输出样式分开。Prompt 只使用局部中性浅色背景；scrollback 沿用终端背景，并以绿色表示成功、蓝色表示工具、红色表示错误。样式键表达语义而不是具体控件，使未来更换终端库时仍能复用展示模型。

## 非 TTY 回退

CI、管道和传入自定义 input/output adapter 的环境使用 `view/render.py` 输出纯文本。它消费同一事件协议，忽略 delta 或内部 checkpoint，并保留工具、审批、Plan、Reflection 和压缩摘要。

回退模式不是次要功能，它保证：

- 自动化测试不依赖真实终端。
- 日志可重定向到文件。
- 无鼠标和低能力终端仍可使用。

## 敏感信息与展示上限

- 工具参数只显示有界摘要。
- 大输出在 Tool Registry 和 TUI 两层裁剪。
- API Key 不进入事件。
- reasoning 只做临时显示，不写入 Session。
- Renderer 不应从 raw exception 打印环境凭据。

## 扩展约束

- 新用户可见行为先定义 AgentEvent，再实现 TUI 和文本 Renderer。
- `item.delta` 只用于实时显示，不承担恢复语义。
- 新 Slash 命令明确它是控制输入还是模型消息。
- 后台任务只能写队列，不能直接操作 PromptSession。
- 审批、stop 和 steering 必须由 Runtime 确认，UI 不能乐观假设已生效。
- 新增底栏字段只能读取稳定快照，避免依赖并发中的可变对象。

## 关键测试

- `test/test_streaming_model.py`：delta、tool call 分片和 usage 聚合。
- `test/test_cli.py`：Slash、TUI 输入模式、审批、steering 和事件渲染。
- `test/test_advanced_runtime.py`：运行中输入与安全停止。
- `test/test_session.py`：持久事件与重放。
