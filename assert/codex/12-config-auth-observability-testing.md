# 12. 配置、认证、可观测性与测试

## 1. 模块职责

这些能力不直接产生 Agent 行为，却决定行为是否可预测、可审计、可发布。配置选择策略，认证建立 provider 身份，模型目录声明能力，可观测性解释运行，测试守住跨模块不变量。

## 2. 配置分层

配置来自默认值、用户 `config.toml`、profile、项目配置、CLI 参数、App Server overrides 和受管策略。合并要区分 scalar replace、map merge、list 策略、显式 null、managed constraints 和 thread/turn 临时 override。

最终 Session 保存 resolved config 和来源，不能在运行中到处重新读文件。

核心对象包括 `Config`、config layers、managed constraints、`AuthManager`、`ModelProviderInfo`、`ModelInfo`、trace/analytics clients 和 schema fixtures。配置决定策略，provider/model info 声明外部能力，observability 只观察而不改变结果。

实现入口：[Config](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/config/mod.rs)、[AuthManager](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/login/src/auth/manager.rs)、[ModelsManager](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/models-manager/src/manager.rs)。

## 3. 热更新

`ReloadUserConfig` 更新可热替换项；thread settings 影响未来 turn；turn settings 只影响指定活动 turn；StepContext 冻结单次 sampling。需要重启 transport、MCP server 或 sandbox backend 的项不能伪装成即时生效。

## 4. 认证

认证支持 ChatGPT 登录、API key、provider headers 和本地 provider。auth manager 管 token 获取、刷新和共享，model client 只请求可用凭据。

日志不得包含 bearer token。认证失败与网络失败分开；provider-owned recovery 用独立 lifecycle，避免无限 retry。

## 5. 模型目录

模型条目声明 slug、context window、reasoning、summary、modalities、tool 支持和 truncation policy。catalog 可按 etag 刷新，但一个 StepContext 固定所用能力。

## 6. 可观测性

- `EventMsg`：产品可见状态。
- rollout：可恢复语义。
- tracing/OTel：内部耗时和错误。
- analytics facts：采样、工具和功能使用。
- token/rate-limit events：资源状态。
- prompt/compaction trace：上下文诊断。

同一调用用 thread id、turn id、item id、call id、response id 关联。没有 correlation ids 就无法还原失败经过哪些层。

metrics 还应区分 sampling time、tool blocking time、approval waiting time 和 compaction time。只统计 turn 总耗时会把用户等待误判为模型慢，把工具慢误判为网络慢。

## 7. 隐私与裁剪

遥测不能默认上传完整 prompt、源码和工具输出。错误消息也要截断，避免 provider body 或命令输出进入日志。ZDR 要求请求可由客户端无状态重建，不能暗中依赖 server 保存历史。

## 8. 测试层次

- 单元：parser、truncation、policy intersection、buffer、event conversion。
- 组件：Session/turn/tool registry、MCP、rollout、thread store。
- 协议：schema fixture、v1/v2 mapping、SDK generated types。
- 集成：mock Responses server 驱动完整 tool loop。
- 平台：Seatbelt、Linux sandbox、Windows backend。
- snapshot：TUI 和 rendered output。

## 9. 关键不变量

1. 每个 tool call 最终有 terminal outcome。
2. 未批准动作不会执行。
3. sandbox 失败不静默降级。
4. tool call/output 在 prompt 中配对。
5. item started/delta/completed 使用同一 id。
6. compaction 后保留任务和用户约束。
7. resume 不重复执行已提交副作用。
8. unknown additive field 不击穿旧客户端。

## 10. 构建与发布

workspace 同时使用 Cargo/Bazel、npm 和 SDK 代码生成。发布要同步 Rust protocol、JSON schema、TypeScript/Python types 和 CLI artifacts。

协议 fixture 是兼容证据：生成文件有变化必须与 Rust enum 变化一起审查。只依赖编译通过无法发现字段重命名、optional 变 required 或旧客户端不再容忍的新通知。

## 11. 当前验证限制

本环境没有 `cargo`，因此只完成源码、调用图、测试代码和 Markdown 静态校验，未执行 Rust tests。动态核验至少应覆盖 core、protocol、app-server、output-truncation、sandboxing 和 thread-store。

## 12. 设计评价

测试覆盖广，但巨大 reducer/dispatcher 使组合状态难穷尽。InnoAgent 应把关键不变量编码为 property/state-machine tests，并让配置、权限、prompt 和事件输出可比较的 resolved manifest。
