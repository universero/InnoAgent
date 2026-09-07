# 06. 权限、审批策略与 Guardian

## 1. 模块职责

权限模块回答四个问题：模型是否看得到工具、调用是否要批准、批准后能访问什么、自动审查反复拒绝时如何停止。Codex 用工具可见性、hook、approval、exec policy、Guardian 和 sandbox 分层实现。

## 2. 权限拦截总链路

```text
模型生成 tool call
  -> ToolRouter 检查存在与可见性
  -> payload/schema 解析
  -> PreToolUse hook
  -> handler 计算 PermissionProfile / SandboxPolicy
  -> exec policy 分析命令片段
  -> approval policy 决定 allow / ask / forbid
  -> 可选 Guardian 自动审查
  -> 等待 decision
  -> SandboxManager::transform
  -> 平台隔离后执行
  -> PostToolUse + terminal outcome
```

## 3. PreToolUse 与 PostToolUse

PreToolUse 在副作用前运行，可基于工具名、参数和 metadata 阻断。PostToolUse 在 handler 返回、失败或取消后运行，用于审计和结果处理。

PostToolUse 不能撤销已发生的写入或命令。要求阻止副作用的策略必须位于 PreToolUse、approval 或 sandbox。

## 4. 审批如何挂起

[request_command_approval](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/mod.rs#L2638) 的顺序：

1. 以 call/submission id 创建 oneshot。
2. 先注册 sender 到 Session pending map。
3. 再发送 `ExecApprovalRequest`。
4. 客户端以 `Op::ExecApproval` 回答。
5. submission loop 查找并移除 sender。
6. waiter 得到 decision 后继续 handler。

必须先注册后发事件，否则快速回答会丢失。接收端消失、channel 关闭或 turn 取消时返回 Abort，属于 fail-closed。

## 5. approval policy

策略决定哪些动作直接允许、哪些可询问、哪些禁止升级、是否可提出持久 rule amendment。`never` 表示“不询问”，不是“全部放行”；在受限 sandbox 下无法完成的动作应失败。

## 6. exec policy

[ExecPolicyManager](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/exec_policy.rs#L337) 对解析后的 command segments 应用 prefix/Starlark rules，产生 Allow、Prompt 或 Forbidden。

复合命令逐 segment 评估。只有全部 segment 明确 Allow 才可能绕过 sandbox；前半段安全不能掩盖后半段危险。解析失败不能乐观放行，因为 shell 重定向、子 shell 和动态展开天然难以静态穷尽。

## 7. PermissionProfile 与 sandbox policy

PermissionProfile 表达文件系统 roots、read/write、特殊路径和网络能力。旧 `SandboxPolicy` 与 granular permissions 并存，需要投影和求交。

无法识别的路径、未物化 workspace、非法 glob 或外部 sandbox 必须 fail-closed。测试 `unsupported_unresolved_and_optional_policy_shapes_fail_closed` 覆盖这些情况。

## 8. Guardian 自动审批

Guardian 是风险 reviewer，不是 sandbox。它构建有预算的 transcript，保留用户授权、最近工具证据和待执行 action，返回允许、拒绝或需用户确认。

transcript 为工具证据预留独立预算，避免巨大聊天历史挤掉当前危险动作；action 参数会裁剪，但必须保留 tool name 和关键结构。

## 9. Guardian 拒绝熔断

[record_denial](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/guardian/mod.rs#L219) 为每个 turn 保存 `consecutive_denials`、最近 review 窗口和 `interrupt_triggered`。

标准策略连续 3 次拒绝即中断，或最近窗口拒绝数达到阈值；非拒绝重置 consecutive。cyber specialty 第一次拒绝即可触发更严格中断。

触发后发送 `GuardianWarning`，异步 `abort_turn_if_active`，成功时补发 idle lifecycle。这是模型反复请求危险工具时的专项 loop breaker。

## 10. apply_patch 权限

apply_patch 先解析补丁得到路径，再判断是否需要审批和 sandbox。安全判断基于结构化 patch，而非模型描述。跨多文件 patch 中任一路径需要升级时，整个调用不能只批准安全子集。

## 11. request_permissions

环境权限请求改变当前环境/turn 的能力集合，命令审批只批准具体动作。响应有 grant scope，必须限制到允许的 turn/session。一次授权也不应自动写入长期 allowlist，除非 decision 明确携带 amendment。

## 12. 失败和竞态

- 重复回答 approval：pending sender 已移除，不能再次执行。
- turn 先取消：迟到批准不得复活调用。
- 客户端断开：fail-closed。
- Guardian 超时/失败：不能当 allow。
- sandbox 启动失败：不能静默回退无沙箱。
- PostToolUse 失败：不能声称副作用未发生。

## 13. 测试证据

- approval tests 验证 call id、允许、拒绝、取消和 channel 关闭。
- Guardian tests 验证连续 3 次、最近 10 次、非拒绝重置和 cyber 单次阈值。
- exec policy tests 覆盖复合命令和 prefix rule。
- permission intersection tests 覆盖无法解析时 fail-closed。
- sandbox escalation tests 验证 denied read 不因 prefix allow 被错误提升。

## 14. 设计评价

多层防御正确，但最终决策分散。InnoAgent 应产出统一 `AuthorizationDecision`：工具指纹、命中规则、Guardian 结果、用户决定、grant scope、最终 sandbox profile 和执行结果，并作为不可变审计记录持久化。
