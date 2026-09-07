# 07. 沙箱、命令执行与文件系统

## 1. 模块职责

该模块把“允许执行”落实为操作系统可强制的资源边界，并管理短命令、后台进程、stdin、stdout、超时、退出状态和补丁写入。审批决定意图，sandbox 限制能力，二者缺一不可。

## 2. exec 调用链

```text
ExecCommandHandler::handle_call
  -> 解析 command/workdir/timeout/yield/tty
  -> shell parser 拆分 command segments
  -> exec policy / approval / permissions
  -> 构造 ExecParams 与 SandboxPolicy
  -> SandboxManager::transform
  -> local/remote exec runtime
  -> spawn child + output collectors
  -> yield 内结束或返回 session id
  -> ExecCommandToolOutput
```

[ExecCommandHandler](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs#L155) 不直接调用裸 `Command`，而是先完成权限和 runtime 选择。

核心对象是 `ExecParams`、`ExecCommandToolOutput`、`UnifiedExecProcess`、`ProcessManager`、`TerminalPermissions`、`SandboxPolicy` 和 `SandboxTransform`。参数、运行中句柄和最终输出分离，避免一个结构同时承担请求、进程控制和序列化。

## 3. 短命令与后台命令

命令在 `yield_time` 内完成时返回完整终态；仍运行则返回 session id，后续通过 write stdin/poll 获取增量。非空 stdin 写入默认快速返回，空 poll 可以等待更久。

后台进程保存在 process manager，最多 64 个。turn interrupt 不自动杀掉这些进程；显式 cleanup 才终止，避免开发服务器因一次模型中断被误杀。

## 4. 输出采集

stdout/stderr 由异步 reader 收集并发送 delta，同时汇入 1 MiB `HeadTailBuffer`。即使客户端长时间不 poll，内存也保持上界。终态包含 exit code、wall time、chunk/session id 和 original token count。

## 5. sandbox 抽象

[SandboxManager::transform](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/sandboxing/src/manager.rs#L358) 把抽象 policy 转成平台启动方式：

- macOS：Seatbelt profile。
- Linux：bubblewrap/seccomp，并保留 legacy Landlock 路径。
- Windows：restricted/elevated backend 和私有桌面等能力。

transform 输出的不只是命令前缀，还包括环境、cwd、网络和路径映射。平台无法表达策略时应失败，而不是降级为无隔离。

`SandboxManager` 选择 backend 时还要考虑当前 OS、feature、外部环境和是否已在容器中。抽象 policy 是意图，materialized command 才是实际执行证据；二者都应进入审计。

## 6. 文件系统语义

策略区分 read roots、write roots、只读 carve-out、特殊路径和缺失路径行为。workspace 可写不意味着整个父目录可写；`.git`、`.codex` 等路径可单独保护。

路径比较必须处理 symlink、canonicalization、Windows path convention 和尚不存在的目标。对写操作过早 canonicalize 不存在路径会失败，过晚又可能被 symlink 绕过，因此实现使用 scoped resolution 和 preserving-symlinks 辅助逻辑。

## 7. 网络

网络权限与文件权限独立。sandbox 网络禁用时，exec policy 允许某个命令并不自动开放网络；网络代理功能在无 sandbox network 能力时也应为 no-op，而不是绕过策略。

MCP server 若作为外部进程运行，其网络由该 server 的启动环境决定，不自动继承本次 shell tool 的 sandbox。

## 8. apply_patch

[ApplyPatchHandler](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/tools/handlers/apply_patch.rs#L359) 先解析 patch，再计算受影响文件、审批和 sandbox。独立 apply-patch crate 负责语法与原子操作细节。

解析阶段必须拒绝目录穿越、非法 hunk 和不一致上下文。执行成功事件携带结构化变更，turn diff tracker 汇总为统一 diff。补丁成功不代表用户看到的最终 diff 已持久化，仍要经过 rollout/event。

apply_patch 与 shell 写文件的区别是前者在执行前就能得到完整目标路径和 diff，因此权限判断更精确；shell 重定向可能依赖动态语法，只能结合解析、审批和 sandbox 保守控制。

## 9. 失败恢复

- spawn 失败：返回 host/tool error，不生成假 exit code。
- timeout：终止或标记进程，保留已采集 head/tail。
- sandbox denial：可按策略请求升级；`never` 下直接失败。
- 进程仍运行：返回 session id，不把超时观察误判为进程失败。
- stdin 写入 closed process：返回终态/缺失句柄，不能重建同一命令。
- patch 部分失败：报告结构化失败，不应让模型假设所有文件已修改。

## 10. 测试与评价

测试覆盖超时、TTY、后台 poll、buffer 上界、sandbox escalation、denied reads、路径保护和 patch 多文件失败。平台 sandbox 是正确的最后防线，但三平台能力并不完全同构；应把“请求 policy”和“实际 materialized policy”同时记录，不能只记录用户配置。
