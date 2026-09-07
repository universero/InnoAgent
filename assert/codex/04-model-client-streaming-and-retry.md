# 04. 模型客户端、流式传输与重试

## 1. 模块职责

模型模块把稳定 `Prompt` 转成 provider 请求，并把不稳定网络流还原为顺序化 `ResponseEvent`。它处理认证、provider 差异、SSE/WebSocket、超时、重试、usage、rate limit、模型路由和取消。

## 2. 分层

- `core/client.rs`：面向 turn 的 `ModelClientSession`。
- `codex-api`：Responses request/event schema、SSE 解码和 API 错误。
- `codex-client`：HTTP/WebSocket、retry、idle timeout、telemetry。
- `http-client`：TLS、proxy、headers 等低层传输。
- model provider/catalog：base URL、auth、capability、context window 和 truncation policy。

[ModelClientSession::stream](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/client.rs#L2027) 是核心入口。

## 3. 请求构造

请求包含 model、instructions、input、tools、parallel tool calls、reasoning、output schema、metadata 和 stream 设置。provider adapter 可改变 endpoint、headers 和认证，但不应改变 core 的 `ResponseItem` 语义。

关键对象包括 `Prompt`、`ModelClient`、`ModelClientSession`、`ModelProviderInfo`、`ResponseStream` 和 `ResponseEvent`。`ModelClient` 保存可跨 turn 复用的 provider/auth/config，`ModelClientSession` 保存连接级状态；把两者分开可以复用 WebSocket，又不让单次 sampling 状态污染整个 thread。

## 4. SSE 与 WebSocket

HTTP Responses 返回 SSE，client 逐 frame 解码。WebSocket 模式允许连接复用和连接级缓存；能力、握手或运行条件不满足时回退 HTTP/SSE。

模型请求本身只生成决策，工具副作用在本地事件处理后发生。若连接在 response 中途断开，重试可能生成不同工具调用，因此 call id 和历史提交边界很重要。

## 5. 重试边界

可重试项通常包括瞬时网络错误、断连、部分 5xx 和 rate-limit；不可重试项包括认证拒绝、无效请求和明确 policy 错误。重试带 backoff，并通过 `StreamError` 告知客户端正在恢复。

重试不是无限循环。turn cancellation、provider retry limit、context recovery 次数和 token budget 都能终止。认证恢复用 `AuthRecoveryStarted/Completed` 独立表达。

重试前必须判断当前错误发生在“尚未收到任何可提交 item”还是“已经流出部分 item”之后。前者可相对安全地重发；后者必须依赖 item id、response id 和 history 提交规则防止重复展示。工具只有在完整 tool call item 被处理后才启动，避免半截 arguments 触发副作用。

## 6. 流式 reducer

`try_run_sampling_request` 读取 stream：

- item added：建立 active item；
- delta：流式投影到 UI；
- item done：形成完整 `ResponseItem`，记录历史并可能 dispatch tool；
- rate limits：先记录，等 usage 一并发 `TokenCount`；
- completed：记录 response id、usage 和 `end_turn`；
- error：分类 retry、compact 或 terminal failure。

## 7. usage 与预算

usage 区分 input、output、cached input 等维度。Session 累积 thread/turn 使用量，并交给 Goal token budget。预算错误在 response completed 后检查，已经发生的 usage 仍先记录。

## 8. context overflow

provider 返回 context overflow 时可 compact 后重试。恢复路径必须有界：压缩后仍超限或错误不是上下文长度问题时终止。

idle timeout 与 context overflow 也不能混淆。idle timeout 表示流长时间无数据，可重连或重试；context overflow 是请求语义超限，重复原请求没有意义，必须先改变上下文。

## 9. provider 能力目录

模型目录决定 context window、auto-compact 阈值、reasoning、输入模态、tool/schema 能力、output truncation policy 和 transport。错误 metadata 会直接导致 prompt 超限、工具不可用或响应解析失败。

## 10. 测试与评价

测试覆盖 SSE frame、idle timeout、retry/backoff、WebSocket fallback、usage、model reroute 和 context errors。分层合理，但 core client 仍承载较多第一方后端语义。InnoAgent 应把 provider capability 当版本化契约，并记录每次请求实际 transport、retry 原因、prompt hash 和 response id。

建议额外建立 fault-injection matrix：连接前失败、首事件前失败、item added 后失败、tool call done 后失败、response completed 丢失。每个位置都要断言是否允许重试、是否重复 item、是否可能重复副作用。
