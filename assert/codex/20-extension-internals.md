# 20. 扩展生态内部实现

## 1. Extension API 的类型化贡献

[`ExtensionRegistryBuilder`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/ext/extension-api/src/registry.rs#L26) 在启动阶段接受 contributor，最终生成 [`ExtensionRegistry`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/ext/extension-api/src/registry.rs#L147)。registry 按贡献类型保存 trait object 列表，不让扩展互相依赖具体实现。

贡献点及调用时机：

| Contributor | 调用时机 | 产物/副作用 |
|---|---|---|
| config | runtime/config 构建 | 配置片段与约束 |
| thread lifecycle | start/resume/ready/idle/stop | 扩展 session data、清理 |
| turn lifecycle | turn start/complete/abort | per-turn 状态与任务 |
| context | prompt 构建前 | 有预算的 context fragments |
| tools | ToolRouter 构建 | specs + handlers |
| pre/post tool | 工具前后 | allow/block/结果检查 |
| skills | skill catalog 构建 | descriptors/resources |
| MCP server | MCP config/runtime 构建 | server definitions |
| turn input/item | 输入和 item 流转 | 验证、转换、观察 |
| token usage | usage 更新 | budget/accounting |

registry 冻结后，一个 turn 使用同一快照。动态刷新通过创建新 projection/下一轮生效，避免工具执行中 handler 被替换。

## 2. `ext/*` 逐模块

| 模块 | 实现机制 | 与 core 的边界 |
|---|---|---|
| `ext/agent` | 注册多 Agent 工具、上下文与 lifecycle | 调用 core AgentControl，不复制 thread runtime |
| `ext/connectors` | 暴露 connector catalog/mentions/tools | 凭据和网络仍由 MCP/provider 控制 |
| `ext/git-attribution` | 记录插件/脚本引起的 Git 变更来源 | 输出 attribution metadata |
| `ext/goal` | GoalService、持久状态、token budget、continuation | 通过工具和 lifecycle 驱动 |
| `ext/guardian-v2` | 自动审查 proposal、证据与反馈 | 返回 reviewer decision，不执行命令 |
| `ext/history-notes` | 从历史生成/维护 notes | 以 context contributor 注入 |
| `ext/image-generation` | 图像请求、产物和附件接入 | 输出 item/attachment，不直接渲染 UI |
| `ext/items` | 扩展 item 类型和转换 | 对接 protocol/event mapping |
| `ext/mcp` | 将 MCP runtime 接入 extension registry | 连接管理在 codex-mcp |
| `ext/memories` | memory startup/read/write 协调 | 持久化在 state/memories crates |
| `ext/queue` | 后续任务队列工具与 lifecycle | queue 数据在 thread store/state |
| `ext/skills` | catalog、selector、resource provider、render budget | 只产生指令/资源与工具贡献 |
| `ext/web-search` | web search 工具与事件 | provider/网络策略仍由 core 控制 |

## 3. MCP Runtime

[`McpRuntime`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/codex-mcp/src/runtime.rs#L96) 根据 `McpRuntimeInput` 物化“一份精确配置”的连接集合。`McpRuntimeContext` 包含环境、auth/HTTP、client capabilities、plugin config 等影响连接身份的条件。runtime 发布 `PublishedMcpRuntime`，旧 turn 可持有旧 snapshot，新配置不会突变其连接。

[`McpConnectionSet`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/codex-mcp/src/connection_manager.rs#L1) 是运行连接的发布视图。内部子模块：

- `startup` 并发启动 required/optional servers，required 失败可阻止能力就绪。
- `status` 汇总 connecting/ready/failed/disabled。
- `tool_catalog` 拉取、分页、sanitization、可见性、environment generation 和缓存。
- `resources` 列举/读取 resource，保留 resource origin。
- `binding`/`binding_clients` 把配置绑定到实际 client。
- `trusted_access` 在敏感调用前判断 server/tool 信任。
- `event_stream` 转发 MCP notifications。

`rmcp-client` 提供 local child/stdio、bounded stdio、streamable HTTP、in-process transport。OAuth 子模块处理 callback、dynamic registration、issuer binding、credential store、refresh lock/transaction；EMA 子模块处理另一套身份交换。HTTP redirect 和 `WWW-Authenticate` 单独解析，防 token 泄漏到异源。

## 4. Tool 与 Resource 路由

tool discovery 结果经过名称命名空间、schema sanitization、OpenAI file fields 处理和 model visibility 过滤，再注册到 ToolRouter。call 前 `prepare_call` 固定 server/tool binding，避免 refresh 后名称指向另一连接。resource list/read 走 `resource_client`，来源 metadata 用于上下文 attribution。

MCP server 可反向发 elicitation；`auth_elicitation`、`user_verification_elicitation` 将其转换为客户端可处理请求。无交互客户端必须明确拒绝或超时，不能无限挂起。

## 5. Plugin 系统

`plugin` crate 定义 `PluginId`、manifest/interface/provider/load outcome。`core-plugins::PluginsManager` 位于 [`manager.rs`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core-plugins/src/manager.rs#L524)，负责把 configured、bundled、local marketplace、remote installed 合并为有效插件集。

内部流程：

1. `manifest` 解析 plugin JSON 与 interface/paths。
2. `marketplace` 解析目录、fallback manifest 和 policy。
3. `remote` 拉取 catalog，`catalog_cache` 管版本缓存。
4. add/install 验证 source、git/npm/bundle，写 installed store。
5. `loader` 物化 skills、MCP servers、hooks、commands/apps。
6. `toggles` 与 managed policy 决定 enabled/disabled。
7. `script_attribution`、metrics 记录运行来源。
8. upgrade/remove 使用可恢复状态更新 cache/store。

`plugin_bundle_archive` 处理 bundle 解包，必须防 path traversal；`git_policy` 限制允许的 git source；`npm_source` 解析 npm 来源；remote bundle 与 legacy remote 分开，避免错误格式互认。

## 6. Skills

`skills` crate 的 parser 读取 `SKILL.md` front matter 与正文，loading 发现目录，model/interface 表达 descriptor/resource，mentions 解析显式 `$skill`，selection 去重并按配置启用。`ext/skills` 在此基础上加入 host/executor catalog、BM25 动态选择和 token budget render。

选择流程是：收集候选 -> 应用路径/配置/角色过滤 -> 显式 mention 优先 -> 语义/BM25 推荐 -> 在预算内渲染摘要 -> 模型需要时再读资源。这样避免数百份 skill 全量进入 system prompt。

## 7. Hooks

`hooks/declarations.rs` 定义 hook 配置，`registry.rs` 组织触发器，`events/*` 为 session start/end、user prompt、pre/post tool、permission、compact、interrupt/stop 建 typed payload。engine 的 `discovery` 找 handler，`dispatcher` 并发/串行调度，`command_runner` 和 `mcp_runner` 执行两类 handler，`output_parser` 解析结构化决策，`output_spill` 处理大输出。

hook 错误策略需要按事件区分：安全 pre-hook 失败宜 fail closed；纯通知 hook 可记录后继续。无限超时或输出会阻塞 turn，因此 runner 必须有 timeout/size limit。

## 8. Memory

Phase 1 读取符合条件的 rollout，使用 DB lease 防重复工作，并行提取候选 facts/summary；失败按 backoff 重试。Phase 2 使用全局锁同步 memory workspace，比较旧新内容，启动内部 consolidation agent。该 Agent 被限制网络和审批，产出 diff 后再写入。

memory read 根据 scope、freshness 和预算注入上下文。删除/重置必须同时处理数据库索引和 workspace 文件。最重要的安全边界是：历史中的外部文本是数据，不可直接升级为高优先级长期指令。

## 9. Goal、Queue 与 Connectors

GoalService 绑定 thread、状态、token budget 和 runtime；工具只允许合法状态迁移，完成/阻塞与普通文本消息分离。Queue 将未来工作项保存到 store，支持 add/list/update/delete/reorder/start。Connectors 将外部数据源呈现为 catalog、mentions、MCP/tool，访问仍受 auth 和网络策略。

## 10. 扩展安全清单

- 安装来源、启用状态、运行权限分别建模。
- manifest 路径 canonicalize 后必须仍位于 bundle 根。
- remote cache 带版本/完整性并可回滚。
- 扩展工具不得绕过统一 lifecycle 与审批。
- dynamic refresh 只影响新 turn 或通过显式版本切换。
- hook/MCP 输出标记来源和信任级别。
- memory/skill/plugin prompt 内容视为不可信数据。

## 11. MCP 根模块逐项补充

| 模块 | 具体职责 |
|---|---|
| `auth_elicitation` | 把认证挑战转换为可路由 elicitation。 |
| `binding` / `binding_clients` | 固定配置、server identity 与实际 client 的绑定。 |
| `catalog` | 注册内置/外部 MCP server 定义并构建 catalog。 |
| `client_capabilities` | 协商 sampling、roots、elicitation 等客户端能力。 |
| `client_tool_catalog` | 接收客户端提供的 MCP tools并做 namespace 管理。 |
| `codex_apps` | 将 Codex app 工具与文件参数映射到 MCP。 |
| `connection_manager` | 启停连接，发布 status/tool/resource snapshot。 |
| `elicitation` | 管理请求 id、等待队列、取消和响应。 |
| `event_stream` | 将 server notifications 发送到订阅者。 |
| `executor_environment_http_client` | 让 MCP HTTP 使用选定执行环境和网络策略。 |
| `mcp` | MCP server/client 公共 glue。 |
| `openai_docs_source_attribution` | 给官方文档资源附加可信来源标记。 |
| `pagination` | 拉取 tools/resources 的 cursor page。 |
| `plugin_config` | 将 plugin MCP 配置合并进有效 server config。 |
| `resource_client` / `resource_origin` | 读取资源并保留 server/source identity。 |
| `rmcp_client` | 对底层 rmcp-client 的 managed wrapper 和状态。 |
| `runtime` | 以配置 generation 构造和发布连接集合。 |
| `server` | Codex 自身作为 MCP server 时的 handler。 |
| `tool_catalog_cache` | 缓存远端工具 schema并按环境 generation 失效。 |
| `tools` | MCP tool 到 Codex tool spec/result 的转换。 |
| `trusted_access` | 计算工具/资源是否需要额外信任或审批。 |

## 12. Core Plugins 根模块逐项补充

| 模块 | 具体职责 |
|---|---|
| `agent_plugin_manifest` | 兼容 Agent 风格 plugin manifest并转内部模型。 |
| `agent_plugin_mcp_overlay` | 把 plugin MCP 配置叠加到基础 server 配置。 |
| `app_mcp_routing` | 决定 app 工具由哪个 MCP endpoint 执行。 |
| `artifact_operation` | 跟踪下载/安装等长操作及产物。 |
| `bundled_plugin_exclusions` | 根据平台/build 排除不可用 bundled plugin。 |
| `command_migration` | 迁移旧 plugin command 表示并生成提示。 |
| `discoverable` | 构建可展示/可搜索的插件条目。 |
| `error_subtype` | 细分安装、解析、网络、策略错误。 |
| `executor_hooks` | 将 plugin hooks 交给 hook engine 执行。 |
| `git_policy` | 限制 remote git source、ref 和更新行为。 |
| `http_client_selector` | 为官方/第三方来源选择受控 HTTP client。 |
| `installed_marketplaces` | 读取和维护已配置 marketplace 列表。 |
| `loaded_cache_metrics` | 记录 cache hit、age、load error。 |
| `loader` | 从 materialized plugin 加载所有 capability。 |
| `manager` | 组合配置、远端状态、policy 和 cache 的总入口。 |
| `manifest` | 解析/校验 plugin.json 与 fallback manifest。 |
| `marketplace` | 加载 catalog、解析 entry/source/interface。 |
| `marketplace_add` | 验证并安装新 marketplace。 |
| `marketplace_policy` | 应用 curated/restricted/managed policy。 |
| `marketplace_remove` | 删除配置与缓存，处理仍被引用的插件。 |
| `marketplace_upgrade` | fetch、checkout、activation 与失败回滚。 |
| `npm_source` | 解析 npm package 来源及版本。 |
| `plugin_bundle_archive` | 安全解包下载 bundle并阻止路径逃逸。 |
| `plugin_metrics*` | 采集加载/使用指标并通过 sidecar 输出。 |
| `provider` | 向 core 提供当前有效插件 snapshot。 |
| `recommended_plugin_install` | 处理推荐插件的显式安装流程。 |
| `remote*` | catalog/search/share/mutation/installed sync 和 legacy 兼容。 |
| `remote_plugin_id_resolver` | 将服务端/本地 id 解析为稳定 PluginId。 |
| `script_attribution` | 标记脚本执行属于哪个插件/version。 |
| `skill_snapshots` | 固化插件 skill 内容用于 turn 一致性。 |
| `startup_sync` | 启动时同步 remote catalog/installed 状态。 |
| `store` | 保存安装清单、版本与来源 metadata。 |
| `toggles` | 计算 enabled/disabled 的最终状态。 |
| `tool_suggest_metadata` | 为工具发现生成简短描述/索引信息。 |

`plugin_metrics_sidecar` 负责独立指标旁路，`remote_legacy` 隔离旧远端格式兼容，`test_support` 提供临时 marketplace、bundle 和 store fixture。
