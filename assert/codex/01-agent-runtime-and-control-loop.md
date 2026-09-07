# 01. Agent 运行时与控制循环

## 1. 模块职责

运行时需要把并发到达的用户输入、设置变更、审批回答、工具完成、取消和后台事件收敛为可推理的顺序。Codex 的关键设计不是“递归调用模型”，而是用 thread、session、submission、turn、step 五个层次隔离状态。

## 2. 五层运行对象

### 2.1 Thread

Thread 是用户可见的长期对话身份，关联 rollout、项目、标题、归档状态和可恢复历史。`ThreadManager` 创建、恢复、fork 并维护活动 thread。

### 2.2 Session

`Session` 是 thread 在当前进程中的活动实例，拥有当前配置与服务、`ContextManager` 历史、active turn、submission channel、pending approvals、event sender、rollout recorder、后台终端和 extension data。

Session 把“可持久化 thread”与“当前进程内句柄”分开。resume 会重建后者，而不是序列化 Tokio task、channel 或 mutex。

### 2.3 Submission

外部操作封装成 `Submission { id, op }`。`Op` 包含 `TurnInput`、`Interrupt`、审批回答、settings update、compact、rollback、review、realtime 等。submission loop 是 session mutation 的串行化边界。

### 2.4 TurnContext

TurnContext 描述一个用户 turn 的稳定语义：turn id、模型、cwd、权限、sandbox、reasoning、输出 schema、session source、工具配置和取消 token。它不是整个 thread 配置的别名，而是一次 turn 的快照。

### 2.5 StepContext

一次 turn 可以多次请求模型。每次请求对应 step，StepContext 捕获当时有效的设置和工具可见性。steering 或 turn settings 更新可能改变后续 step，但不能回写已经发出的模型请求。

## 3. 入口链路

```text
客户端提交 Op::TurnInput
  -> Session submission_loop 读取队列
  -> 校验当前 turn 和输入模式
  -> start_or_steer_turn
  -> 构建 TurnContext
  -> spawn_task(TaskKind::Regular)
  -> run_turn
  -> pre-sampling compact / hooks / 输入记录
  -> 循环 run_sampling_request
  -> 处理模型 item、启动工具 future
  -> 工具输出写回 history
  -> needs_follow_up ? 下一次采样 : 完成
  -> stop hooks / TurnComplete / idle lifecycle
```

源码入口：[`submission_loop`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/handlers.rs#L529)、[`run_turn`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/turn.rs#L163)。

## 4. 为什么 submission 必须串行

假设客户端同时发出“开始 turn”“切换模型”“审批命令”“中断”。如果这些操作直接并发修改 Session，会出现审批回答先于 pending waiter 注册、模型切换影响已发出的请求、interrupt 找不到刚创建的 task、rollout 顺序与 UI 顺序不同。

Codex 把这些操作先排入 channel，再由 submission loop 顺序解释。工具执行本身可以并发，但控制面的状态改变有单一顺序。这是 actor-like 设计，不等于所有工作都单线程。

## 5. turn 内部循环

`run_turn` 同时承担 pre-sampling compaction、turn hooks、MCP 启动、模型 client session、prompt 构建、多次 sampling、tool future drain、mailbox 抢占、token budget、diff、stop hook 和终止事件。

核心循环条件是采样结果 `needs_follow_up`。模型返回工具调用、`end_turn=false` 或队列中存在需要处理的输入时继续；形成最终 assistant message 且没有待处理工具时退出。

## 6. 工具并发与 turn 串行并存

模型一次响应可返回多个工具调用。`try_run_sampling_request` 将工具调用转成 future 放入 `in_flight`，继续消费流事件，响应完成后统一 `drain_in_flight`。

1. 文本 delta 不必等待工具结束才向 UI 流动。
2. 独立工具可以并发执行。
3. 下一次模型采样必须等本批工具结果归并完成，避免观察到半完成状态。

并行不是无条件的。具体 handler、工具语义、审批和共享工作区仍可能串行化或产生冲突，模型端的 `parallel_tool_calls` 只是允许，不是正确性保证。

## 7. steering 与设置变更

`TurnInput` 带 routing mode，可启动新 turn 或向活动 turn 注入输入。steering 不会修改已经发送到 provider 的 request，只能进入输入队列，在后续 step 消费。

`ThreadSettings` 影响未来 turn；`TurnSettings` 指向具体运行中 turn；StepContext 则固化一次采样。三层设置避免“全局配置热更新”导致历史请求语义漂移。

## 8. 取消与中断

`Op::Interrupt` 取消当前 task，但不会自动终止后台 terminal；清理后台终端使用独立的 `CleanBackgroundTerminals`。这一区分防止用户只想停止模型时意外杀死显式启动的长期服务。

取消 token 传播到模型流和工具 future。采样结束后仍会先 drain 必须收尾的工具、发送已记录 token usage，再检查取消并返回 `TurnAborted`，避免账务和客户端状态丢失。

Guardian 触发的中断还会补发 idle lifecycle，因为它绕过正常任务完成路径；普通用户 interrupt 不走相同补偿逻辑。

## 9. turn 完成条件

`response.completed` 只代表一次 sampling 完成。系统还要确认所有 in-flight tool future 已结束、工具结果已写回上下文、没有 `needs_follow_up`、token/diff 事件已发、stop hook 没要求继续、没有 cancellation 或 budget error。之后才产生 `TurnComplete`。

## 10. 错误与恢复

### 10.1 模型流错误

可重试网络错误留在 sampling 逻辑中；上下文超限可触发 compact 后重试；不可恢复错误转为 `Error` 并结束 turn。

### 10.2 工具错误

工具可返回“给模型看的失败结果”，使模型修正参数；host panic、channel 失败或 invariant 破坏则终止 turn。两者不能合并成一个异常类型。

### 10.3 审批等待

审批是挂起点，不是 turn 结束。pending waiter 由 call id 关联；客户端断开或 sender 消失时默认 Abort，避免无人响应时自动放行。

### 10.4 进程重启

运行中的 future 无法恢复；resume 从 rollout 重建已提交历史和 thread 配置。未形成终态的工具调用要由 history normalization 生成合成输出或显式失败，保证下一次 prompt 结构合法。

## 11. 状态机

```text
Idle -> Queued -> Preparing -> Sampling
Sampling -> ToolPending / ApprovalPending -> Sampling
Sampling -> Completing -> Idle
活动状态 -> Interrupting -> Aborted -> Idle
Preparing / Sampling -> Compacting -> Sampling 或 Failed
```

源码没有用单一 enum 表达全部状态，而由 task presence、turn context、pending maps、事件和 rollout 条目共同表示。客户端必须消费事件投影，不应猜测内部 mutex 状态。

## 12. 测试证据

- session tests 覆盖 start、steer、interrupt、settings 更新和并发 submission。
- turn tests 覆盖工具调用、流式 delta、取消后 token 事件、context overflow 和 compaction。
- `spawn_task_does_not_update_previous_turn_settings_for_non_run_turn_tasks` 证明非普通 turn task 不污染上一轮设置。
- review tests 验证 review task 仍发完整 lifecycle。

## 13. 设计评价

优点是控制面顺序清晰、运行面可并发、取消和审批都是一等状态。主要风险是 `run_turn` 和 `try_run_sampling_request` 职责过重，后者在固定版本中约 608 行。更可维护的方案是把 sampling reducer、tool scheduler、stream projector、turn finalizer 拆成显式组件，并以状态转换测试覆盖组合。
