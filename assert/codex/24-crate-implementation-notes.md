# 24. 全 Crate 实现说明

本章是 [14-crate-catalog.md](14-crate-catalog.md) 的实现级补充。大型 crate 的内部调用链见 15 至 22 章；此处确保每个 workspace 成员都有输入、处理、输出和边界说明。源码目录统一固定在 [`codex-rs`](https://github.com/openai/codex/tree/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs)。

## 核心、领域与 Agent

- **`core`**：输入是配置、用户 `Op`、模型 stream 和工具结果；由 ThreadManager/Session/submission loop/run_turn 维护状态机；输出 `Event`、rollout 和副作用请求。它依赖 provider、store、extensions 和 sandbox，是系统组合中心而非稳定 SDK。
- **`core-api`**：提取宿主可依赖的核心接口与参数，避免直接暴露 Session 内部。变更应比 `core` 更保守，并通过 facade 转换为内部类型。
- **`protocol`**：定义 `Op`、`EventMsg`、ResponseItem、token usage、session metadata 等内部领域消息。serde 表示同时服务 channel、rollout 和部分表面映射，因此字段演进必须考虑旧历史。
- **`history`**：处理历史记录的抽取、规范化或选择，输入为 protocol items，输出为模型/显示可消费历史。它不执行工具，也不负责物理持久化。
- **`context-fragments`**：提供有类型、可排序的上下文片段及渲染接口，供 core/extension 组合 prompt。预算和信任标签应在组合阶段保留。
- **`prompts`**：打包内置提示模板并按模式读取，输出纯 prompt 内容；不负责模型调用或权限判断。
- **`features`**：定义 feature keys、stage/default 和配置结构，提供启用查询。feature gate 只决定能力是否出现，不能代替权限校验。
- **`collaboration-mode-templates`**：保存 plan/default 等协作模式模板，输入模式和变量，输出 developer instructions。
- **`agent-identity`**：定义 Agent 身份、显示和序列化标识，保证 thread/trace/消息能稳定关联同一 actor。
- **`agent-roles`**：定义 explorer/reviewer/default 等角色配置、工具约束和模型覆盖；由 spawn 解析并投影到子 Agent。
- **`agent-graph-store`**：持久化 parent-child、状态与拓扑查询，供多 Agent UI/control 使用。它不保存完整对话内容。
- **`guardian-context`**：注册并组织 Guardian 所需的上下文 sections，使安全审查获得结构化证据而非任意拼接字符串。
- **`response-debug-context`**：收集模型请求/响应诊断上下文，供调试或反馈；必须受开关、脱敏和保留策略约束。
- **`thread-manager-sample`**：演示外部宿主构建 ThreadManager、启动 thread 和消费事件，是示例集成而非生产 daemon。

## 配置、身份与模型

- **`config`**：从 system/user/profile/project/CLI/cloud layers 读取 TOML，执行 merge、alias、strict validation、managed requirements 和 permissions projection，输出带来源的有效配置。写配置通过 edit 模块保持结构，不应绕过 schema。
- **`config-schema`**：从配置类型生成 JSON Schema/文档制品，供编辑器和发布检查；生成结果必须与 `config` 类型同 commit。
- **`codex-home`**：解析 `CODEX_HOME`、默认目录与 symlink 规则，为 config、auth、history、plugin 和 state 提供统一根路径。
- **`login`**：实现 API key/ChatGPT 登录、状态和登出，向 auth manager 提供凭据，不直接发送模型请求。
- **`keyring-store`**：封装系统 keyring 的读写/删除与 fallback 错误，把秘密从普通配置文件分离。
- **`aws-auth`**：解析 AWS credential chain/signing 所需身份，服务 Bedrock provider；刷新失败作为认证错误返回。
- **`workload-identity`**：从运行环境获取工作负载身份 token，带缓存/刷新语义，供企业后端认证。
- **`model-provider-info`**：保存 provider 名称、默认 endpoint、wire API 和配置 metadata，作为 UI/config 的静态描述。
- **`model-provider`**：将 provider info、auth 和 HTTP client 组合成可调用 provider；包含 Bearer 与 Bedrock 实现、共享刷新状态和 models endpoint。
- **`models-manager`**：合并远端 catalog、本地 cache、内置 presets 和 overrides，输出模型能力快照；能力字段直接影响 context、reasoning、tools 和 UI。
- **`ollama`**：探测/调用 Ollama 本地服务并映射为统一 provider，处理本地 endpoint 与模型列表。
- **`lmstudio`**：对 LM Studio 做同类适配，保持 core 不感知其专用 API 差异。
- **`backend-client`**：封装非纯模型的 Codex 后端请求、错误和认证传播，供 cloud/config/account 等能力使用。
- **`chatgpt`**：封装 ChatGPT account/backend 特定 API 与类型，和通用 HTTP 层分离。
- **`codex-backend-openapi-models`**：保存由后端 OpenAPI 生成的数据类型；人工业务逻辑不应写入生成目录。
- **`codex-experimental-api-macros`**：标记/生成实验 API 元数据，使协议导出和 capability gate 能识别非稳定字段。

## 模型 API 与网络

- **`codex-api`**：为 responses、compact、images、memories、models、search、session、realtime endpoint 构造 typed request并解析 response/SSE；把 HTTP 错误映射为 API 错误。
- **`codex-client`**：在 API 之下实现 retry policy、SSE framing、idle timeout 和 telemetry；不理解 thread 持久化或工具执行。
- **`http-client`**：构造 reqwest/rustls client，处理 custom CA、系统代理、route-aware pool、redirect 和 TLS fallback。跨 origin redirect 需保护凭据。
- **`websocket-client`**：提供 WebSocket 握手、帧收发、关闭和错误抽象，供 Responses/App Server 等长连接使用。
- **`responses-api-proxy`**：代理 Responses 请求并保留流式语义，适合调试/环境转发；npm 子包负责其分发入口。

## CLI、App Server 与协议

- **`cli`**：解析命令与 override，选择 TUI、exec、app-server、MCP、cloud、plugin 或 sandbox helper。它不应复制配置合并和安全策略。
- **`tui`**：将键盘/粘贴/命令转换为 AppEvent 和 thread 请求，将 server/core events 转为 history cells、streaming markdown、diff、approval 和状态视图。内部维护多 thread UI 状态，但执行权限留在 core。
- **`exec`**：构造单次/恢复 session，提交 turn，分别以 human 或 JSONL processor 消费通知；负责退出码和最终输出文件。
- **`arg0`**：读取进程名并选择内部 helper 入口，使一个发布 binary 可承担多个受控子程序。
- **`app-server`**：维护连接初始化状态和 request context，按 ClientRequest 分派 domain processors，把 core events 映射为 notifications，并管理 server->client 请求。
- **`app-server-client`**：提供 typed request handle、notification stream 和生命周期，供 TUI/宿主连接 server。
- **`app-server-daemon`**：管理长期运行 server、发现/连接和退出，避免每个客户端重复初始化重资源。
- **`app-server-transport`**：实现 stdio JSONL、WebSocket/UDS 等字节传输，不处理 thread 业务。
- **`app-server-protocol`**：定义 v1/v2 RPC 类型、method 映射、serialization scope、schema export 和 history projection。
- **`app-server-protocol-noop-macros`**：在不生成 schema/导出代码的构建场景提供同名空宏，降低 feature 组合复杂度。
- **`app-server-test-client`**：封装测试 server 启动、initialize、send/read/wait，供 suite 断言真实 RPC 时序。
- **`app-server/tests/common`**：共享 mock server、fixture、timeout 和 test harness，只供集成测试。
- **`core/tests/common`**：提供模型 mock、session builder、event matcher 和测试依赖，避免 core integration tests 重复搭建环境。
- **`stdio-to-uds`**：把 stdin/stdout framing 桥接到 Unix socket，用于本地进程拓扑转换。
- **`uds`**：封装 socket 路径、连接和平台错误，是 transport primitive。
- **`terminal-detection`**：检测 TTY/终端特性，决定颜色、交互和输出模式，不参与业务状态。

## 工具与执行

- **`tools`**：承载可复用工具规格、结果和基础 helper，供 core/extension 注册；实际 turn routing 在 core。
- **`apply-patch`**：解析 patch grammar、验证 hunk/path并执行文件变更，输出结构化成功/失败；core handler 再叠加审批和事件。
- **`shell-command`**：解析 shell 命令、复合 segment 和展示形式，为 exec policy 提供比字符串前缀更可靠的输入。
- **`execpolicy`**：加载/执行 Starlark policy并返回 Allow/Prompt/Forbidden；它只做规则判断，不启动进程。
- **`exec-server-protocol`**：定义环境、进程、文件、stream、accepted/completed 等远端执行消息。
- **`exec-server`**：实现 client/server、environment registry、local/remote process/FS、Noise relay、capability discovery、recovery 和 telemetry。
- **`exec-server/tests/support`**：提供 server/client fixture、证书或进程 harness，供跨 crate integration tests。
- **`code-mode-protocol`**：定义 host/runtime 之间的 worker、tool delegate 和结果消息。
- **`code-mode-runtime`**：在受控 JS/V8 runtime 中执行模型代码，向 host 请求允许的工具而非直接访问宿主。
- **`code-mode-host`**：启动/管理 runtime 进程或 gRPC session，执行资源和退出控制。
- **`code-mode`**：core 侧 broker，按 turn 创建 worker、转发工具并关联取消。
- **`v8-poc`**：验证 V8 嵌入、构建和运行假设，是 code mode 的实验性支撑。
- **`file-search`**：按 query/root 生成文件候选，供 TUI mention 与工具使用；结果仍需路径权限过滤。
- **`file-system`**：定义本地/远端统一文件操作接口与 metadata，供 App Server/exec server 使用。
- **`file-watcher`**：注册路径并发布变化，用于 config/skills/plugin refresh；负责去抖和取消订阅。

## 沙箱与安全

- **`sandboxing`**：把抽象权限转为平台 launch spec，统一 denial/violation 和 terminal query；任何无法满足的安全约束应失败关闭。
- **`linux-sandbox`**：Linux helper 进程应用 namespace、Landlock/seccomp/bwrap 相关限制后 exec 目标命令。
- **`bwrap`**：定位/封装 bubblewrap（含 vendored 构建），为 Linux mount/network 隔离提供基础。
- **`mxc-sandbox`**：macOS sandbox/MXC 相关系统调用和 profile 辅助，供高层 manager 使用。
- **`windows-sandbox-rs`**：构造 restricted token、ACL/filesystem/network 规则和 Windows 进程环境。
- **`windows-sandbox-service`**：以服务边界执行需要 elevated setup 的 Windows sandbox 操作，客户端不直接持有长期高权限。
- **`process-hardening`**：设置平台进程安全属性，减少调试、继承或动态加载攻击面。
- **`network-proxy`**：按 execution 注册 egress policy，转发 HTTP并通过 credential broker 注入受控凭据和审计决策。
- **`shell-escalation`**：识别/处理需要更高权限的 shell 执行路径，必须与 approval policy 结合。
- **`secrets`**：扫描或封装敏感值，支持日志/输出脱敏；不是完整凭据库。

## Extension、MCP、Plugin 与 Skill

- **`ext/extension-api`**：定义各类 contributor trait、输入/输出和 immutable registry；启动时注册，turn 中只读。
- **`ext/agent`**：贡献 spawn/send/wait/interrupt 等多 Agent 工具及上下文，委托 core AgentControl。
- **`ext/connectors`**：把 connector catalog 和能力加入上下文/工具，实际认证与网络调用由下层负责。
- **`ext/git-attribution`**：把工具/脚本来源附加到 Git 变更或事件，支持审计。
- **`ext/goal`**：实现 GoalService、持久状态、token accounting、create/update 工具和 continuation；限制特殊 subagent 修改顶层目标。
- **`ext/guardian-v2`**：构造审查上下文、调用 reviewer 并生成结构化安全决策/反馈。
- **`ext/history-notes`**：从 thread history 维护短注记并作为 context contribution，避免直接修改原始历史。
- **`ext/image-generation`**：注册图像生成工具、处理产物引用并写 attachment/item。
- **`ext/items`**：定义扩展 item 的转换和 lifecycle，使非核心 item 能进入统一事件流。
- **`ext/mcp`**：把 MCP runtime、tools、resources 与 lifecycle 注入 core extension registry。
- **`ext/memories`**：协调 memory read/startup/write task 与上下文注入。
- **`ext/queue`**：提供持久队列工具和 thread lifecycle，调用 state/thread-store 保存排序。
- **`ext/skills`**：构建 host/executor catalog、BM25 选择、按预算渲染和资源读取。
- **`ext/web-search`**：注册 web search 工具、请求与结果事件，并服从 provider/network policy。
- **`codex-mcp`**：根据精确配置创建发布型 runtime，管理多 server 连接、catalog、resource、elicitation、client tools 和可信访问。
- **`rmcp-client`**：实现 MCP stdio/HTTP/in-process transport、bounded I/O、OAuth/EMA、redirect、refresh lock 和测试 server。
- **`connectors`**：定义连接器 metadata、配置与共享客户端模型，供 ext 与 App Server 展示。
- **`skills`**：解析 SKILL.md、发现目录、解析 mention、选择/去重并生成 skill interface。
- **`plugin`**：定义稳定 PluginId、manifest、interface、provider 和 load outcome。
- **`core-plugins`**：发现 marketplace、安装/升级/移除、缓存 remote bundle、应用 policy、加载 skill/MCP/hook/app 并记录 attribution。
- **`hooks`**：定义生命周期事件 schema，发现并调度 command/MCP handlers，解析 allow/block/context 输出并处理大输出 spill。
- **`memories/read`**：查询、筛选和格式化长期记忆，在预算内注入 turn context。
- **`memories/write`**：从 rollout 提取候选，使用 lease/backoff 和全局锁运行 consolidation，再更新 memory workspace/DB。

## 存储

- **`thread-store`**：以 trait 提供 thread 生命周期、历史、分页、项目、section 和搜索；local 组合 rollout/SQLite，in-memory 服务测试。
- **`rollout`**：维护 JSONL recorder、metadata、文件名/ordinal、seek/reverse scan、index、migration、compression 和 DB 投影。
- **`rollout-trace`**：记录独立调试 trace bundle并通过 reducer 还原执行视图；不可当作用户历史主存储。
- **`state`**：管理 SQLite schema/migration 和 thread/project/queue/goal/memory/artifact/log/graph 运行 API，并提供 recovery/backfill。
- **`message-history`**：向用户输入历史文件做单次 append，读取供 shell-style history；不保存完整 tool trajectory。
- **`attachment-store`**：保存图片/文件 blob 与引用 metadata，提供读取和清理，降低 event payload 体积。

## Cloud、迁移与实时

- **`cloud-config`**：获取/缓存云端配置 bundle并转为 config layers，受 managed requirements 与来源追踪控制。
- **`cloud-tasks-client`**：实现云任务 API 请求、认证和错误映射。
- **`cloud-tasks-mock-client`**：提供内存/fixture 行为，验证上层不依赖真实服务。
- **`cloud-tasks`**：提供 task 创建、列表/差异 UI 与环境探测，调用 client 而不实现云服务端。
- **`worktree`**：创建/管理 Git worktree 和分支，向 task 提供隔离 cwd；不提供 OS 安全隔离。
- **`external-agent-migration`**：探测其他 Agent 的 session/config，转换受支持字段并记录导入结果。
- **`realtime-webrtc`**：管理 WebRTC peer、media/data channel、session 信令和关闭状态。
- **`voice-host`**：连接音频设备、realtime session 与宿主事件，处理开始/停止和媒体流。

## 可观测性与工程

- **`analytics`**：定义结构化产品/使用事件与发送接口，调用方负责只传允许字段。
- **`otel`**：初始化 tracer/meter、传播 context、记录 model/tool/turn 指标和 exporter。
- **`otel-trace-websocket`**：将 trace 事件通过 WebSocket 输出给诊断消费者，处理断连和队列。
- **`diagnostics`**：维护 gauge/health snapshot 和诊断查询，不保存完整业务状态。
- **`feedback`**：收集用户说明和经筛选的日志/config/thread 资料，构建可上传报告。
- **`build-info`**：暴露版本、commit、target/profile 等编译期信息，用于 handshake/诊断。
- **`install-context`**：识别安装渠道与更新提示所需上下文。
- **`test-binary-support`**：在测试中定位 workspace binaries、设置环境并启动子进程。
- **`async-utils`**：提供取消、channel/task 等通用异步辅助，避免业务 crate 重复实现。
- **`ansi-escape`**：解析/移除 ANSI 控制序列，保护日志和非终端输出格式。
- **`git-utils`**：封装 Git 查询、diff、root/branch 等通用操作。

## Utilities

- **`utils/absolute-path`**：以 newtype 保证构造后的路径为绝对路径，减少函数间重复检查。
- **`utils/approval-presets`**：提供安全模式到 approval/permission 组合的映射，最终 enforcement 仍在 core/sandbox。
- **`utils/audio`**：音频格式、缓冲和设备相关辅助。
- **`utils/cache`**：提供带版本/过期语义的缓存基础件，不作为事实存储。
- **`utils/cargo-bin`**：从 Cargo 环境或构建目录解析测试/辅助 binary。
- **`utils/cli`**：共享 clap、输出或错误展示辅助。
- **`utils/elapsed`**：统一 duration 测量和人类可读格式。
- **`utils/fuzzy-match`**：计算候选匹配分数，调用方负责权限与业务过滤。
- **`utils/git-discovery`**：从 cwd 向上发现 repo 和 marker，处理非 Git 目录。
- **`utils/home-dir`**：跨平台解析 home，失败显式返回而非拼接空路径。
- **`utils/image`**：读取、识别、缩放和编码图像，供输入/附件/生成能力使用。
- **`utils/json-to-toml`**：将结构化 JSON 值转换为 TOML 表示，供 config edit/API 使用。
- **`utils/oss`**：隔离开源构建可用性差异，使闭源/服务能力有明确 fallback。
- **`utils/output-truncation`**：按字节、行和方向截断工具输出并标记省略，防 context 爆炸。
- **`utils/path-uri`**：在本地/远端 path 与 URI 间转换，保留平台语义。
- **`utils/path-utils`**：提供 canonicalization、ancestor 等函数；安全调用方仍需防 TOCTOU。
- **`utils/plugins`**：共享 plugin namespace/id 辅助，避免 core/plugin 循环依赖。
- **`utils/pty`**：创建 PTY、resize、异步读写和退出监测，供 unified exec。
- **`utils/readiness`**：用一次性/广播状态协调异步组件启动完成或失败。
- **`utils/redacted-string`**：让 Debug/Display 默认隐藏敏感内容，显式方法才可取原值。
- **`utils/rustls-provider`**：统一初始化 rustls crypto provider，防多个 crate 重复安装。
- **`utils/sandbox-summary`**：把权限配置转换为可展示摘要，不参与 enforcement。
- **`utils/sleep-inhibitor`**：长 turn 期间申请系统不休眠句柄，Drop 时释放。
- **`utils/stream-parser`**：处理跨 chunk 的行/frame 边界，为 SSE/JSONL 等上层解析提供原语。
- **`utils/string`**：集中字符串裁剪、规范化等无状态函数。
- **`utils/template`**：对受控模板做变量替换，未知变量/非法模板返回错误。

## 非 Rust Package

- **`codex-cli`**：npm 安装脚本按 OS/arch 选择已签名 binary 并透传 argv/signal；不包含 Agent runtime。
- **`sdk/typescript`**：以 `codex exec --json` 为 transport，提供 Thread/Event 类型和 async generator。
- **`sdk/python`**：启动/连接 App Server，提供 typed RPC、notification routing、thread/turn/goal API。
- **`sdk/python-runtime`**：把版本锁定的 CLI binary 作为 Python distribution 依赖交付。
- **`responses-api-proxy/npm`**：分发 Responses proxy 的 Node 入口/平台制品。
- **`.devcontainer/codex-install`**：为开发容器自动安装 Codex 与依赖。
- **`scripts`**：生成 schema/SDK artifacts、检查 workspace、打包、签名、发布和 smoke test。

## 边界结论

所有 crate 都能归入协议、运行时、能力、安全、状态、表面或基础设施之一。判断一个新模块是否合理的标准不是“能否拆成 crate”，而是它是否拥有独立不变量、可替换实现或需要阻断依赖方向；否则拆分只会增加 workspace 和发布成本。
