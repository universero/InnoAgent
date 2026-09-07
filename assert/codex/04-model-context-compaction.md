# 04. 模型、上下文与压缩

## 模型传输

[`ModelClientSession::stream`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/client.rs#L2027) 是核心流式入口。wire 语义统一为 Responses API；优先复用 WebSocket，在握手或运行条件不满足时回退 HTTP/SSE。`codex-api` 定义 Responses 请求/事件和 SSE 错误映射，`codex-client` 提供 retry、SSE framing、idle timeout、telemetry，`http-client` 提供更低层 TLS/proxy transport。

这种分层把“模型协议”“可靠传输”“通用 HTTP”分开。provider 配置可改变 base URL、headers、auth、retry 与模型能力，但核心 turn 不需要为每个厂商重写状态机。本地 `ollama`/`lmstudio` 通过 provider adapter 接入。

## Prompt 组装

[`build_prompt`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/turn.rs#L1387) 将历史 items、当前输入、instructions、环境、skills、工具规格和 output schema 汇总。模型只能看到 `ToolRouter::model_visible_specs()` 返回的工具；内部工具、受 feature 控制工具和不适用于当前 subagent 的工具不会泄露。请求允许 `parallel_tool_calls: true`，但最终并发能力仍受 handler/runtime 控制。

上下文来源包括 system/developer/user 消息、AGENTS.md 层级指令、工作目录/仓库信息、skills、MCP resources、历史摘要和上一轮 tool output。`context-fragments` 用于结构化片段，`prompts` 与 collaboration-mode templates 提供可版本化模板。

## 压缩

压缩既可由显式 `compact` 操作触发，也可在接近上下文上限时触发。核心目标不是简单截断，而是把历史转换成可继续执行的摘要/状态，再写回 rollout。压缩前后仍要保留 thread/turn 边界、关键工具副作用和用户约束。

上下文超限必须区分三类：本地估算已超限、provider 返回 context error、模型流中途失败。自动 compact 只能在策略允许且输入仍可恢复时进行；否则应发明确错误，避免无限“压缩 -> 重试”。

## Usage 与能力

模型目录和 manager 管理 context window、reasoning、summary、tool 能力等 metadata。usage 事件区分 input/output/cache tokens，并服务于 UI、analytics 和 Goal token budget。TypeScript SDK 对旧事件缺少 `cache_write_input_tokens` 时补零，说明跨版本事件兼容是实际负担。

## 评价

统一 Responses 语义降低了 provider 分叉，但“能力 metadata 正确性”成为关键依赖。InnoAgent 应让 context assembly 产出可检查的中间对象，并对 token 预算、工具可见性、压缩前后不变量做 snapshot tests。
