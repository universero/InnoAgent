# 11. 多 Agent、Goal、队列与 Realtime

## 1. 模块职责

该模块解决长任务拆分、子 Agent 通信、用户输入排队、目标预算记账，以及实时语音与普通 turn 共存。

## 2. AgentRegistry 与路径身份

子 Agent 使用路径身份，例如 `/root/reviewer`。registry 管理 spawn、状态、父子关系、reset ordinal 和关闭。路径便于理解，内部仍需稳定 thread/session id。

核心对象包括 `AgentRegistry`、`AgentPath`、子 thread handle、mailbox、`InterAgentCommunication`、`GoalAccountingState` 和 queue records。registry 管拓扑，thread store 管持久状态，两者不能互相替代。

实现入口：[AgentRegistry](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/agent/registry.rs)、[GoalAccountingState](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/ext/goal/src/accounting.rs)。

## 3. spawn 链路

```text
spawn_agent -> 校验槽位、名称和 fork_turns
  -> 创建子 thread/session -> 选择继承历史
  -> 记录 parent/root metadata -> 启动子 turn
  -> CollabAgentSpawnBegin/End
```

子 Agent 可继续 spawn，因此必须有全局并发上限和层级约束。继承的是可序列化上下文，不是父 Agent 的 pending approval、future 或 UI 状态；继承上下文也不能提升 sandbox 权限。

spawn 失败必须发 begin 对应的 terminal/end 事件，并释放预占槽位。父 Agent 不能仅凭 spawn tool 成功就认定子任务完成，而要等待状态或最终消息。

## 4. mailbox

send/followup 把消息写入目标 mailbox。运行中的采样可在 item 边界发现 pending mail 并提前结束当前 response，让下一 step 纳入消息。这是受控抢占，不是任意修改已发请求。

消息带 author、recipient、内容和可选 encrypted content，转为 `AgentMessage` 历史 item，持久化后才能在 resume 中恢复。

## 5. wait 与 queue

wait 只观察已存在 Agent 的状态。超时只是观察超时，不等于 Agent 失败；轮询是合法重复调用。

用户在 Agent 忙时的新消息进入 durable thread queue，并发 `ThreadQueueChanged`。queue 是用户到 thread，mailbox 是 Agent 间通信。

## 6. Goal

Goal 保存 objective、status、token/time usage 和进展，状态写入 thread store，并发 `ThreadGoalUpdated`。父 Agent 可计入 descendant usage；进度更新用 semaphore 串行，避免并发 tool completion 重复扣费。

## 7. Goal 失败熔断

`record_tool_outcome` 只把默认 namespace 的 `exec`、handler 确实执行且失败标为 execution failure。任意成功工具重置累计。

若活动 goal 的一个 turn 没有成功工具但发生 exec failure，则同一 goal 连续失败 turn 加一；达到 3 才返回 goal id 供上层标记 blocked。模型回答错误、审批拒绝、用户中断或任意业务失败不会都被算入。

[execution_failure_goal](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/ext/goal/src/accounting.rs#L134) 实现这一阈值和按 goal id 重置逻辑。

## 8. Realtime

Realtime 用独立 `Op/EventMsg` 管理 start/audio/text/speech/SDP/close/voices。它共享 thread/session 身份，但媒体流生命周期独立于普通 Responses sampling，UI 也不能复用普通 message delta reducer。

## 9. 风险与测试

风险包括子 Agent 同时改同一文件、mailbox 改变原计划、权限错误继承、goal blocked 启发式误判和 usage 重复记账。测试覆盖 spawn/reset、mailbox、wait、goal usage、三次 failure、成功重置和 realtime lifecycle。InnoAgent 还应增加工作区租约或文件级冲突检测。

多 Agent 的正确完成条件是父目标得到证据并整合，而不是所有子 Agent 进入 terminal。子 Agent 失败、取消或超时应作为结果返回，由父 Agent 决定重试、降级或终止。
