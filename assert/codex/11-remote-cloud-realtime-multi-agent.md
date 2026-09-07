# 11. 远端、Cloud、Realtime 与多 Agent

## 多 Agent

[`AgentControl::spawn_agent`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/agent/control/spawn.rs#L1) 校验角色、深度/数量和上下文，最终通过 `ThreadManager::spawn_subagent` 创建子 thread。agent graph store 保存 parent/child 与状态；v1/v2 tool handlers 提供 spawn、send、wait、interrupt 等控制。

子 Agent 与顶层 Agent 共享核心运行时和很多资源，但应有独立 turn、取消、tool visibility 与 rollout identity。ephemeral/review 等角色会收紧工具。rollout-trace v2 可让子线程共享 root trace writer，独立顶层 thread 仍有独立 bundle。

主要风险是并发副作用冲突、循环等待、成本失控和上下文重复。图结构、深度限制、单写入控制和显式 wait/interrupt 比“任意递归 spawn”更可治理。

## Remote/Cloud

`cloud-tasks*` 管云任务 API、客户端和 mock；`cloud-config` 拉取受控配置；`backend-client`/`chatgpt` 对接后端。开源仓库包含客户端和协议实现，不代表 Codex cloud 服务端开源。远端 environment/exec 需要注册环境、同步 workspace roots 和转发状态，不能假设本地路径在远端存在。

`external-agent-migration` 负责外部 Agent 配置探测/导入；App Server 提供相应 API 和导入历史。迁移必须明确哪些配置可安全转换，尤其 hooks、MCP command 和凭据路径。

## Worktree

`worktree` 与 git utilities 支撑隔离分支/目录。它解决版本控制工作区冲突，不等同于 OS sandbox；恶意进程仍可能访问 worktree 外路径，必须叠加 sandbox permissions。

## Realtime 与 Voice

`realtime-webrtc` 提供实时媒体/信令能力，`voice-host` 负责语音宿主。Turn processor 也暴露 realtime start/append/stop 类操作。实时路径引入音频设备、WebRTC 生命周期、抖动、取消和隐私数据，与普通文本 Responses stream 的故障模型不同。

## Code Mode

`code-mode-protocol/runtime/host` 加上 core broker，让模型生成的代码在专用 worker 中调用工具。`CodeModeService::start_turn_worker` 只在 router 要求时启动，避免每轮固定付出 V8/worker 成本。代码模式扩大表达能力，也要求更严格的资源、超时、工具代理和结果序列化边界。

## 建议

InnoAgent 的多 Agent 应优先支持有界 DAG、父子预算、独立取消和只读共享上下文；远端执行、worktree 与 sandbox 必须作为三个正交维度建模。
