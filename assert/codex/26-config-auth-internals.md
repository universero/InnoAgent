# 26. 配置与认证内部实现

## 1. 两阶段配置模型

Codex 将配置分成“原始 TOML 层”和“可执行 Config”。`config` crate 负责发现、读取、merge、requirements 和 provenance；`core/config` 的 `ConfigBuilder` 再结合 cwd、CLI/harness overrides、auth/model/environment 生成运行时配置。

[`load_config_layers_state`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/config/src/loader/mod.rs#L132) 是分层加载主路径；[`ConfigBuilder::build_inner`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/config/mod.rs#L1434) 把层结果解析成完整 runtime config。这样 App Server 可以读取/编辑层，core 则只消费校验后的 snapshot。

## 2. 配置来源与顺序

典型来源从低到高为 defaults/system、user、profile、trusted project、CLI/harness。cloud/enterprise managed requirements 不是普通“更高优先级值”，而是对最终配置施加不可突破的约束。`ConfigLayerEntry` 保存 source、value 和 metadata；`ConfigLayerStack` 保留顺序与诊断信息。

[`load_local_config_layers_with_overrides`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/config/src/loader/local.rs#L104) 加载本机层并应用 override；`discover_project_layers` 沿目录发现项目配置；`project_trust_context` 计算规范化 trust key。相对 cwd override 必须在 trust 判断前正确解析，避免通过路径表示差异绕过信任。

## 3. Merge 不是统一覆盖

普通 scalar 通常采用高优先级覆盖，table 递归 merge，数组按字段语义处理。但安全 requirements 使用专门的 merge state：例如 deny-read 集合做并集而不是被 profile 覆盖；managed hooks/model policy/permissions 各有独立约束合并。

因此不能用通用 deep-merge 替代当前逻辑。关键规则是：用户层可选择能力，managed 层限制可选择范围；低信任项目层不能注入 exec policy、session-start hooks 等高风险配置。源码测试明确检查 exec policy 与 hooks 只从 trusted project layer 加载。

## 4. 配置子模块

| 子模块 | 输入与处理 | 输出/使用方 |
|---|---|---|
| `config_toml` | serde 解析用户 TOML | typed raw config |
| `loader/layer_io` | 读取文件/managed source | layer entries + diagnostics |
| `loader/local` | system/user/project/override 排序 | local layer stack |
| `loader/project_discovery` | cwd/root markers/trust | project layers |
| `merge` | 按字段合并 TOML | effective value tree |
| `overrides` | 解析 `key=value` CLI 路径 | 顶层 overlay |
| `profile_toml` | 选择 user profile | profile overlay |
| `config_requirements` | 反序列化 managed policy | typed constraints |
| `requirements_layers/*` | hooks/models/permissions/rules 合并 | effective requirements |
| `permissions_toml` | permission profiles 与 merge | profile catalog |
| `auth_policy` | 允许的登录模式/account 限制 | login enforcement |
| `mcp_types`, `mcp_requirements`, `mcp_edit` | MCP server config/约束/写入 | McpRuntimeInput |
| `plugin_edit`, `marketplace_edit` | 插件配置的结构化编辑 | PluginsManager reload |
| `skills_config` | skill roots/toggles | skills catalog |
| `hook_config` | hook declarations | HookRegistry |
| `shell_environment_policy` | 环境变量继承/过滤 | exec environment |
| `browser_use`, `computer_use` | 浏览器/桌面能力设置 | tools/requirements |
| `application_requirements` | 应用级强制项 | capability projection |
| `cloud_config_bundle/layers` | 远端受管配置 | layer stack |
| `strict_config` | unknown/deprecated/error policy | 启动是否允许继续 |
| `diagnostics` | 带 source 的 warning/error | TUI/App Server 展示 |
| `fingerprint` | 配置稳定摘要 | reload/cache 判断 |
| `thread_config` | thread 作用域加载与 remote 适配 | start/resume config |
| `tui_keymap` | key chord 配置校验 | TUI input map |

## 5. Permission Profile 解析

permission profile 将 filesystem、network、approval、Windows backend 等组合成可选择配置。`merge_permission_profiles` 合并用户定义；managed profiles 限制可用集合；core 再解析 active profile 为 `ResolvedPermissionProfile`。展示名称和 sandbox summary 不能替代 resolved policy。

legacy sandbox mode 到 granular permissions 的投影可能有损。遇到同时传入两种表示时，App Server/core 应拒绝矛盾，而不是随意选择。managed filesystem constraints 在 core config 最终阶段施加，保证 CLI/turn override 不能绕过。

## 6. Network 与应用约束

managed network requirements 可包含 enable/disable、allowed/denied domains、代理策略。deny-only 与 allow-only 在 full access 下有专门语义，global wildcard 可能被 managed policy 拒绝。最终 network proxy runtime 再验证实际 policy 与 constraints 相容。

MCP、plugins、apps、hooks、models 也可受 requirements 限制。刷新 MCP config 时必须同时替换 managed server/plugin requirements，不能只刷新用户层导致旧约束残留或新约束漏用。

## 7. Config 编辑与 reload

App Server `ConfigManager` 提供 read/value_write/batch_write，先读取层与 schema，再修改指定用户文件并 reload。批量写应作为一个逻辑操作验证，避免前半成功后半失败。`fingerprint` 和 file watcher 判断是否真的变化，随后刷新 models/MCP/plugins/skills 等依赖配置的 runtime。

reload 不能原地改变正在执行的 turn；新 snapshot 用于新 thread/turn，必要的 UI notification 明确告知变更。解析错误保留旧有效配置，同时报告 source/span，而不是把整个 runtime 清空。

## 8. 认证对象

`login` crate 的 auth manager 从 API key 环境变量、auth file/keyring、ChatGPT OAuth 或 Bedrock key 中解析当前认证。不同认证模式互斥切换时会清理不再适用的凭据，例如切换 OpenAI API key 与 Bedrock key，避免旧凭据被意外继续使用。

ChatGPT token refresh 包含：读取当前 auth generation -> 请求 refresh -> 再检查 auth 是否已被其他线程修改 -> 仅在仍匹配时更新 storage。该 compare-before-write 避免慢 refresh 覆盖用户刚完成的新登录。expired refresh token、account/workspace mismatch 被标记为永久错误，不应无限重试。

## 9. 凭据存储与传播

API key 可以来自显式参数或环境；长期 OAuth token 优先进入 keyring/受控 auth store。`AuthCredentialsStoreMode` 决定 file/keyring 等后端。provider 请求只获得所需 token/headers；日志使用 redacted type。HTTP redirect、proxy 和 MCP OAuth 各自还要限制 credential origin。

App Server 的 account login 是异步流程：请求返回 login id，完成/失败通过 notification；MessageRouter 按 login id 路由。连接断开要取消等待但不制造“登录成功”。

## 10. 配置与认证测试不变量

- project profiles 被忽略，避免仓库选择用户 profile。
- 未信任项目的 hooks/exec policy 不加载。
- managed requirements 优先于用户和 CLI 放宽请求。
- deny 集合合并不会因普通 TOML 覆盖而缩小。
- relative path override 不改变 project trust 判断。
- strict mode 对未知/错误字段明确失败。
- token refresh 不覆盖更新后的 auth generation。
- account mismatch 与过期 refresh token 是永久错误。
- proxy/custom CA 设置同时作用于登录与模型请求。
- config write 后 schema、source metadata 和 comments/结构尽量保持。

## 11. 设计判断

Codex 配置系统复杂的根源不是 TOML，而是“来源、信任、约束、作用域、热更新”五个维度。InnoAgent 若只用最后写入覆盖，会在企业策略和项目配置之间产生安全漏洞。最小正确模型应是 `value + source + scope + constraint + generation`。

## 12. Config 根模块遗漏项说明

| 模块 | 具体职责 |
|---|---|
| `browser_computer_use_requirements` | 合并浏览器与 computer-use 的受管限制。 |
| `cloud_config_layers` | 将远端 bundle 分解为有来源的 layer entries。 |
| `codex_home_symlink` | 检测/规范化 Codex home symlink，保证 trust key 稳定。 |
| `config_layer_source` | 定义 user/system/project/managed/CLI 来源 metadata。 |
| `host_name` | 获取主机标识供配置条件和诊断。 |
| `in_app_browser_requirements` | 对内置浏览器能力施加 managed policy。 |
| `key_aliases` | 兼容旧配置键并规范化到当前 schema。 |
| `project_root_markers` | 配置项目根发现所用 marker。 |
| `requirements_exec_policy` | 把 managed requirements 转为 exec policy 限制。 |
| `state` | 保存 ConfigLayerEntry/Stack 和加载状态。 |
| `types` | 集中定义最终配置和各功能 TOML 类型。 |
| `test_support` | 构建临时 home/layer/requirements fixture。 |
