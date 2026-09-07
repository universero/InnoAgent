# 15. Core 内部实现图谱

## 1. 核心对象与所有权

### `ThreadManager`

[`ThreadManager`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/thread_manager.rs#L226) 是进程级组合根。它持有认证、模型管理、扩展注册表、MCP、thread store、agent graph、环境管理等共享依赖，并维护已加载 thread 的映射。它不是会话状态本身，而是创建和定位 `CodexThread` 的工厂/注册表。

关键路径：

- [`start_thread`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/thread_manager.rs#L955)：解析配置与来源，创建 live persistence，spawn Session，注册 thread，发 ready lifecycle。
- [`resume_thread_from_rollout`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/thread_manager.rs#L1046)：读取历史与 metadata，重建配置/上下文并重新打开 writer。
- [`fork_thread`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/thread_manager.rs#L1259)：截取源历史，创建新 id 和 lineage，再走新 thread 初始化。
- subagent 创建同样复用 manager，因此父子线程共享基础设施但不共享可变 turn 状态。

失败不变量：创建失败不能留下“已注册但不可运行”的 thread；live writer 获取失败必须终止启动；shutdown 要先停止活动任务，再 flush/close persistence。

### `CodexThread`

[`CodexThread`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/codex_thread.rs#L177) 是供外部表面持有的线程句柄，内部包裹 `Session`。它暴露 submit、start-or-steer、interrupt、next_event、config snapshot、history snapshot、MCP call、rollout flush 和 lifecycle 方法。这个 facade 避免 App Server/TUI 直接锁 Session 内部字段。

`start_or_steer_turn` 的语义不是盲目创建第二个 task：若已有活动 turn 且允许 steering，则把输入送入当前 turn；否则启动新 turn或返回 NotSubmitted。调用者必须检查 `Started/Steered/NotSubmitted`，不能只依赖请求成功。

### `Session`

[`Session`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/session.rs#L42) 保存 session-scoped mutable state、当前 active task、pending approvals、services、config 和 channels。[`SessionState`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/state/session.rs#L33) 把原先散落字段收拢成持久会话状态；`SessionServices` 承载可共享服务。锁粒度围绕 state/active turn/pending request 分开，避免持锁等待模型或工具 I/O。

## 2. Submission 状态机

[`Op`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/protocol/src/protocol.rs#L592) 是输入控制消息，包含用户输入、interrupt、approval response、compact、review、rollback、MCP refresh、shutdown 等。[`EventMsg`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/protocol/src/protocol.rs#L1356) 是输出事件载荷。

[`submission_loop`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/handlers.rs#L529) 是单一控制面：

| 输入类别 | 处理方式 | 关键不变量 |
|---|---|---|
| turn input | 新建 task 或 steer active turn | 同一 session 最多一个主 task |
| interrupt | 取消 active task/token | 旧 task 结果不能污染下一轮 |
| approval response | 按 request id 完成 oneshot | 未知/过期响应不得执行动作 |
| compact | 本地或远端压缩历史 | 压缩结果作为新历史事实写入 |
| rollback/review | 专用 task handler | 与普通 user turn 区分来源 |
| MCP refresh | 更新 runtime projection | 当前 turn 使用稳定快照 |
| shutdown | 停 task、生命周期、flush store | channel 异常也执行 teardown |

## 3. Turn 上下文

[`TurnContext`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/turn_context.rs#L194) 是一轮执行的冻结上下文，包含 turn id、model/provider、cwd、sandbox/approval、tools、skills、environment、output schema、token/feature 设置和 telemetry。Thread config 可在 turn 开始时被 override，但运行中不应从全局配置重新读取导致语义漂移。

`core/context/*` 把注入模型的系统内容拆成有来源的片段：base/developer/user instructions、environment、permissions、plugins/apps、multi-agent role、current time、Guardian evidence、network rule、compaction summary、turn aborted、unsupported media 等。片段化的价值是可排序、可预算、可按 feature/角色选择，而不是维护一个不可审计的大字符串。

## 4. ContextManager

`context_manager/history.rs` 管模型可见历史，`normalize.rs` 把协议 items 规范化，`updates.rs` 应用增量更新。它处理 assistant message、reasoning、function call/output、compaction 和特殊 context item，避免原始 rollout 全量直接发送给 provider。

重要不变量：

- 每个 function call 与 output 必须可配对；孤立 output 需修复或过滤。
- system/developer 层级不能因 history merge 降级为 user 内容。
- image/file 引用在发送前完成支持性与大小检查。
- compact 后的 summary 替代历史窗口，不删除持久化审计事实。
- 模型切换时按目标模型能力重新规范化，而非复用旧 wire payload。

## 5. `run_turn` 分阶段

[`run_turn`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/turn.rs#L163) 可拆为八个阶段：

1. 提交用户 item，初始化 turn metadata/timing/diff tracker。
2. 解析 skills、plugins、MCP 和 environment，生成本轮 ToolRouter。
3. 构造 `ToolCallRuntime`，仅在工具需要时启动 code-mode worker。
4. `build_prompt` 从 ContextManager 生成 provider-neutral Prompt。
5. sampling loop 建立 Responses stream，处理增量 events。
6. 收到 function call 时并发 dispatch；结果转 `FunctionCallOutput` 回填。
7. 无工具调用且 response complete 时结束；必要时压缩并重试。
8. 写 usage、turn status、diff、rollout，发 completion/abort/error lifecycle。

任务取消必须贯穿 model stream、tool future、exec process 和 event emission。只停止 UI 消费会留下后台副作用，因此 active task 持有 cancellation token 并由 Session 统一中断。

## 6. Core 内部子模块逐组说明

| 子模块组 | 实现职责 | 关键边界 |
|---|---|---|
| `session/*` | spawn、submission、turn context、tasks、approvals、settings、extension metrics | 所有可变会话状态的串行入口 |
| `state/*` | session services/state、service containers | 与持久层 crate 的 SQLite state 不同 |
| `context/*` | 各类模型上下文片段 | 只描述输入，不直接执行能力 |
| `context_manager/*` | 历史规范化、更新、截断 | provider-neutral history |
| `client*`, `responses_*` | 模型请求、stream、metadata、retry | transport 细节下沉到 client crates |
| `compact*` | 本地/远端/v2 压缩、图片与 token budget | 保持恢复语义与审计历史 |
| `tools/*`, `function_tool` | 规格、router、registry、parallel、handlers | handler 内承担审批/隔离 |
| `exec*`, `unified_exec` | 命令准备、环境和 session | 不直接决定产品级审批策略 |
| `agent/*`, `agent_communication` | 子 Agent registry/control/role/status/message | 受 graph、角色和预算限制 |
| `guardian/*`, `safety` | 自动安全审查、证据、反馈 | 不能替代 OS sandbox |
| `mcp*`, `connectors` | MCP runtime projection、调用、文件参数、连接器 | 统一进入 ToolRouter |
| `plugins/*`, `skills` | plugin/skill 注入与展示 | 当前 turn 使用不可变快照 |
| `agents_md*` | 分层发现、缓存和组合 AGENTS.md | 目录作用域决定优先级 |
| `rollout*`, `state_db_bridge` | core 与 store/state 的适配 | 写入失败必须显式上报 |
| `realtime_*` | 实时会话、history、prompt、sideband | 与普通 Responses turn 分开 |
| `environment_selection` | 本地/远端环境选择和状态转发 | 路径、能力、权限共同解析 |
| `image_preparation` | 图片读取、缩放、detail | 控制 payload 和 token 成本 |
| `hook_runtime`, `hook_mcp_executor` | hook 执行适配 | pre/post 时序不可混淆 |
| `turn_diff_tracker`, `turn_metadata`, `turn_timing` | 变更、元数据、耗时 | 为 UI/trace/store 提供一致标识 |
| `shell_snapshot`, `user_shell_command` | shell 环境快照与用户命令 | 与模型工具命令区分来源 |

## 7. 测试重点

`core/src/*_tests.rs` 与 `core/tests` 广泛覆盖：stream retry、context normalization、compaction、approval race、exec policy、MCP tool call、plugin mentions、agent role、environment selection、image validation、event mapping 和 teardown。最关键的回归用例应围绕“中断与完成同时发生”“审批响应晚到”“恢复后重复副作用”“compact 后 tool pair 丢失”“配置在 turn 中途变化”。

## 8. Core 根模块逐项说明

| 根模块 | 具体实现作用 |
|---|---|
| `apply_patch` | 将模型 patch tool 请求转换为 parser 调用、审批、文件变更和事件。 |
| `apps` | 聚合 Codex apps 描述并渲染给模型，连接 connector/plugin catalog。 |
| `client` | 模型 session、Responses stream、provider 请求和错误恢复的核心适配。 |
| `client_common` | client 间共享 headers、metadata、请求参数和错误辅助。 |
| `realtime_context` | 生成实时模式专用上下文，控制文本 turn 与实时会话的状态交接。 |
| `realtime_conversation` | 管理实时 conversation、existing call、sideband 和 BEM 状态。 |
| `realtime_history` | 把实时事件投影为可持久化/展示历史。 |
| `realtime_prompt` | 构建 realtime API 所需指令和会话 prompt。 |
| `responses_metadata` | 规范化客户端传入的 Responses metadata 并限制透传范围。 |
| `responses_retry` | 分类 stream/API 错误并计算有界重试与退避。 |
| `session` | Session spawn、submission handlers、turn context、任务和审批。 |
| `codex_thread` | 对外 thread facade、事件读取、配置快照与 lifecycle。 |
| `compact` | 压缩入口和本地摘要流程。 |
| `compact_model_fallback` | 主压缩模型不可用时选择兼容模型。 |
| `compact_remote` | 远端压缩 API 适配。 |
| `compact_remote_history` | 将历史投影为远端压缩请求并合并结果。 |
| `compact_remote_v2` | 新版远端压缩编排。 |
| `compact_token_budget` | 计算压缩触发阈值与保留预算。 |
| `agent` | Agent resolver、registry、role、status 和 control。 |
| `agent_communication` | 父子/同级 Agent 消息封装和投递。 |
| `attestation` | 处理客户端或执行环境证明信息。 |
| `codex_delegate` | 把部分任务委派到受支持的外部/内部执行路径。 |
| `command_canonicalization` | 展开 shell wrapper并得到可供 policy 判断的规范命令。 |
| `config` | 把通用 config crate 的层结果解析为 core runtime Config。 |
| `connectors` | connector catalog、调用和 context 接入。 |
| `context` | 所有具体上下文片段类型。 |
| `context_manager` | 会话历史规范化、更新与模型窗口构造。 |
| `current_time` | 生成时间上下文并控制刷新频率。 |
| `cyber_access_program` | 处理特定网络/安全访问计划和 turn metadata。 |
| `elicitation` | 注册、等待和完成 MCP/客户端澄清请求。 |
| `environment_selection` | 解析本地/远端环境选择并转发环境状态。 |
| `exec` | 将 sandbox transform 后的请求交给本地/远端执行后端。 |
| `exec_env` | 按 shell environment policy 构建子进程环境变量。 |
| `exec_policy` | 连接解析命令与 Starlark policy，生成 approval requirement。 |
| `guardian` | Guardian prompt、review session、decision、feedback 和 metrics。 |
| `hook_mcp_executor` | 让 hook engine 通过 MCP handler 执行。 |
| `hook_runtime` | 连接 Session 生命周期与 hook dispatcher。 |
| `image_preparation` | 校验、缩放和编码模型输入图片。 |
| `installation_id` | 创建/读取稳定安装标识供诊断和服务请求。 |
| `mcp` | 在 core 中持有/投影 McpRuntime。 |
| `mcp_skill_dependencies` | 计算 skill 对 MCP server/tool 的依赖。 |
| `mcp_tool_approval_templates` | 生成 MCP tool 审批显示内容。 |
| `mcp_tool_exposure` | 按配置、角色和能力决定 MCP 工具可见性。 |
| `mcp_openai_file` | 处理 MCP schema 中 OpenAI 文件参数。 |
| `mcp_tool_call` | 执行 MCP call、account 检查和 telemetry。 |
| `mention_syntax` | 解析 skill/plugin/file 等 mention 语法。 |
| `network_policy_decision` | 把网络访问请求映射为 policy decision 与解释。 |
| `original_image_detail` | 跟踪图片原始 detail，避免转换后丢失语义。 |
| `plugins` | plugin discovery、mention、instructions、render 和 metrics。 |
| `prompt_debug` | 在显式调试模式输出 prompt 构造信息。 |
| `sandbox_tags` | 给请求/事件附加 sandbox 分类标签。 |
| `sandboxing` | core 侧 sandbox policy 选择和 manager 调用。 |
| `session_prefix` | 构建 session 起始上下文/事件前缀。 |
| `session_startup_prewarm` | 并发预热模型、MCP 或其他启动依赖并可取消。 |
| `skills` | 将 skill 选择和内容注入当前 turn。 |
| `stream_events_utils` | Responses 增量事件拼接和统一转换辅助。 |
| `test_support` | core consumers 的 mock server/session fixture。 |
| `unified_exec` | 在 core 层统一 exec/write_stdin session 语义。 |
| `windows_sandbox` | Windows sandbox setup/readiness 调用。 |
| `event_mapping` | 内部细粒度事件到对外 thread item/lifecycle 的映射。 |
| `thread_manager` | 进程级 thread 注册、创建、恢复、fork 和 subagent。 |
| `web_search` | Web search 调用和事件转换。 |
| `windows_sandbox_read_grants` | 管理 Windows read grants 与路径投影。 |
| `agents_md` | 读取并组合 AGENTS.md 指令。 |
| `agents_md_manager` | 缓存、作用域和刷新 AGENTS.md。 |
| `rollout` | core 与 rollout/thread store 的写入适配。 |
| `rollout_budget` | 限制 rollout/context 相关数据规模。 |
| `safety` | 安全判断共享类型和辅助。 |
| `session_rollout_init_error` | 对启动阶段 rollout 错误做可恢复分类。 |
| `shell` | shell 选择、命令和平台行为。 |
| `shell_snapshot` | 捕获可复用的 shell 环境快照。 |
| `spawn` | 统一子进程 spawn 参数和生命周期。 |
| `state_db_bridge` | 将 core lifecycle 投影到 state DB。 |
| `thread_rollout_truncation` | 处理 thread 历史截断与一致边界。 |
| `tools` | tool spec/router/registry/runtime/handlers。 |
| `turn_diff_tracker` | 汇总一轮中工作区文件变化。 |
| `turn_metadata` | 保存 turn id、来源、模型等 metadata。 |
| `turn_timing` | 记录排队、模型、工具和总耗时。 |
| `function_tool` | function tool schema/payload 的共享转换。 |
| `state` | Session 内存状态与共享 services。 |
| `tasks` | turn/compact/review 等异步任务包装。 |
| `user_shell_command` | 区分用户直接 shell 与模型发起命令。 |
| `util` / `utils` | core 内部无状态辅助，避免进入公共 API。 |
| `memory_usage` | 采集运行时内存指标。 |
| `otel_init` | 初始化 core 所需 telemetry provider。 |
| `git_info_tests` | 验证仓库/分支 metadata 提取的测试模块。 |
