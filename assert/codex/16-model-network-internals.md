# 16. 模型与网络内部实现

## 1. 分层职责

| 层 | 模块 | 职责 |
|---|---|---|
| 领域调用 | `core/client.rs` | 把 Turn Prompt、tools、metadata 送入模型 session |
| API 语义 | `codex-api` | Responses/compact/images/models/search/realtime endpoint 类型 |
| 可靠传输 | `codex-client` | retry、SSE frame、idle timeout、telemetry |
| 通用 HTTP | `http-client` | TLS、custom CA、proxy、redirect、route-aware pool |
| WebSocket | `websocket-client` | 建连、frame 和连接错误 |
| Provider | `model-provider*` | auth、endpoint、headers、catalog、shared state |
| 模型目录 | `models-manager` | model info、presets、cache、overrides、协作模式 |

`core` 不直接拼 HTTP。它把 Prompt 转交 provider/client；`codex-api` 负责 wire schema；`codex-client` 决定网络重试；`http-client` 不理解 Agent turn。这种分层使 retry 不会误重放工具副作用，因为网络层只重试模型请求阶段。

## 2. Responses 请求

[`ModelClientSession::stream`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/client.rs#L2027) 接收 provider-neutral Prompt 和 model info，补充 headers、conversation metadata、reasoning、service tier、tools 与 output schema。WebSocket 可复用 session；不可用或失败时回退 HTTP/SSE。回退发生在传输边界，不改变上层 item/event 模型。

`codex-api/requests/responses.rs` 构造请求；`endpoint/responses.rs` 和 `responses_websocket.rs` 处理两种 transport；`sse/responses.rs` 把 data frames 解码为 typed events；`error.rs` 统一 HTTP/provider 错误；`safety_buffering.rs` 对需要缓冲的输出做控制。

## 3. Streaming 状态

sampling loop 必须区分：连接前失败、headers 后失败、流中断、provider error event、正常 completed、取消。只有确认安全的阶段才自动 retry。idle timeout 防止连接存在但永不产出；指数退避受次数和总时长限制；每次尝试带 telemetry，最终只向上层形成一个逻辑 turn 结果。

增量事件映射包括 output item add/delta/done、reasoning summary、function arguments delta、usage、rate limit 和 error。function arguments 必须在 item done 后按完整 JSON 校验，不能对半截 delta 执行工具。

## 4. Provider 与认证

`model-provider/provider.rs` 描述 base URL、query、headers、wire API、retry/timeout 和 auth provider；`auth.rs`/`bearer_auth_provider.rs` 提供 token；Amazon Bedrock 子模块处理 SigV4/凭据刷新、catalog、Mantle/runtime 差异和错误映射。`ollama`、`lmstudio` 提供本地服务探测与配置。

provider shared state 允许连接池、token/cache 在 sessions 间复用，但认证刷新需同步，避免并发 401 触发刷新风暴。错误输出必须脱敏 headers/token。

## 5. 模型能力目录

`models-manager` 的 `ModelInfo` 不只是显示名称，还驱动 context window、reasoning effort、summary、tool/parallel call、input modalities、service tier 等运行决策。manager 合并远端 catalog、本地 cache、内置 fallback 和用户 override。动态目录失败时使用稳定 fallback，但不能把不支持的模型伪装成支持全部功能。

`model_presets` 面向 UI 推荐，`collaboration_mode_presets` 绑定模型与协作模式，`cache` 提供离线启动。override 测试保证局部字段合并不丢默认能力。

## 6. HTTP 安全细节

`http-client` 的 route-aware client pool 按目标/代理/TLS 条件复用 client；redirect 需重新检查目标和敏感 header，避免 Authorization 被带到不同 origin。custom CA 与 platform proxy 可配置，但失败应给出诊断而非静默降级为不验证证书。network proxy 还可审计/限制实际 egress。

## 7. 失败与验证矩阵

| 场景 | 应有行为 |
|---|---|
| WebSocket 握手失败 | 在允许时回退 HTTP/SSE并记录原因 |
| SSE 半帧/非法 JSON | parser 返回协议错误，不生成工具调用 |
| 429/5xx | 依据 provider policy 有界重试 |
| 401 | 认证层刷新或明确失败，不无限重试 |
| context exceeded | 进入 compact/错误路径，不按网络错误重试 |
| usage limit | 发专用错误与状态，不掩盖为 timeout |
| idle stream | idle timeout 取消 transport |
| redirect 到异源 | 删除敏感 header并执行 route policy |
| model catalog 不可用 | 使用缓存/内置 fallback并标注来源 |

## 8. 相关 crates

`codex-api`、`codex-client`、`http-client`、`websocket-client`、`model-provider`、`model-provider-info`、`models-manager`、`backend-client`、`chatgpt`、`ollama`、`lmstudio`、`aws-auth`、`workload-identity`、`responses-api-proxy` 和 `codex-backend-openapi-models` 在本章均有实现定位；它们的逐项职责也见 crate catalog。
