# 14. Crate 与 Package 目录

本目录按职责覆盖基线中 `codex-rs` 的 workspace/root Cargo manifest、成员 crate、测试支持 crate 和非 Rust package。名称后的说明是静态职责摘要，不代表稳定公共 API。

## 核心运行时与领域

| Crate | 职责 |
|---|---|
| `core` | ThreadManager、Session、turn/model/tool 主编排 |
| `core-api` | 较稳定的核心公共 API 类型 |
| `protocol` | 内部 Op/Event/Item 等领域协议 |
| `history` | 历史项处理与转换 |
| `context-fragments` | 可组合上下文片段 |
| `features` | feature flag 定义与解析 |
| `prompts` | 内置 prompt 资源与加载 |
| `collaboration-mode-templates` | 协作模式模板 |
| `config` | 分层配置加载、合并与运行时快照 |
| `config-schema` | 配置 schema 与生成支持 |
| `codex-home` | Codex home 路径与目录约定 |
| `agent-identity` | Agent 身份模型 |
| `agent-roles` | Agent 角色与限制 |
| `guardian-context` | Guardian 审查上下文 section |
| `response-debug-context` | Responses 调试上下文 |

## 表面、协议与传输

| Crate | 职责 |
|---|---|
| `cli` | 顶层命令解析和 arg0 分派 |
| `tui` | 终端交互应用 |
| `exec` | 非交互执行与 JSONL 输出 |
| `app-server` | JSON-RPC 服务与业务 processors |
| `app-server-client` | App Server 客户端 |
| `app-server-daemon` | 长驻 daemon 管理 |
| `app-server-protocol` | JSON-RPC v1/v2 类型与 schema |
| `app-server-protocol-noop-macros` | 协议宏禁用/兼容支持 |
| `app-server-transport` | stdio/WebSocket/UDS transport |
| `app-server-test-client` | App Server 测试客户端 |
| `app-server/tests/common` | App Server integration support |
| `core/tests/common` | Core integration test support |
| `arg0` | 按可执行名多路复用入口 |
| `stdio-to-uds` | stdio 与 Unix socket 桥接 |
| `uds` | Unix domain socket 工具 |
| `terminal-detection` | 终端能力检测 |

## 模型、API 与网络

| Crate | 职责 |
|---|---|
| `codex-api` | Responses API 类型、builder、SSE 解析 |
| `codex-client` | 模型 transport、retry、stream timeout |
| `http-client` | 通用 HTTP/TLS/proxy 客户端 |
| `websocket-client` | WebSocket transport |
| `responses-api-proxy` | Responses API 代理 |
| `backend-client` | Codex/ChatGPT 后端客户端 |
| `chatgpt` | ChatGPT 相关 API 与认证交互 |
| `codex-backend-openapi-models` | 后端 OpenAPI 生成模型 |
| `model-provider-info` | provider 静态信息 |
| `model-provider` | provider client 构造与抽象 |
| `models-manager` | 模型目录、能力与选择 |
| `ollama` | Ollama 本地 provider |
| `lmstudio` | LM Studio 本地 provider |
| `aws-auth` | AWS 认证支持 |
| `workload-identity` | workload identity |
| `codex-experimental-api-macros` | 实验 API 标记/生成宏 |

## 工具与代码执行

| Crate | 职责 |
|---|---|
| `tools` | 通用工具模型/共享实现 |
| `apply-patch` | patch 解析与安全应用 |
| `shell-command` | shell 命令解析/表示 |
| `execpolicy` | Starlark exec policy 引擎 |
| `exec-server` | 本地/远端命令执行服务 |
| `exec-server-protocol` | exec server wire protocol |
| `exec-server/tests/support` | exec server 测试支持 |
| `code-mode` | code mode broker/session |
| `code-mode-protocol` | code mode 协议 |
| `code-mode-runtime` | 代码执行 runtime |
| `code-mode-host` | code mode host |
| `v8-poc` | V8 集成实验 |
| `file-search` | 文件搜索能力 |
| `file-system` | 文件系统工具/抽象 |
| `file-watcher` | 文件变化监听 |

## 安全与隔离

| Crate | 职责 |
|---|---|
| `sandboxing` | 跨平台 sandbox policy 与 manager |
| `linux-sandbox` | Linux sandbox 入口/实现 |
| `windows-sandbox-rs` | Windows restricted sandbox |
| `windows-sandbox-service` | Windows sandbox service |
| `mxc-sandbox` | macOS sandbox 相关封装 |
| `bwrap` | bubblewrap 集成 |
| `process-hardening` | 进程加固 |
| `network-proxy` | 网络限制、代理与凭据 broker |
| `shell-escalation` | shell 权限升级路径 |
| `secrets` | secret 检测/处理 |
| `keyring-store` | 系统 keyring 凭据存储 |
| `login` | 登录与认证流程 |

## 扩展、MCP、Skills 与 Plugins

| Crate | 职责 |
|---|---|
| `ext/extension-api` | typed contributor API/registry |
| `ext/agent` | Agent 扩展贡献 |
| `ext/connectors` | connector 扩展 |
| `ext/git-attribution` | Git/script attribution |
| `ext/goal` | Goal 状态、预算和工具 |
| `ext/guardian-v2` | Guardian v2 审查扩展 |
| `ext/history-notes` | 历史注记 |
| `ext/image-generation` | 图像生成扩展 |
| `ext/items` | item 扩展处理 |
| `ext/mcp` | MCP extension contribution |
| `ext/memories` | memory extension integration |
| `ext/queue` | thread queue 扩展 |
| `ext/skills` | 新 skills catalog/selector/runtime |
| `ext/web-search` | Web 搜索扩展 |
| `codex-mcp` | MCP runtime/catalog/connection set |
| `rmcp-client` | MCP stdio/HTTP/OAuth client |
| `connectors` | connector 基础模型/客户端 |
| `skills` | skill 解析、资源与样例 |
| `plugin` | plugin 公共模型 |
| `core-plugins` | plugin manager/marketplace/install/load |
| `hooks` | 生命周期 hook 与 handlers |
| `memories/read` | memory 读取与注入 |
| `memories/write` | memory 提取与 consolidation |

## 状态、历史与图

| Crate | 职责 |
|---|---|
| `thread-store` | storage-neutral thread store |
| `rollout` | JSONL rollout 读写/发现 |
| `rollout-trace` | 调试 trace bundle/reducer |
| `state` | SQLite 状态与 metadata 投影 |
| `message-history` | 用户输入历史 JSONL |
| `attachment-store` | 附件持久化 |
| `agent-graph-store` | 多 Agent 图状态 |

## Cloud、环境、迁移与实时

| Crate | 职责 |
|---|---|
| `cloud-config` | 云端/受管配置加载 |
| `cloud-tasks` | 云任务领域层 |
| `cloud-tasks-client` | 云任务 API client |
| `cloud-tasks-mock-client` | 云任务 mock |
| `worktree` | Git worktree 管理 |
| `external-agent-migration` | 外部 Agent 配置迁移 |
| `realtime-webrtc` | WebRTC realtime |
| `voice-host` | 语音 host |
| `thread-manager-sample` | ThreadManager 使用样例 |

## 可观测、反馈与工程支持

| Crate | 职责 |
|---|---|
| `analytics` | 产品/运行分析事件 |
| `otel` | OpenTelemetry traces/metrics |
| `otel-trace-websocket` | OTel trace WebSocket 输出 |
| `diagnostics` | 诊断指标/状态 |
| `feedback` | 用户反馈收集 |
| `build-info` | 构建版本信息 |
| `install-context` | 安装来源/上下文 |
| `test-binary-support` | 二进制测试支持 |
| `async-utils` | 异步通用组件 |
| `ansi-escape` | ANSI escape 处理 |
| `git-utils` | Git 通用能力 |

## Utilities

| Crate | 职责 |
|---|---|
| `utils/absolute-path` | 绝对路径类型 |
| `utils/approval-presets` | 审批预设 |
| `utils/audio` | 音频工具 |
| `utils/cache` | 缓存原语 |
| `utils/cargo-bin` | Cargo binary 定位 |
| `utils/cli` | CLI 通用辅助 |
| `utils/elapsed` | 耗时格式化/统计 |
| `utils/fuzzy-match` | 模糊匹配 |
| `utils/git-discovery` | Git 仓库发现 |
| `utils/home-dir` | home 目录解析 |
| `utils/image` | 图片处理 |
| `utils/json-to-toml` | JSON/TOML 转换 |
| `utils/oss` | OSS 构建差异辅助 |
| `utils/output-truncation` | 输出截断 |
| `utils/path-uri` | path/URI 转换 |
| `utils/path-utils` | 路径通用函数 |
| `utils/plugins` | plugin utility types |
| `utils/pty` | PTY 封装 |
| `utils/readiness` | readiness 协调 |
| `utils/redacted-string` | 日志脱敏字符串 |
| `utils/rustls-provider` | rustls provider 初始化 |
| `utils/sandbox-summary` | sandbox 摘要 |
| `utils/sleep-inhibitor` | 防系统休眠 |
| `utils/stream-parser` | 流解析工具 |
| `utils/string` | 字符串辅助 |
| `utils/template` | 模板渲染 |

## 非 Rust Packages 与构建根

| Package/Root | 职责 |
|---|---|
| `codex-rs/Cargo.toml` | Rust workspace 根与统一依赖 |
| `codex-cli` | npm CLI 包装和平台 binary 分发 |
| `sdk/typescript` | `@openai/codex-sdk`，exec JSONL adapter |
| `sdk/python` | `openai-codex`，App Server RPC client |
| `sdk/python-runtime` | Python 发布绑定的 pinned runtime |
| `responses-api-proxy/npm` | Responses proxy 的 npm 包装 |
| `.devcontainer/codex-install` | devcontainer 安装 package |
| `scripts` | Python/Node 发布、schema、CI 工具 |
| repository root `package.json` | pnpm workspace 与开发命令 |

## 目录核验说明

上表包含基线下发现的全部业务 crate、utility crate、测试 support crate和 package。`vendor/bubblewrap` 等 vendored 第三方源码不作为 Codex 自研模块逐项解释；其集成责任归入 `bwrap`/`linux-sandbox`。
