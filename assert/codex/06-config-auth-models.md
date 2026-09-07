# 06. 配置、认证与模型管理

## 配置层

官方公开优先级为：CLI override > trusted project `.codex/config.toml` > selected profile > user config > system config > defaults。源码中的 `config`、`config-schema`、`codex-home`、`features` 和 approval presets 分别处理加载、schema、目录、feature gate 和安全预设。

项目配置只有在项目受信任时生效，避免 clone 一个仓库就自动获得任意 MCP、hook 或高权限设置。配置结果不是原始 TOML map，而是经过类型化、默认值、managed requirements、profile 和 policy projection 后的 snapshot。App Server 的 config read/write/batch write 也复用该层，而不是直接改文件。

## 运行时覆盖

thread start 可指定 cwd、model、sandbox/permissions、approval、personality、collaboration mode 等；turn start 还能做本轮覆盖。[`turn_start_inner`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/app-server/src/request_processors/turn_processor.rs#L514) 先校验输入，再构建 environments 和 thread settings override，最后提交 `TurnInputRequest`。这说明设置具有 process/config、thread、turn 多级作用域。

## 认证

`login` 管 ChatGPT/API key 登录流程，`keyring-store` 负责凭据存储，`aws-auth` 和 `workload-identity` 支撑特定运行环境，`backend-client`/`chatgpt` 封装后端调用。认证状态与 provider config 分离：选择 endpoint 不应直接意味着凭据来源。

敏感值使用 redacted wrapper、环境变量或 keyring，日志和错误路径需避免泄漏。App Server 初始化记录 client metadata，但认证能力仍由 server runtime 控制，客户端不能仅通过 JSON-RPC 参数自授予权限。

## Provider 与模型目录

`model-provider-info` 保存静态 provider 描述，`model-provider` 构建实际 client，`models-manager` 合并本地、远端或动态模型目录及能力。`ollama`、`lmstudio` 是本地 adapter。模型选择必须同时考虑 provider、context window、reasoning/summary 支持、Responses/tool 支持和 service tier。

## 风险

- 配置来源多，若 UI 只显示最终值而不显示来源，排障困难。
- managed policy、project trust、feature flag、turn override 可共同决定行为，组合测试成本高。
- 动态模型目录可能与静态 fallback 不一致；关键启动路径需要稳定 fallback。
- 旧 sandbox enum 与 granular permission 并存时，配置 round-trip 可能丢信息。

InnoAgent 应为每个有效配置值保存 provenance，并让安全相关 override 只能收紧或显式审批后放宽。
