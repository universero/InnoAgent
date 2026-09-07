# 21. 用户表面与 SDK 内部实现

## 1. CLI

`cli` 的职责是解析顶层命令、加载通用 override、选择 TUI/exec/app-server/MCP/sandbox/cloud/plugin/marketplace 等入口。`arg0` 支持同一 binary 依据 argv[0] 充当 helper。CLI 应保持 orchestration 薄层，业务验证下沉到 config/core/processor，避免不同入口行为漂移。

主要命令域包括登录、交互、非交互 exec、resume/fork、app-server、MCP server 管理、cloud tasks、sandbox debug、plugin/marketplace 和 completion。命令返回码必须由最终 turn/operation 状态决定，而不是“请求成功发送”。

## 2. TUI 顶层循环

[`ChatWidget`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/tui/src/chatwidget.rs#L554) 是主要会话 UI 状态，`App` 管多 thread/页面和全局事件。[`AppEvent`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/tui/src/app_event.rs#L263) 是 UI actor 消息，`AppEventSender` 被输入、后台任务和 server event handler 共享。

输入路径：terminal key/paste -> composer -> slash command/mention/attachment 解析 -> input queue -> App Server/core submit。输出路径：server/core event -> app event routing -> thread target -> ChatWidget protocol handler -> history cell/stream state -> Ratatui render。

## 3. TUI 内部模块分组

| 模块组 | 实现内容 |
|---|---|
| `tui/*`, `frames`, `render`, `style`, `color` | terminal 初始化、帧、主题和绘制 |
| `app/*` | 全局事件分发、thread 路由、启动/恢复/重连、页面状态 |
| `chatwidget/*` | 单会话协议处理、输入、stream、exec、approval、goal/plugin/MCP UI |
| `bottom_pane/*` | composer、popup、question、approval、status、skill/plugin 选择 |
| `history_cell/*` | message/reasoning/tool/diff/session 等历史单元 |
| `markdown*`, `live_wrap`, `transcript_reflow` | 增量 Markdown 与 resize reflow |
| `streaming` | answer delta 合并、flush、完成边界 |
| `resume_picker`, `session_resume`, `session_archive_commands` | 列表分页、恢复、归档 |
| `app_server_session`, `app_server_connection` | RPC client、request handles、history adapters |
| `approval_events`, `permission_*` | 权限展示和 decision 提交 |
| `multi_agents`, `app/agent_*` | Agent overview、导航、状态 feed |
| `dynamic_tools*`, `hooks_rpc` | 动态工具和 hook 交互 |
| `file_search`, `mention_codec`, `task_mentions` | `@`/路径/任务引用 |
| `clipboard_*`, `external_editor`, `pager_overlay` | 终端外部交互 |
| `updates*`, `npm_registry` | 更新检查和版本提示 |

UI 状态尤其要处理乱序/重复事件：started item 映射到完成 item；恢复 session 在新 usage 到达前不显示伪造 token；旧 git/status 后台响应不能覆盖新请求；resize 时流式表格与普通文本采用不同 reflow。

## 4. Exec 表面

[`exec::run_main`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/exec/src/lib.rs#L258) 加载 bootstrap config、建立 App Server/core session、启动或恢复 thread、提交 turn并消费通知。`worktree` 可为任务建立隔离工作树。

`EventProcessorWithHumanOutput` 把通知渲染到 stderr/stdout，维护最终消息和 usage；`EventProcessorWithJsonOutput` 输出稳定 `ThreadEvent` JSONL，并用 raw id -> exec item id 映射协调 started/completed。结束时会 reconcile 尚未完成的 started items，避免消费者留下悬挂状态。

human output 和 JSON output 必须分离：JSON 模式不能混入进度文字；最终 answer 文件写入也要在进程退出前完成。SIGINT/timeout 映射为 interrupt，退出码反映失败或拒绝。

## 5. TypeScript SDK

`sdk/typescript/src/codex.ts` 配置 binary/base URL/API key；`exec.ts` spawn CLI 并读取 JSONL；`thread.ts` 提供 start/resume/run/runStreamed；`items.ts`/`events.ts` 定义公开对象；`outputSchema.ts` 创建临时 JSON Schema。

[`runStreamedInternal`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/sdk/typescript/src/thread.ts#L70) 的关键行为：规范化文本/图片输入；将 thread options 映射给 exec；逐行 `JSON.parse`；从 `thread.started` 捕获 thread id；兼容补齐 usage 字段；generator finally 清理临时 schema。AbortSignal 必须终止子进程，而不只是停止迭代。

## 6. Python SDK

Python SDK 的 `_app_server.py`/client 管理子进程、stdin/stdout JSON-RPC、request id 和 initialize；`_message_router.py` 把 response、global/login/turn/goal notification 分流；`_thread.py`/`_turn.py` 提供高层对象；`generated/*` 来自协议 schema。

[`MessageRouter.route_notification`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/sdk/python/src/openai_codex/_message_router.py#L176) 先识别 login，再识别 thread goal，最后按 turn id 路由。通知可能早于 `register_turn`，因此 pending deque 缓存；未知 turn 的 completed 直接清缓存，避免永久泄漏。`fail_all` 在进程退出时唤醒全部等待者。

`python-runtime` 固定匹配版本的 Codex binary，解决“用户机器没有 CLI”与协议版本漂移，但增加 wheel 体积和多平台发布矩阵。

## 7. 表面一致性要求

| 语义 | TUI | exec | TS SDK | Python SDK |
|---|---|---|---|---|
| transport | in-process/App Server | core/App Server event | 子进程 JSONL | App Server JSON-RPC |
| 多轮 thread | 完整 | resume 支持 | 保存 thread id 后重启 exec | 长驻连接与 thread 对象 |
| 双向审批 | 交互 UI | 受非交互策略限制 | 受 CLI 能力限制 | 原生 server request/response |
| 事件兼容 | UI adapter | human/JSON processors | JSON parse + compatibility | generated registry/router |
| 取消 | UI interrupt | signal/interrupt | AbortSignal -> process | RPC interrupt/close |

测试必须以同一行为契约覆盖四个表面，而不是要求内部实现相同。

## 8. TUI 根模块逐项索引

以下模块均属于 UI 表面，不拥有核心执行权限：

| 模块 | 具体职责 |
|---|---|
| `additional_dirs`, `cwd_prompt`, `working_directory` | 收集并展示 cwd/额外目录选择。 |
| `app`, `app_event`, `app_event_sender` | 顶层状态机、消息类型与发送句柄。 |
| `app_backtrack`, `unarchive_prompt` | UI 历史回退与恢复确认。 |
| `app_command`, `slash_command` | 解析本地 UI 命令并映射 action。 |
| `app_info`, `session_state`, `thread_transcript` | 展示应用/thread 元数据和 transcript。 |
| `app_server_connection`, `app_server_session` | 连接、request handle、history/model/fs adapters。 |
| `app_server_approval_conversions`, `approval_events` | 协议审批与 UI decision 转换。 |
| `ascii_animation`, `motion`, `pets`, `daybreak` | 可选动画/视觉主题状态。 |
| `assistant_directives` | 处理 assistant 发给 UI 的受控 directive。 |
| `auto_review_denials` | 展示自动审查拒绝和后续动作。 |
| `backend_banners`, `notifications` | 后端公告、警告和通知呈现。 |
| `bottom_pane` | composer、popup、approval、question、status 等底部交互集合。 |
| `branch_summary`, `get_git_diff`, `diff_model`, `diff_render` | 获取并展示 Git/turn diff。 |
| `chatwidget` | 单 thread 的输入、事件、stream 和工具 UI 核心。 |
| `clipboard_copy`, `clipboard_paste`, `clipboard_html` | 终端剪贴板与 HTML/text 转换。 |
| `collaboration_modes`, `model_catalog`, `model_migration` | 模式/模型选择与迁移提示。 |
| `color`, `style`, `theme_picker`, `terminal_palette` | 色彩主题和终端适配。 |
| `config_update`, `local_settings`, `managed_new_thread_defaults` | 用户设置写入与有效默认值。 |
| `custom_terminal`, `terminal_probe`, `terminal_title` | 终端后端、能力探测和标题。 |
| `debug_config`, `experimental_features` | 配置诊断与 feature UI。 |
| `permission_discovery`, `permission_shortcuts` | 可用 profile 探测和快捷切换。 |
| `worktree_browser`, `worktree_startup` | worktree 选择/创建进度。 |
| `dynamic_tools`, `dynamic_tools_mcp` | 客户端动态工具及 MCP 暴露。 |
| `exec_cell`, `exec_command` | 命令 item 的状态与展示。 |
| `external_agent_config_migration` | 外部配置导入 UI。 |
| `external_editor`, `pager_overlay` | 调用编辑器/分页器并恢复 terminal。 |
| `file_search`, `mention_codec`, `task_mentions` | 搜索和编码输入引用。 |
| `frames`, `render`, `public_widgets`, `ui_consts` | Ratatui layout、widgets 和常量。 |
| `git_action_directives` | 解析受控 Git 操作 directive。 |
| `goal_display`, `goal_files`, `session_queue_commands` | Goal/queue 的显示和操作。 |
| `history_cell`, `insert_history` | 把完成 item 变成可滚动历史。 |
| `hooks_rpc`, `startup_hooks_review` | hook 状态、审批和启动检查。 |
| `ide_context`, `inline_visualization` | IDE 上下文和内联可视化。 |
| `key_hint`, `keymap`, `keymap_setup` | 快捷键解析、冲突和提示。 |
| `line_truncation`, `width`, `wrapping` | Unicode/终端宽度与安全截断。 |
| `live_wrap`, `resize_reflow_cap`, `transcript_reflow` | 增量文本在 resize 时重排。 |
| `markdown`, `markdown_render`, `markdown_stream`, `markdown_text_merge`, `table_detect` | Markdown 增量解析、表格识别和渲染。 |
| `multi_agents` 与 `app/agent_*` | 子 Agent 列表、状态、导航和详情。 |
| `named_session_lookup`, `resume_picker`, `session_resume` | 按 metadata 查询和恢复 thread。 |
| `onboarding`, `oss_selection` | 首次启动和 OSS provider 选择。 |
| `selection_list`, `tooltips` | 通用选择器和帮助浮层。 |
| `service_tier_resolution`, `token_usage` | tier 选择和 token/窗口显示。 |
| `session_archive_commands`, `session_start`, `startup_*` | 会话启动、归档、preflight、错误和草稿恢复。 |
| `session_log` | 本地 TUI session 诊断日志。 |
| `skills_helpers` | skill mention、列表和启用状态辅助。 |
| `status`, `status_indicator_widget` | 当前任务、连接和资源状态。 |
| `streaming` | assistant delta 缓冲、合并和 flush。 |
| `temporary_structured_request` | 临时结构化交互请求状态。 |
| `terminal_hyperlinks`, `terminal_visualization_instructions` | OSC 链接与终端可视化提示。 |
| `text_formatting` | 文本段落、缩进和样式辅助。 |
| `update_action`, `update_prompt`, `update_versions`, `updates`, `updates_cache`, `npm_registry` | 更新发现、版本比较、缓存和交互。 |
| `vim_search` | Vim 风格 transcript 搜索。 |
| `windows_sandbox` | Windows sandbox setup/readiness UI。 |
| `workspace_command`, `workspace_messages` | workspace 级命令和消息。 |
| `test_backend`, `test_support`, `tests` | 渲染/event 测试后端与 fixture。 |

`local_chatgpt_auth` 处理仅用于本地 TUI 的账号状态适配；`startup_draft`、`startup_error`、`startup_orchestration`、`startup_preflight` 分别负责草稿恢复、错误表示、启动步骤编排和前置检查；`version` 提供 UI 版本信息；`daemon_startup_tests` 验证 daemon 连接启动路径。

## 9. Exec 根模块逐项

| 模块 | 具体职责 |
|---|---|
| `cli` | 定义非交互参数、resume/review/schema 等选项。 |
| `event_processor` | 统一处理器 trait 与终止状态。 |
| `event_processor_with_human_output` | 彩色文本、工具摘要、最终答案与 usage。 |
| `event_processor_with_jsonl_output` | 稳定机器事件、item id 映射和 reconciliation。 |
| `exec_events` | 面向 SDK/脚本的 JSONL event types。 |
| `worktree` | exec 请求的临时/托管 worktree 准备。 |
| `tests` | CLI、输出、退出码、恢复和 schema 测试。 |
