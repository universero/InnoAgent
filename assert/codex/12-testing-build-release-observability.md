# 12. 测试、构建、发布与可观测性

## 测试资产

静态盘点发现约 1300 个 Rust 测试源文件/测试目录文件、115 个 snapshot、51 个 Python 测试文件和 9 个 TypeScript 测试文件。重点不是数量，而是测试层级：

- core 单元/集成测试覆盖 turn、tools、config、rollout、compaction、approval 和 provider mock。
- App Server suite 按 v2 方法覆盖 request/notification、错误、分页、plugin、MCP 和 thread 生命周期。
- sandbox/exec policy 使用平台条件测试与命令解析样例。
- protocol schema、snapshot 和 SDK generated artifacts 防止跨语言漂移。
- mock model server、mock cloud client、test app server 支撑确定性端到端测试。

snapshot 很适合事件/schema/UI，但应审查语义差异，不能无脑更新。安全策略测试需覆盖拒绝路径、channel 断开、组合命令和路径逃逸。

## 构建

Rust workspace 使用 Cargo，同时提供 Bazel 文件；JavaScript 使用 pnpm/npm；Python 使用 pyproject。多个平台二进制、sandbox helper、npm wrapper 和 Python runtime 需要协调版本。条件编译覆盖 macOS/Linux/Windows、vendored bubblewrap、V8/code mode、WebRTC 等能力，CI 矩阵成本显著。

## 发布

发布链至少包含 Rust binaries、平台 npm artifacts、TypeScript SDK、Python SDK/runtime、generated protocol/schema。版本不一致可能表现为 unknown method、字段缺失或 binary 不支持 flag，而不是编译错误。应在发布前执行 schema regeneration clean check、跨 SDK smoke test、binary version handshake 和安装测试。

## 可观测性

`otel`、`otel-trace-websocket`、`analytics`、`diagnostics`、`feedback`、`response-debug-context` 分别覆盖 trace/metric、实时导出、产品事件、运行诊断、用户反馈和模型响应调试。工具 registry 为每次调用建立 span，并记录 lifecycle terminal outcome；模型 client 记录请求、retry、stream timeout 和 token usage。

可观测数据可能含 prompt、路径、命令和 tool output，必须有 redaction、采样和用户配置边界。`secrets` 与 redacted-string 只是基础设施，真正安全取决于所有事件 producer 是否正确使用。

## 本次验证状态

本研究校验了文件结构、固定 commit、关键符号/调用图、引用路径和文档完整性。由于环境缺少 `cargo`，未运行上游 build/test；因此不能声称平台沙箱或端到端协议已在本机动态通过。
