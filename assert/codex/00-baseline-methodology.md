# 00. 基线与研究方法

## 固定基线

- 上游仓库：`https://github.com/openai/codex`
- 提交：`694b6319d3ad2399f6e435760a22d9b9357f0697`
- 提交时间：`2026-09-07T04:59:12Z`
- 标题：`Use app-server metadata for TUI session restoration (#43360)`
- 许可证：Apache-2.0
- 本地只读副本：`/tmp/openai-codex-study`

研究固定 commit，而不是使用不断变化的 `main`。例如核心 thread 管理以 [`thread_manager.rs`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/thread_manager.rs#L930) 为准，App Server 路由以 [`message_processor.rs`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/app-server/src/message_processor.rs#L975) 为准。

## 方法

1. 对完整仓库建立代码知识图谱，索引 Rust、TypeScript、Python 的符号、调用、导入和复杂度；索引规模约 12.1 万节点、69.8 万边。
2. 从 workspace/package 清单建立模块目录，再从入口、核心状态机、协议、工具、安全、存储、扩展和 SDK 逐层追踪。
3. 对关键结论同时检查定义、调用方、错误路径和测试；不只阅读 README。
4. 将官方文档用于确认公开契约，将源码用于确认真实行为；两者冲突时明确记录，不混为一谈。
5. 对架构优缺点使用“事实 -> 影响 -> 建议”链条，避免把推断写成事实。

## 证据等级

- **S1 源码事实**：固定 commit 的实现、类型、测试或 schema 可直接证明。
- **S2 官方语义**：OpenAI Codex 官方文档公开承诺的行为。
- **S3 设计推断**：根据调用链、依赖和失败语义作出的工程判断。
- **S4 建议**：面向 InnoAgent 的选择，不代表上游路线图。

官方参考包括 [Open Source](https://learn.chatgpt.com/docs/open-source.md)、[App Server](https://learn.chatgpt.com/docs/app-server.md)、[Sandboxing](https://learn.chatgpt.com/docs/sandboxing.md)、[Config](https://learn.chatgpt.com/docs/config-file/config-basic.md)、[AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md.md)、[MCP](https://learn.chatgpt.com/docs/extend/mcp.md)、[SDK](https://learn.chatgpt.com/docs/codex-sdk.md) 和 [Noninteractive mode](https://learn.chatgpt.com/docs/non-interactive-mode.md)。官方明确开源 CLI、SDK、App Server；IDE extension 与 Codex cloud 不在该仓库的开源范围内。

## 限制

本机没有 `cargo`，因此没有执行 Rust 编译、clippy 或测试。本文完成的是固定版本的全仓静态审计、调用图核验、测试资产盘点和文档链接校验。条件编译、平台沙箱、真实云服务、OAuth、WebRTC 和端到端网络行为仍需要对应平台与凭据做动态验证。
