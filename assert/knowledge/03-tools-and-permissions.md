# 工具与权限

## 模块定位

工具层是模型与真实环境之间的可信执行边界。模型只能输出工具名和 JSON 参数，不能直接读取文件、启动进程或修改 Plan。所有调用都必须经过统一链路：

```text
Model Tool Intent
-> Pydantic schema validation
-> canonical arguments
-> Guardrail preflight / Permission decision
-> ToolAuthorization
-> allowed: Tool execution
-> needs_confirmation: ApprovalRequest + pause
-> Guardrail postflight
-> output bounding
-> ToolResult + AgentEvent + AgentState
```

主要模块：

| 文件 | 职责 |
|---|---|
| `core/tool/base.py` | `ToolCall`、`ToolSpec`、`ToolAuthorization`、`ToolResult`、`BaseTool` |
| `core/tool/approval.py` | `ApprovalRequest`、稳定选项和 CLI decision 解析 |
| `core/tool/registry.py` | 注册、参数规范化、授权、Guardrail 链、执行和批量调度 |
| `core/tool/decorators.py` | 内置工具注册标记 |
| `core/tool/search.py` | 工具较多时的轻量候选搜索 |
| `core/tool/*_tool.py` | 每个工具的 Prompt、schema 和实现 |
| `core/guardrails` | 路径、写前读取、审批、只读模式和 Plan 校验 |
| `core/config/permissions.py` | 仓库级持久授权与拒绝规则 |

## 基础契约

### `ToolInput`

所有内置工具参数继承 `ToolInput`，配置 `extra="forbid"`。未知字段直接返回模型可见的参数错误，避免模型拼错参数后工具静默忽略。

每个字段都提供 description、长度、范围或枚举约束，原因是工具 schema 本身就是模型提示的一部分。仅在自然语言描述约束、但 schema 接受任意值，会让 Provider 的结构化调用失去价值。

### `BaseTool`

每个工具声明：

- `name`：稳定调用名。
- `description`：模块级 `TOOL_PROMPT`。
- `input_model`：Pydantic 参数模型。
- `is_write`：是否可能产生副作用。
- `requires_confirmation`：是否需要纳入审批策略。
- `parallel_safe`：是否允许并发执行。

这些属性由代码定义，不能由模型参数覆盖。

`BaseTool.execute()` 统一捕获参数错误和运行异常，将其转换为 `ToolResult(status="error")`。工具异常不应击穿 LangGraph，也不能只打印到终端后消失。

### `ToolSpec`

`BaseTool.spec()` 同时给出模型契约和本地可信能力：name、description、parameters、`is_write`、`requires_confirmation`、`parallel_safe`。Provider adapter 只发送前三个标准 function 字段；其余字段只供 Registry、权限和界面使用，不能由模型参数伪造。

### `ToolAuthorization`

授权对象保存 tool name、规范化 arguments、状态、原因和 Guardrail metadata。状态只有 `allowed`、`needs_confirmation`、`blocked`、`error`。执行阶段使用授权对象中绑定的 arguments，而不是再次信任调用方传入的可变字典；工具名不匹配时直接返回 error。

参数校验发生在 Guardrail 和审批之前。无效参数因此直接成为 error，不会先让用户批准一个最终无法执行的调用。工具执行时仍保留二次 schema 校验，作为扩展工具和直接调用路径的防御层。

### `ToolResult`

| 字段 | 用途 |
|---|---|
| `status` | `success`、`error`、`needs_confirmation`、`blocked` |
| `output` | 进入模型 tool message 的有界摘要 |
| `data` | Runtime 消费的结构化数据，如 Plan、Task、Skill 正文 |
| `warnings` | 非致命问题，如结果截断 |
| `metadata` | 路径、哈希、exit code、Guardrail 记录等审计信息 |

模型主要看到 `output`，Runtime 和 UI 可以读取完整结构化字段。这样避免把大对象重复塞入模型上下文，同时保留状态更新能力。

### `ApprovalRequest`

审批请求是可序列化的暂停协议，包含 request id、首个工具摘要、当前待批 calls、顺序上尚未执行的 deferred calls、reason 和结构化 options。TUI 展示 label/description，Runtime 只消费稳定 value。旧 Session 的字符串 options、`id` 字段和旧 `needs_confirmation` 事件在恢复时升级，不改变授权语义。

## Registry 授权与执行链

`authorize_tool()` 与 `execute_authorized()` 是两个明确阶段：

1. 查找工具；未知工具返回 error authorization。
2. 用 Pydantic 校验并规范化参数。
3. 逐个执行 Guardrail `before()`，任一异常都 fail-closed。
4. 返回 allowed、needs_confirmation、blocked 或 error authorization。
5. Runtime 对 needs_confirmation 创建 `ApprovalRequest`，不产生 ToolResult。
6. `execute_authorized()` 校验授权绑定的工具名并执行绑定参数。
7. 逆序执行 Guardrail `after()`，再限制模型可见输出。

兼容入口 `execute_tool()` 仍可把非 allowed authorization 转成旧式 ToolResult，供独立工具调用和已有集成使用；主 Runtime 不走这条审批表达方式。postflight 若异常，工具可能已经产生副作用，因此结果会明确标记 `effect_uncertain`，而不是声称操作从未执行。

Guardrail 默认安装顺序：

```text
PlanGuard
-> PathGuard
-> WriteBeforeReadGuard
-> FileConfirmationGuard
-> ReadOnlyGuard
```

先校验 Plan 与路径，再进行预读和权限判断，可避免对非法目标做不必要操作。`ReadOnlyGuard` 在链尾再次兜底，保证未来修改 Confirmation 逻辑时仍不会放过写操作。

## 内置工具实现

### `read`

用途：读取工作区内 UTF-8 文本，可指定 1-based、闭区间的行范围。

实现要点：

- 先读取原始 bytes，再以 `errors="replace"` 解码，保证异常编码不会让图崩溃。
- 返回总行数、实际返回范围、byte/character count 和 SHA-256。
- 行范围在 schema 层校验，拒绝结束行小于起始行。

SHA-256 是后续 `write.expected_sha256` 的版本标识，不代表内容安全或来源可信。

### `write`

用途：创建或完整替换一个 UTF-8 文件。

实现步骤：

1. 解析工作区路径并创建父目录。
2. 读取旧 bytes，计算旧 SHA-256。
3. 若提供 `expected_sha256`，先检查读取后是否已发生变化。
4. 在目标同目录创建临时文件，写入、flush 并 `fsync`。
5. 已有文件时复制 POSIX mode bits。
6. 替换前再次检查目标哈希，缩小并发覆盖窗口。
7. 使用 `os.replace()` 原子替换。
8. 异常时清理临时文件。

同目录临时文件确保 rename/replace 通常处于同一文件系统，避免跨设备时失去原子性。双重哈希检查是乐观并发控制，不是文件锁；检查与 replace 之间仍存在极小竞争窗口，生产环境可进一步引入版本化 patch 或文件锁。

该工具重写整个文件，不适合巨型文件或高冲突协作。后续应增加 patch/diff 工具，而不是继续堆叠 write 参数。

### `ls`

用途：列出目录，递归深度限制为 1 到 5。

递归时会展示符号链接目录，但不会进入链接目标，防止遍历逃逸和循环。扫描过程同时受 `max_entries` 和 Runtime 字符预算约束，达到上限立即停止，不会先构造完整目录列表；Registry 仍保留最终兜底截断。

### `grep`

用途：搜索工作区文本，默认正则，可选择固定字符串、忽略大小写、glob 和最大返回条数。

实现优先使用 `rg`，不可用时回退到 `grep -rn`。命令中使用 `--` 结束选项，确保以 `-` 开头的 pattern 不会被解释成参数。返回 match count、returned count 和截断 warning。

`rg`/`grep` stdout 逐行消费，达到 `max_results` 或字符预算后立即终止子进程，不在内存中缓存完整搜索输出。

### `shell`

用途：执行一个非交互 Shell 命令。它被标记为写能力、需要确认且不可并行。

实现使用 `subprocess.Popen(shell=True)`：

- cwd 固定为首个 allowed root。
- stdout 与 stderr 由独立 reader 持续排空，每个缓冲区只保留有界字节，并在 output 中组合有界摘要。
- timeout 范围为 1 到 600 秒。
- POSIX 系统使用新 session，使子进程进入独立进程组。
- 超时后向进程组发送 `SIGKILL`，等待 reader 和进程完成回收。
- ToolResult 记录 stdout、stderr、exit code 和 timeout。

Shell 仍可通过命令访问工作区外资源，也没有网络和资源隔离，因此它不是沙箱。Prompt 只能减少误用，真正的安全需要容器或 MicroVM。

### `plan`

用途：进入 Planning 子图创建或修订计划。工具本身不修改文件，也不执行命令。

它通过 Runtime 注入的 `plan_runner` 调用模型阶段；独立测试或服务缺失时回退到 `PlanningService`。返回 Plan、Tasks、usage 和 warning，并标记 `parallel_safe=False`，避免与共享模型流并发。

### `task`

用途：在已有 Plan 下创建任务或更新一个任务状态。

- 创建时必须有 title，status 默认 `pending`。
- 更新时必须显式传入 task_id 和 status，避免无意重置。
- `blocked` 必须有具体 result 说明阻塞原因。
- `done` 应在验证后设置，而不是模型声称完成时设置。
- 更新后通过 `sync_steps_from_tasks()` 同步 PlanStep。

### `skill`

用途：按精确名称激活一个已发现 Skill。

工具 output 只返回激活结果，完整 Skill 正文放在 `data.active_skill`，由 Runtime 写入 `active_skills` 并在下一轮 Context 注入。这样避免正文同时出现在 tool message 和 Skill section 中造成双份 token。

### `subagent`

用途：委派一个自包含的只读调查任务。

schema 只允许 `read`、`ls`、`grep`，Runtime 再与注册表和只读白名单求交集，形成双层约束。Subagent 共享模型适配与事件通道，因此当前串行执行，不标记 parallel safe。

## 动态工具描述与搜索

`tool_schemas(context)` 会调用 `dynamic_description()`。Plan 和 Task 工具因此可以把当前 revision、步骤数或任务统计暴露给模型，而不改变固定 schema。

`search_tools()` 当前使用简单关键词评分，适合工具数量较少的阶段。其接口已经允许传入 Context，未来可以替换为 embedding 或规则检索，而不改变 Runtime 的工具描述格式。

## 并行调度

`execute_many()` 按原始顺序扫描调用：

- 相邻的 `parallel_safe=True` 且 `is_write=False` 调用组成一个并行窗口。
- 写操作、Shell、未知工具和共享状态工具先刷新前一窗口，再串行执行，并阻止后续只读调用越过。

并发结果按原始调用索引回填，而不是按完成顺序返回。模型看到稳定顺序，事件和测试也不会因线程调度产生随机变化。

Runtime 按顺序授权，遇到首个 `needs_confirmation` 就停止：此前前缀可以执行，该调用成为 ApprovalRequest，后续调用保存为 deferred tail。审批恢复后先处理被阻塞调用，再继续 tail；因此 `[write, read]` 不会在 write 等待审批时提前读取旧内容。Registry 收到的 call 与 authorization 数量不一致会直接报错，避免错位授权。

当前并行安全依赖工具作者声明。新增工具时必须检查：

- 是否写同一文件或共享缓存。
- 是否复用非线程安全客户端。
- 是否改变 AgentState、Plan、Skill 或权限。
- 是否会启动长时间进程。

## 权限模式

| 模式 | 行为 |
|---|---|
| `ask` | 写工具和 Shell 默认询问，命中持久 allow 时跳过 |
| `auto` | 自动执行，但 deny、路径边界和其他 Guardrail 仍生效 |
| `readonly` | 阻止所有 `is_write=True` 工具 |

审批选项：

- `allow_once`：只批准当前 pending call。
- `allow_always`：写入当前仓库 `.innoagent/permissions.json`。
- `deny`：当前调用返回 blocked；持久 deny 规则也可由 PermissionStore 表达。

## 持久权限规则

`PermissionStore` 的匹配策略：

- deny 优先于 allow，避免宽泛 allow 覆盖明确拒绝。
- 文件规则保存规范化的仓库相对路径；工作区外路径保存绝对路径，但仍会被 PathGuard 拦截。
- v3 Shell 规则保存原始 `command_text` 并逐字匹配。引号、转义、变量展开和命令替换形式不同，就必须重新审批。
- v1/v2 的 argv/prefix allow 规则无法恢复原始 shell 语义，因此迁移后失效关闭；旧 deny 规则仍可通过 `shlex` 保守匹配并继续拦截。
- 无 path 和 command 的工具按完整 arguments 匹配。
- 权限文件带 `version: 3`，通过同目录临时文件和 `os.replace()` 原子更新。
- 已存在的文件若 JSON 损坏、schema 非法或版本未知，权限查询返回 deny-by-default；缺少文件才表示没有持久规则。

`allow_always` 不是关闭安全机制。持久 allow 只跳过确认，仍受 PathGuard、readonly 和工具 schema 限制。

## 路径边界

`ToolContext.resolve_path()` 将相对路径基于首个 allowed root 解析，并调用 `Path.resolve()` 归一化。`PathGuard` 再检查解析结果是否位于任一 allowed root 内，因此 `..` 和指向外部的符号链接不能绕过边界。

Shell 不使用 path 参数，无法由 PathGuard 解析命令内部路径。这是 Shell 必须单独隔离的原因。

## Write Before Read

`WriteBeforeReadGuard` 会在写入前读取已有目标的有限内容，或列出新文件父目录。其作用是：

- 为审批和审计提供目标现状。
- 降低盲目覆盖概率。
- 让 Runtime 知道目标是否存在。

它不是用户主动审阅的替代，也不能阻止读取后并发修改；真正的冲突保护由 `expected_sha256` 提供。

## 错误与结果上限

- schema 错误、未知工具和授权异常都会在执行前 fail-closed，并成为模型可见 error ToolResult。
- `needs_confirmation` 只存在于授权协议；主 Runtime 不把它追加到模型消息。
- postflight 失败会返回 `effect_uncertain`，提醒调用方副作用可能已经发生。
- read、ls、grep 和 shell 在读取或捕获阶段就实施上限；Registry 对所有工具的 output 再做最终兜底截断并添加 warning。
- Shell 的 `data.stdout/stderr` 同样有界；其他工具的任意 `data` 与 metadata 仍需实现者主动限制，不能塞入无限大对象。
- 工具“成功”仅表示系统调用完成，不能证明业务目标正确。

## 扩展工具的检查清单

1. Prompt 是否说明何时使用、何时不用和危险边界。
2. schema 是否拒绝未知字段并约束范围。
3. 是否正确设置 `is_write`、`requires_confirmation`、`parallel_safe`。
4. 是否通过 `ToolContext` 获取路径、状态和服务。
5. 是否返回结构化失败而不是抛裸异常。
6. 是否限制输出、超时和资源。
7. 是否需要 operation id、幂等键或补偿。
8. 是否有成功、参数错误、权限拒绝、超时和边界测试。

## 关键测试

- `test/test_tools.py`：schema、read/write、grep、ls、shell、Skill 和 Task 契约。
- `test/test_permissions.py`：ask、allow once、allow always、deny 和 readonly。
- `test/test_approval_protocol.py`：审批选项、call id、旧格式升级和 CLI aliases。
- `test/test_parallel_tools.py`：并行执行与返回顺序。
- `test/test_main_loop.py`：审批暂停和恢复。
