# 05. 工具、执行、审批与沙箱

## ToolRouter 与 Registry

[`ToolRouter`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/tools/router.rs#L1) 组合工具规格、来源和 handler。Registry 区分 trusted/external 工具；[`dispatch_any_with_terminal_outcome`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/tools/registry.rs#L495) 完成：查找工具 -> 校验 payload 类型 -> PreToolUse hook -> lifecycle started -> OTel span -> handler -> PostToolUse hook -> terminal lifecycle。

关键语义：PreToolUse 可以在副作用前阻断；PostToolUse 的 block 只能把结果改成拒绝/错误，不能撤销已经发生的文件写入或命令执行。任何审计系统都必须明确这个时间边界。

## Exec 与 apply_patch

[`ExecCommandHandler::handle_call`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs#L155) 解析命令、工作目录、timeout、TTY/session，计算审批需求和 sandbox transform，再交给本地或远端 exec backend。长进程通过 session id 与 stdin/poll API 继续交互。

[`ApplyPatchHandler::handle_call`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/tools/handlers/apply_patch.rs#L359) 解析补丁、检查影响路径、执行审批/隔离策略并产出结构化结果。`apply-patch` 独立 crate 负责语法和应用，不依赖模型文本“看起来正确”。

## 审批

[`request_command_approval`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/mod.rs#L2638) 先注册 pending oneshot，再发送 approval event。接收端消失时返回 `Abort`，即 fail-closed。环境权限请求在同文件约 2773 行；`Never` 或 granular policy 禁止请求时不会偷偷升级，可接入 Guardian 自动审查，取消时清理 pending。

审批回答的是“是否允许这次动作”，sandbox 回答的是“即使执行也能访问什么”，两者互补。`approval_policy=never` 不等于无沙箱，full access 也不等于跳过用户审批。

## Exec policy

[`ExecPolicyManager::create_exec_approval_requirement_for_parsed_commands`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/exec_policy.rs#L337) 对 shell 解析后的每个 command segment 应用 Starlark prefix rules，结果为 Allow/Prompt/Forbidden。只有所有 segment 都有显式 Allow 时才可 bypass sandbox；复合命令不能用前半段 allow 掩盖后半段风险。

## Sandbox

[`SandboxManager::transform`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/sandboxing/src/manager.rs#L358) 将抽象 policy 转成平台执行：macOS Seatbelt；Linux 优先 bubblewrap/seccomp，并保留 legacy Landlock 路径；Windows 使用 restricted/elevated backend。无法无损映射或启动隔离时按策略 fail closed。

保护不仅是 cwd 可写性，还涉及网络、额外目录、`.git`/`.codex` 敏感路径、环境变量、子进程继承和远端环境。legacy `SandboxPolicy` 与新 permissions split model 同时存在，投影可能有损，是当前安全模型的主要技术债。

## 评价

多层防御优于单一 allowlist，但必须保证决策日志能回答：谁请求、哪条规则命中、谁批准、最终 sandbox 是什么、实际退出状态是什么。InnoAgent 应先实现这条可审计链，再增加自动审批。
