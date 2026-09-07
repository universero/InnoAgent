# 03. 核心运行时与 Turn Loop

## ThreadManager

[`ThreadManager`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/thread_manager.rs#L1) 是进程级 thread 编排器，持有共享 auth、models、extensions、stores 和 agent graph。`start_thread`、`resume_thread_from_rollout`、`fork_thread` 分别创建、恢复和派生 thread；remove/shutdown 负责资源释放；subagent 路径仍回到同一 manager，因此子 Agent 不是另一套 runtime。

## Session 不变量

[`Session`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/session.rs#L1) 代表一个持续会话。源码注释声明同一 session 同时最多一个 running task，但新用户输入可以 interrupt 或 steer 当前 task。`Session::spawn` 建立 submission/event channel、持久化和后台服务。

[`submission_loop`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/handlers.rs#L529) 串行消费 `Submission<Op>`。它集中分派 turn input、interrupt、approval response、MCP refresh、compact、rollback、review、shutdown 等操作。这个 actor-like 单写入点避免多个 UI 请求直接并发修改 Session；工具本身仍可并行执行。即使输入 channel 非正常消失，循环退出路径也会 teardown runtime、发 thread stop lifecycle 并关闭持久层。

## 一轮 turn

[`run_turn`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/turn.rs#L163) 的主流程是：

1. 固化 turn settings、environment、skills 和可用工具。
2. 构建 `ToolCallRuntime`，按需启动 code-mode worker。
3. 从历史、用户输入、developer instructions、环境上下文等构建 Prompt。
4. 调用 sampling request，消费 Responses stream。
5. 将 assistant items、reasoning、tool calls 转为内部事件和持久化 item。
6. 对工具调用经 ToolRouter 执行并把 output 回填下一次 sampling。
7. 直到模型完成、用户中断、错误、上下文超限或 usage limit。

`run_sampling_request` 位于同文件约 1416 行。provider stream 错误按可重试类型处理；上下文超限会走压缩/错误语义，usage limit 单独上报，不能与瞬态网络重试混同。

## 并发与取消

Session 状态变更串行化，但模型流、工具、MCP、exec session 和后台任务均是异步的。取消 token 与 turn id 用于抑制旧 turn 结果污染新 turn。审批使用 oneshot request/response，先登记 pending 再向 UI 发事件，防止响应先到造成竞态。

## 评价

这是“串行控制面 + 并行数据面”的合理方案。风险是 `run_turn` 和 handler dispatch 逐渐吸收过多产品语义。应持续把 provider、tool runtime、context policy、persistence lifecycle 提取为可单测组件，而不是继续在 turn 函数内加分支。
