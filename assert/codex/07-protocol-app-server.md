# 07. 协议与 App Server

## 协议职责

`app-server-protocol` 定义 JSON-RPC 请求、响应、通知、v1/v2 类型和 schema；noop macros 支持不启用生成时编译；协议类型再映射到 `core` 的 `Op/Event`。官方契约是双向 JSON-RPC 2.0，stdio 使用一行一个 JSON，WebSocket 为实验能力。

连接先 `initialize`，协商 client name/version/capabilities/MCP extensions，之后才允许普通请求。请求 id 被包装为 connection-aware id，避免多连接复用 server 时冲突。

## 中央分发

[`MessageProcessor::handle_initialized_client_request`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/app-server/src/message_processor.rs#L975) 从 `ClientRequest` enum 分派到 config、environment、filesystem、thread、turn、project、skills、MCP、plugin、account、feedback 等 processor。该函数在基线中约 739 行，图分析 cyclomatic complexity 165、cognitive complexity 329，是明确的扩展热点。

优点是 enum exhaustive match 让新增协议方法必须被处理；缺点是所有域共享一个中央编译点。后续可按 domain 使用 generated dispatcher，同时保留类型穷尽性。

## Thread API

[`thread_start_inner`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/app-server/src/request_processors/thread_processor.rs#L1121) 校验 history mode 与 store 能力、project、sandbox/permissions、workspace roots 和 environments，再调用 `ThreadManager`。同一 processor 还处理 resume/fork/archive/delete/unarchive/revert/rollback/list/search/read、分页 turns/items、section/project metadata 和后台 terminals。

resume 从 rollout 重建 session；fork 从历史分叉但生成新 thread identity；archive 是可恢复状态，delete 是破坏性操作；rollback/revert 对历史和工作区影响不同，客户端不能将它们当同义词。

## Turn API

[`turn_start_inner`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/app-server/src/request_processors/turn_processor.rs#L514) 做直接输入许可、tool output 互斥、字符上限、图片 URL、cwd/environment/settings 校验，然后调用 `start_or_steer_turn`。响应只返回 `InProgress` turn，后续 items/status 通过通知流发送。另有 steer、interrupt、review、realtime 和 settings update。

## 事件与背压

Server 把 core events 映射为 protocol notifications，并按 connection/thread subscription 投递。审批和 elicitation 是 server->client request，需要响应关联和超时/断连处理。慢客户端必须通过 bounded queue、取消或断连限制，不能无限积压模型 token。

## 兼容性

协议同时承担 v1/v2、experimental fields、generated TypeScript/Python schema。兼容策略应遵循 additive fields、unknown notification 容忍、明确 deprecated window 和 schema CI。Python SDK 的 generated notification registry 证明协议代码生成已成为发布链的一部分。
