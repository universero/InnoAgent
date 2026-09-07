# 18. App Server 与协议内部实现

## 1. 连接层状态机

`app-server-transport` 抽象 stdio、WebSocket 和 UDS；stdio 一行一个 JSON-RPC message。`MessageProcessor` 为每条连接创建 `ConnectionSessionState`，状态从未初始化转为 initialized，记录 client name/version/capabilities/MCP extensions。initialize 之前的业务请求被 gate 拒绝；重复 initialize 也不能覆盖已建立身份。

`connection_rpc_gate` 控制请求并发与连接关闭，`connection_cleanup` 清理订阅、pending server requests、filesystem watches 和 event streams。`outgoing_message` 维护 response/notification 目标连接及写入完成；connection-aware request id 防止不同客户端 id 冲突。

## 2. 请求分派结构

[`handle_initialized_client_request`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/app-server/src/message_processor.rs#L975) exhaustive match `ClientRequest`。域 processor 与职责如下：

| Processor/模块 | 方法族 | 如何落到内部实现 |
|---|---|---|
| `initialize_processor` | initialize/capability | 构建 connection session |
| `thread_processor` | start/resume/fork/read/list/search/archive/revert/rollback | `ThreadManager` + `ThreadStore` |
| `turn_processor` | start/steer/interrupt/review/realtime/settings | `CodexThread` submit/start_or_steer |
| `config_processor` | read/write/batch/requirements | config manager/service |
| `account_processor` | login/logout/status/rate limits | auth manager/backend |
| `mcp_processor` | status/oauth/refresh/resources | MCP runtime/connection set |
| `plugins`, `marketplace_processor` | list/install/update/remove | PluginsManager/store |
| `apps_processor`, `catalog_processor` | apps/tools/catalog | extension/plugin catalogs |
| `environment_processor` | add/info/status | environment manager/exec server |
| `fs_processor` | read/write/list/copy/remove/watch | local/remote FS abstraction |
| `command_exec_processor`, `process_exec_processor` | one-shot/process session | exec server |
| `projects`, `thread_sections` | project/section CRUD | ThreadStore/state projection |
| `thread_goal_processor`, `thread_queue_processor` | goal/queue lifecycle | ext goal/queue + state |
| `feedback_processor` | feedback/doctor report | feedback bundle |
| `windows_sandbox_processor` | readiness/setup | Windows sandbox service |
| `remote_control_processor` | pairing/client/status | remote control state |

## 3. `thread/start`

[`thread_start_inner`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/app-server/src/request_processors/thread_processor.rs#L1121) 的输入处理顺序很重要：

1. 检查请求 history mode 与 store 是否支持分页历史。
2. 校验 project id 并从 store 读取 project。
3. 解析 legacy sandbox 与 granular permissions，拒绝冲突组合。
4. 解析 runtime workspace roots 和 environment selections。
5. 合并 config/profile/client metadata，形成 StartThreadOptions。
6. 调用 ThreadManager，注册 event forwarding 和 subscription。
7. 返回 thread snapshot；后续状态通过 notification 更新。

若先创建 Session 再校验 project/store 能力，会遗留半初始化 thread；当前顺序避免该问题。

## 4. `turn/start`

[`turn_start_inner`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/app-server/src/request_processors/turn_processor.rs#L514) 先加载 thread 并检查 direct input 是否允许。`toolOutput` 与非空普通输入互斥，tool name 不得为空；文本字符数有上限；ResponseItem 中图片 URL 需验证。

随后解析 cwd、workspace roots、environment、additional context 和 thread settings overrides，构造 `TurnInputRequest`。`start_or_steer_turn` 返回三态：Started 生成新 turn；Steered 把输入加入已有 turn；NotSubmitted 转内部错误。只有真正新启动且有输入时才触发 memories startup task。request id 与 turn id 的映射在响应前记录，保证后续通知可关联。

## 5. 通知映射

`bespoke_event_handling` 和 protocol `event_mapping` 把 core Event 转成 ServerNotification。常见生命周期为：`thread/started`、`turn/started`、`item/started`、delta、`item/completed`、usage、`turn/completed`。审批则反向发 server request，等待 client response。

`thread_state`/`thread_status` 聚合活跃、空闲、错误状态；`token_usage_replay` 在恢复时补齐 usage；`thread_enrichment` 把 metadata/project/preview 注入列表响应；`notification_media` 处理媒体数据，避免在普通 payload 中失控膨胀。

## 6. 协议代码生成

`app-server-protocol/protocol/common.rs` 的宏生成 [`ClientRequest`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/app-server-protocol/src/protocol/common.rs#L208) 和 [`ServerNotification`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/app-server-protocol/src/protocol/common.rs#L1652)。`v1` 保持旧契约，`v2` 按领域拆文件；`export`/`precomputed_exports` 生成 JSON Schema/TypeScript/Python artifacts；fixture tests 检查生成物与源码一致。

兼容约束：新增 optional field 通常可向后兼容；重命名 method、改变 enum tag、把 optional 改 required 均是破坏性变更。experimental API 必须显式标识，客户端 capability 决定是否可调用。

## 7. 其他 App Server 内部模块

| 模块 | 实现职责 |
|---|---|
| `config_manager*`, `config_layer` | 配置读写、reload、分层来源和诊断 |
| `models_refresh_worker` | 后台刷新模型目录并通知客户端 |
| `skills_watcher`, `plugin_config_reload`, `mcp_refresh` | 文件变化触发能力重载 |
| `dynamic_tools` | 客户端动态工具注册/调用 |
| `fs_watch`, `fuzzy_file_search` | 文件监听和候选搜索 |
| `command_exec` | App Server 自身命令执行适配 |
| `code_mode_host` | code mode host 生命周期 |
| `external_auth`, `auth_mode`, `attestation` | 外部认证与客户端证明 |
| `turn_cost_worker` | 异步计算/获取 turn 成本 |
| `otel_reloader`, `app_server_tracing` | 动态遥测与 request trace |
| `image_url` | 输入图片 URL 校验 |
| `filters` | thread/list/search 条件转换 |
| `in_process` | 同进程 App Server transport |

## 8. 失败与并发语义

- 所有 request 必须恰好一个 response/error；通知没有 response。
- response writer 失败不能让已执行的副作用自动重试。
- connection close 清理 pending request context；审批等待方收到 abort/error。
- event subscription 与 thread 生命周期分离，unsubscribe 不等于停止 thread。
- filesystem watch、MCP event stream 等 connection-scoped 资源必须在断连时释放。
- 对大输入、未知 thread、unsupported store operation 使用稳定错误码，而非 panic。

## 9. 测试面

`app-server/tests/suite/v2` 对几乎每个方法族做端到端测试；protocol 有 serialization/schema fixture；message processor 有 tracing 测试；config manager、model refresh、external migration、turn cost、transport 都有独立测试。新增 RPC 时至少要同时更新 enum、processor、schema fixture、SDK artifacts 和请求/通知集成测试。

## 10. App Server 根模块补充索引

| 模块 | 具体职责 |
|---|---|
| `analytics_utils` | 把 RPC 方法、错误类型、thread/turn id 转为分析事件字段。 |
| `app_info` | 汇总 server version、features 和运行环境信息。 |
| `app_server_tracing` | 为连接和请求建立 trace，传播 request context。 |
| `attestation` | 验证/转换客户端证明信息。 |
| `auth_mode` | 解析当前账号与允许的认证模式。 |
| `codex_home_metrics` | 统计 Codex home 中配置/状态资产的健康指标。 |
| `config_manager_service` | 把 ConfigManager 暴露为可复用异步服务并缓存有效状态。 |
| `current_time` | 为请求和通知提供一致服务器时间。 |
| `effective_plugin_change` | 比较 reload 前后有效插件，决定通知和 runtime 刷新。 |
| `error_code` / `server_request_error` | 将领域错误稳定映射为 JSON-RPC code/data。 |
| `external_agent_migration` | detect/import 外部配置及 session 迁移。 |
| `models` | App Server model response 投影。 |
| `request_serialization` | 控制实验字段和客户端版本对应的序列化范围。 |
| `user_verification*` | 用户验证 API、认证和响应转换；不可用时返回明确错误。 |

其中 `user_verification_response` 专门把验证领域结果映射为协议响应，避免 processor 直接拼 JSON。
