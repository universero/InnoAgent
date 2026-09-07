# 09. App Server 与客户端表面

## 1. 模块职责

App Server 把进程内 core 转成可嵌入的双向 JSON-RPC 服务，使 TUI、IDE、桌面应用和 SDK 复用同一 Agent runtime。它负责协议，不重新实现推理循环。

## 2. 连接初始化

连接先执行 `initialize`，交换 client name/version/capabilities/MCP extensions。初始化前拒绝普通请求，避免客户端在能力未知时启动 thread。

stdio 传输一行一个 JSON；WebSocket 是实验能力。request id 被包装为 connection-aware identity，避免多个连接使用相同数字 id 时冲突。

核心对象包括 `MessageProcessor`、`ClientRequest`、`ServerRequest`、`ServerNotification`、`OutgoingMessageSender`、`ThreadRequestProcessor` 和 `TurnRequestProcessor`。processor 负责参数和领域编排，message processor 负责连接级路由。

## 3. 请求分发

[MessageProcessor](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/app-server/src/message_processor.rs#L975) 对 `ClientRequest` exhaustive match，分发到 thread、turn、config、environment、filesystem、skills、MCP、plugin、account、feedback 等 processor。

优点是新增 enum variant 必须处理；缺点是中央函数过大、领域耦合。固定版本该分发是明显复杂度热点。

## 4. thread API

[thread_start_inner](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/app-server/src/request_processors/thread_processor.rs#L1121) 校验 history/store、project、sandbox/permissions、workspace roots 和 environment，再调用 ThreadManager。

同域还处理 resume、fork、archive、delete、rollback、list/search/read、turn/item pagination、project metadata 和后台 terminals。

## 5. turn API

[turn_start_inner](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/app-server/src/request_processors/turn_processor.rs#L514) 校验输入、tool output 互斥、字符上限、图片 URL、cwd/environment/settings，之后调用 `start_or_steer_turn`。

响应只确认 `InProgress` turn；items 和终态通过通知流发送。这防止长 turn 占住单个 RPC response。

## 6. server-to-client request

审批、权限、elicitation 和 dynamic tool 是服务端主动请求客户端。App Server 必须维护 pending request、关联 id、超时和断连清理。普通 notification 没有 response，不能承载需要决策的交互。

这也是 App Server 与普通 REST API 的关键区别：一个 turn 内服务端随时可能反向询问客户端，因此 SDK 必须同时运行 response reader 和 request handler，不能只等待最初的 `turn/start` response。

## 7. 客户端表面

- TUI：把通知归约为 chat cells、plan、审批弹窗和 terminal view。
- exec：输出人类文本或稳定 JSONL，适合 CI。
- TypeScript SDK：启动/连接 App Server，暴露 thread/turn async event。
- Python SDK：生成协议类型和 client wrapper。
- IDE/桌面：订阅 thread，按 item id reducer 渲染。

这些表面不应重新判断 sandbox 或工具成功，它们只展示 core 决策并提交用户回答。

## 8. 兼容性

v1/v2、experimental fields、generated TypeScript/Python schema 同时存在。兼容策略应是 additive field、未知 notification 容忍、明确 deprecated window、schema fixture CI。

## 9. 背压和断连

慢客户端不能让模型 delta 无限堆积。连接层需要 bounded queue、取消或断连策略。断连时审批必须 fail-closed；thread 本身可继续持久化，客户端重连后从 store 恢复 completed items。

客户端 reducer 应以 thread/turn/item id 路由事件，而不是“当前屏幕上的活动 turn”。多 thread 订阅、后台 Agent 和 reconnect 都会让全局当前指针失效。

## 10. 测试与评价

测试覆盖 initialize gate、request routing、thread/turn lifecycle、approval response、v1/v2 mapping 和 generated schema。App Server 是可复用 harness 的关键，但中央 dispatcher 应按 domain 生成路由表，并让每个 notification 显式声明 v1/v2 映射和持久化策略。
