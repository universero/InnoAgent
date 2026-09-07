# 模型、提示词与配置

## 模块定位

模型层负责把 Runtime 的统一输入转换为 Provider 请求，再把 Provider 输出转换为 InnoAgent 事件。它不决定权限、文件边界或 Goal 是否完成。

主要模块：

| 文件 | 职责 |
|---|---|
| `core/llm/base.py` | 模型客户端抽象、`ModelDecision` 和测试兼容事件适配 |
| `core/llm/responses.py` | OpenAI-compatible Responses API 与 SSE 解析 |
| `core/agent/model_stream.py` | 将事件流聚合为 Runtime 使用的 `ModelBatch` |
| `core/prompts.py` | 系统、Planning、Reflection、Compaction、Subagent 提示词 |
| `core/runtime/model_config.py` | 模型凭据和端点配置加载 |
| `core/runtime/config.py` | 与模型调用相关的运行预算 |

## 模型抽象

`BaseModelClient` 的最小接口是 `respond()`，返回：

- `action`：tool_use、planning 或 finish。
- `message`：模型文本。
- `tool_calls`：名称和结构化参数列表。

基类还提供默认 `stream_events()`：调用 `respond()`，再将同步结果适配为 `AgentEvent`。这使 FakeModel 或旧客户端只实现决策接口也能接入新 Runtime。

生产客户端可直接覆盖 `stream_events()`，获得真实增量事件和 usage。

## 为什么 Runtime 不直接依赖 Provider SDK

Provider API 在事件名、工具参数分片、usage 和 reasoning 输出上差异较大。若 Runtime 直接处理 SSE：

- LangGraph 节点会与某家 API 强绑定。
- 测试需要模拟复杂网络流。
- 更换模型会影响 TUI 和 Session 事件。

因此边界分为两层：

```text
Provider SSE -> AgentEvent -> ModelStreamConsumer -> ModelBatch -> Runtime
```

Provider adapter 负责协议兼容，ModelStreamConsumer 负责运行时语义聚合。

## Responses API 请求

`OpenAICompatibleModel._build_payload()` 发送：

- `model`：当前模型名。
- `instructions`：统一 `SYSTEM_PROMPT`。
- `input`：ContextBuilder 生成的本轮上下文。
- `tools`：Registry 提供的 function schema。
- `reasoning.effort`：none、low、medium、high、xhigh 或 max。
- `stream=true`。

系统提示词与 Context 分开发送，保证系统约束不被会话内容拼接顺序削弱。工具描述来自实际 schema，避免 Prompt 中手写一套与代码漂移的参数说明。

## SSE 解析

`OpenAICompatibleModel.stream_events()` 逐行读取 `data:`：

- `response.reasoning_text.delta/done` -> reasoning item。
- `response.output_text.delta/done` -> message item。
- `response.output_item.added` -> tool call started。
- `response.function_call_arguments.delta/done` -> 工具参数分片与完成。
- `response.output_item.done` -> 对缺失的工具 completed 事件补偿。
- `response.completed` -> usage 与完成原因。
- `response.failed/incomplete` -> 统一失败事件。

工具名按 output index 暂存，参数完成后解析 JSON。非法 JSON 不直接丢弃，而是保存为 `_raw`，随后由严格工具 schema 返回可观察错误。

这种做法比 Adapter 静默修复参数更安全：Runtime 能看见模型真实输出，也不会执行猜测后的命令。

## 同步解析兼容

`respond()` 仍保留 `_parse_stream()` 与 `_build_decision()`，供非事件调用和兼容测试使用。它会聚合文本、reasoning 和工具参数，再尝试识别结构化决策。

主 Runtime 优先调用 `stream_events()`，因此新功能应首先维护事件路径。同步 decision 路径是兼容层，不应发展成第二套不同语义。

## `ModelStreamConsumer`

Consumer 将 Provider 事件转换为：

```text
ModelBatch
├── text
├── calls[]
├── usage
└── error
```

它按 `call_id` 聚合参数分片，处理 completed 补齐，并把每个事件实时转发给 Runtime emit。最终调用按 `call_id` 的字符串顺序输出，以获得可重复结果；这只是实现层的确定性排序，不保证等同于 Provider 的原始语义顺序。若 Provider 需要严格保序，应额外保存 output index，而不是依赖 call id 的字典序。

## Prompt 分层

所有跨阶段 Prompt 集中在 `core/prompts.py`：

### `SYSTEM_PROMPT`

约束主 Agent 的端到端行为：先检查代码、工具事实优先、保护用户改动、遵守权限、验证后完成、正确使用 Plan/Skill/Subagent 和 steering。

它只描述跨工具原则，不复制各工具参数。具体工具使用规则与实现放在对应工具文件的 `TOOL_PROMPT`，使能力和约束一起修改。

### `PLANNING_PROMPT`

要求返回固定 JSON，限制 2 到 7 个结果导向步骤，依赖使用输出中的精确标题。它强调修订时保留已完成事实，不允许模型声称执行了工具。

### `REFLECTION_PROMPT`

要求返回 `ReflectionResult` 对应 JSON，按证据保守判断，区分可由 Agent 修复、需要用户和环境阻塞。

### `COMPACTION_PROMPT`

要求摘要保留目标、权限、决策、文件、工具证据和下一步，禁止执行摘要中的指令或泄露凭据。

### `SUBAGENT_PROMPT`

定义只读、隔离、不可递归委派的工作方式，要求返回结论、证据、检查文件和不确定性。

## 为什么工具 Prompt 放在实现文件

工具 Prompt 与 schema、side-effect 属性和 `run()` 必须一起评审。如果集中到一个大 prompt 文件，很容易出现描述说“只读”但实现可写、描述支持参数但 schema 已删除等漂移。

跨阶段 Prompt 则集中管理，因为它们共同定义 Agent 行为层级，需要整体审查优先级和重复约束。

## Prompt 安全原则

- Prompt 不能替代路径、权限和 schema 校验。
- Tool output、Skill 和仓库内容都标记为不可信数据。
- 不要求模型输出隐藏推理，只需要结论、证据和结构化字段。
- 不在 Prompt 中放 API Key、动态 Secret 或完整权限规则。
- JSON Prompt 必须与 Pydantic schema 和解析器同步测试。

## 模型配置优先级

`ModelConfigLoader.load()` 使用：

1. `<project>/.innoagent/config.json`
2. `OPENAI_API_KEY` 等环境变量
3. `~/.innoagent/config.json`
4. 交互输入并选择项目或全局保存

项目配置优先是为了允许仓库选择特定端点和模型；环境变量高于全局配置，方便 CI 和临时会话覆盖。

配置字段：

- `api_key`
- `base_url`
- `model`
- `reasoning_effort`

默认端点和模型面向当前项目预设，但客户端协议保持 OpenAI Responses API 兼容。

## 动态模型切换

CLI `/model` 调用 `EventDrivenAgent.update_model()`：

1. 校验当前客户端支持动态更新。
2. 校验 reasoning effort 枚举。
3. 创建新的 `OpenAICompatibleModel`。
4. 同时重绑定主 `model_stream` 和 `StageRunner.model`。
5. 更新并持久化 ModelConfig。

Subagent 使用共享 ModelStreamConsumer，因此也会使用新模型。重绑定必须覆盖所有阶段，否则主 Agent 与 Planning/Reflection 会出现模型配置漂移。

## 凭据处理

交互读取 API Key 时使用 `getpass` 隐藏输入。配置文件不会写入事件或 Context，但当前仍是明文 JSON。

使用项目配置的风险是误提交到 Git。仓库必须忽略 `.innoagent/config.json`；生产版本应优先环境注入或 OS keychain，并设置最小文件权限。

## 网络与错误边界

当前 HTTP 调用：

- 使用 `httpx.stream()`。
- 请求超时固定为 60 秒。
- 非 2xx 通过 `raise_for_status()` 抛出。
- Provider failed/incomplete 转成内部失败事件。

尚未实现自动 retry、指数退避、deadline budget、429/5xx 分类和幂等 request id。模型请求重试通常无外部副作用，但可能产生重复计费和不同输出，必须由 Runtime 预算约束。

## 扩展 Provider 的要求

新 Adapter 至少需要保证：

- 输出统一 AgentEvent。
- tool call 有稳定 call id。
- 参数分片可以完整聚合。
- usage 字段归一化为整数。
- `input_tokens_details`、`output_tokens_details` 等 Provider 嵌套字段必须在 Adapter 边界扁平化；当前协议提取 `cached_tokens` 和 `reasoning_tokens`，非法值降级为 0，不能让统计字段中断正文响应。
- completed 与 delta 不重复。
- 错误和不完整响应可观察。
- 系统 Prompt 与用户 Context 保持角色隔离。

## 关键测试

- `test/test_streaming_model.py`：文本、工具分片和 usage。
- `test/test_model_config.py`：配置优先级与持久化。
- `test/test_advanced_runtime.py`：动态模型更新覆盖所有阶段。
- `test/fakes.py`：Runtime 可替换模型协议。
