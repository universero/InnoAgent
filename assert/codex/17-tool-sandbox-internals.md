# 17. 工具执行与安全内部实现

## 1. 工具从声明到结果

工具链不是一个 `name -> function` 字典，而是五层对象：spec 决定模型可见 JSON schema；router 决定本轮可见集合；registry 绑定 handler 与来源；runtime 决定并发和取消；handler 完成领域校验、审批和执行。

完整时序：

1. core、extension、MCP、plugin 根据配置注册工具。
2. ToolRouter 根据 model/feature/role/environment 过滤规格。
3. 模型返回 function call name、call id、arguments。
4. registry 校验工具存在、payload 类型和来源。
5. 执行 PreToolUse hook；block 在副作用前终止。
6. 发 tool started，创建 trace span。
7. handler 请求审批、选择 sandbox/backend、执行。
8. 执行 PostToolUse hook；它只能改变返回结果，不能撤销动作。
9. 发送 completed/failed/declined terminal outcome，写入 rollout。

[`dispatch_any_with_terminal_outcome`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/tools/registry.rs#L495) 是该时序的集中实现。每条分支都必须产生 terminal outcome，否则 UI/trace 会永久显示 running。

## 2. 并发工具 runtime

`tools/parallel.rs` 的 `ToolCallRuntime` 按 call 管理 futures，并把结果与原 call id 对齐。并行只适用于允许并行的工具；共享 PTY、工作区写入、计划/Goal 等状态型工具需要串行或自行加锁。turn cancel 要向所有活动 call 传播，并等待/标记无法立即停止的外部操作。

## 3. Unified Exec

`core/tools/handlers/unified_exec` 将一次 tool call 归一化为命令、cwd、env、timeout、TTY 与 session 操作。`exec_command` 可创建新进程或轮询已有 session，`write_stdin` 向已有 PTY 写入。handler 先规范化命令和工作目录，再计算 policy/approval，最后调用 `sandboxing` 或 `exec-server`。

`shell-command` 解析 shell 结构并提取 command segments；`command_canonicalization` 处理 wrapper/shell 形式，降低通过 `bash -c` 等包装绕过 prefix rule 的概率。解析失败不能默认 allow。

## 4. Exec Policy

`execpolicy` 运行 Starlark 规则，core 的 [`create_exec_approval_requirement_for_parsed_commands`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/exec_policy.rs#L337) 聚合每个 segment 的 Allow/Prompt/Forbidden。聚合规则应满足 Forbidden 优先、Prompt 次之、全部显式 Allow 才能绕过 sandbox。未匹配不是 Allow。

executable identity 子模块解析真实可执行文件，防止仅按用户输入首 token 判断；Windows command line 和 POSIX shell 解析分别测试。

## 5. 审批状态机

审批请求包含 command/patch、cwd、风险、权限增量和稳定 request id。Session 先把 oneshot sender 放入 pending map，再发事件；UI 返回 accept/decline/abort，handler 等待结果。取消或连接关闭清理 map，receiver 失败按 Abort。可选 Guardian reviewer 能自动批准、拒绝或要求人工，但其输出仍经同一状态机。

审批 grant 应绑定参数快照，而不是只绑定工具名；命令在审批后若发生重写必须重新审批。remember/prefix rule 是持久策略变更，与单次批准分开。

## 6. Apply Patch

`apply-patch` crate 解析自定义 patch grammar、定位文件、校验上下文并原子应用可行的 hunks。core handler 负责将 patch 影响路径映射到 sandbox/approval，并生成 diff/event。直接调用 standalone executable 和模型工具调用应共享 parser，避免两套边界行为。

失败包括 malformed patch、上下文不匹配、路径越界、目标已变化、权限拒绝和部分写入。调用方必须基于结构化结果判断，不能只读 stdout 文案。

## 7. Sandbox Manager

[`SandboxManager::transform`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/sandboxing/src/manager.rs#L358) 将命令与抽象 policy 变成平台 launch spec：

| 平台 | 主要实现 | 限制点 |
|---|---|---|
| macOS | Seatbelt profile，MXC 辅助路径 | profile 路径/网络规则需正确转义 |
| Linux | bubblewrap + seccomp，legacy Landlock fallback | user namespace、系统 bwrap 可用性差异 |
| Windows | restricted token 或 elevated service | filesystem projection 与 Unix 语义不同 |

`policy_transforms` 把 permission profile、writable roots、network 与 protected paths 映射为平台策略；`denial`/`violation` 识别 sandbox 拒绝并提供可解释错误；`terminal_queries` 回答进程终端查询；`spawn` 执行最终进程。无法建立要求的隔离时不得静默裸跑。

## 8. Exec Server

`exec-server` 把执行与文件系统能力服务化：

- `client`/`server`/`rpc`：请求关联、session registry 和 dispatch。
- `environment*`：环境注册、bootstrap、config、capability discovery。
- `local_process`/`remote_process`：统一进程句柄和输出流。
- `local_file_system`/`remote_file_system`：路径 URI 映射与文件流。
- `no_follow`/`regular_file`/`sandboxed_file_open`：防 symlink traversal 和文件类型混淆。
- `noise_relay`：有序加密 frame、握手和双向转发。
- `client_recovery`/`client_refresh`：连接恢复与能力刷新。
- `websocket_pong_watchdog`：检测半开连接。

远端执行的“accepted”只表示服务端接单，不等于完成；operation/session id 必须贯穿后续 stream 和取消。

## 9. 其他安全模块

`network-proxy` 实施 egress policy、credential broker 和执行关联；`process-hardening` 降低宿主进程攻击面；`shell-escalation` 封装需要升级的路径；`secrets`/redacted-string 控制日志泄露；`keyring-store` 保存长期凭据。worktree 只隔离 Git 修改，不是安全边界。

## 10. 必测攻击面

- `cmd1 && cmd2`、subshell、解释器包装和路径别名绕过 policy。
- symlink、`..`、大小写、UNC path 和 mount 重开导致的目录逃逸。
- approval 后参数变更、重复响应、过期响应和断连。
- sandbox 初始化失败时裸执行。
- Post hook 被误认为事务回滚。
- 远端 accepted 后重试导致重复副作用。
- MCP/plugin 工具绕过统一 registry。

## 11. Sandboxing 根模块逐项

| 模块 | 实现作用 |
|---|---|
| `manager` | 选择 backend、执行 policy transform 并启动命令。 |
| `policy_transforms` | 将抽象 permissions 转成平台可表达规则。 |
| `seatbelt` | 生成 macOS sandbox-exec profile 与参数。 |
| `landlock` | 建立 Linux Landlock filesystem rules。 |
| `bwrap` | 构造 bubblewrap namespace/mount/network 参数。 |
| `windows` | restricted/elevated Windows backend 选择。 |
| `windows_mxc` | Windows/MXC 特殊环境适配。 |
| `spawn` | 执行变换后的 program/env/cwd并管理子进程。 |
| `denial` | 从 stderr/status 识别 sandbox denial。 |
| `violation` | 生成结构化 violation 和可展示原因。 |
| `terminal_queries` | 回答终端属性查询，避免 sandbox child 卡死。 |
