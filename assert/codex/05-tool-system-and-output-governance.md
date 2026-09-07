# 05. 工具体系与输出治理

## 1. 模块职责

工具模块把模型返回的字符串参数变成受控副作用，并把执行结果转换成既适合用户观察、又不会挤爆模型上下文的结构。核心问题包括工具发现、schema、路由、生命周期、并发、长输出、长文件、失败和重复调用。

## 2. 工具来源

- 内置工具：exec、apply_patch、plan、request_user_input、view_image 等。
- Responses 托管工具：web search 等，由 provider 执行。
- MCP 工具：运行时发现并映射为 namespaced function tool。
- extension/plugin 工具：通过 registry contributor 注入。
- dynamic tool：客户端在宿主侧执行，再把结果返回 core。
- 多 Agent 工具：spawn、send、wait、close 等。

`ToolRouter::model_visible_specs` 决定哪些工具真正暴露给当前模型。注册存在不代表可见；feature、session source、subagent role、权限和 provider capability 都可过滤。

## 3. 规格与实现分离

工具 specification 包含 name、description、JSON schema 和 Responses tool type；handler 实现执行。`ToolRegistry` 建立 `ToolName -> handler` 映射并拒绝重复注册。

工具名包含 namespace，避免 MCP/plugin 与内置工具碰撞。兼容旧工具名时应在边界归一化，不能让多个别名变成不同权限主体。

## 4. 完整分发链路

```text
ResponseItem::FunctionCall / CustomToolCall
  -> 解析 name / namespace / call_id / arguments
  -> ToolRouter 选择 handler
  -> ToolRegistry::dispatch_any_with_terminal_outcome
  -> payload 类型校验
  -> PreToolUse contributors/hooks
  -> lifecycle started + telemetry span
  -> handler.handle(invocation)
  -> PostToolUse contributors/hooks
  -> lifecycle terminal outcome
  -> ToolOutput 转 FunctionCallOutput
  -> ContextManager 裁剪并记录
  -> 下一次模型 sampling
```

[dispatch_any_with_terminal_outcome](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/tools/registry.rs#L495) 保证无论成功、失败、阻断还是取消，都给 lifecycle contributor 一个 terminal outcome。

## 5. 工具错误的两条通道

参数不合法、非零退出码、文件不存在、MCP 业务错误等可编码成 tool output，让模型修正。handler panic、registry invariant、join error、channel 关闭表示 harness 异常，应终止 sampling/turn，不能伪装成普通失败。

## 6. 命令输出第一层限流

unified exec 使用 [HeadTailBuffer](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/unified_exec/head_tail_buffer.rs#L11)，默认总上限 1 MiB：

- 50% 固定给 head，填满后不改变；
- 50% 给 tail，用 `VecDeque` 保留最新字节；
- 超出时丢中间并累计 `omitted_bytes`；
- 输出时在 head/tail 间插入省略标记；
- 多次 drain 的 buffer 可合并，仍保持容量上界和省略计数。

该算法保留命令开头上下文和结尾错误/摘要，比只留单侧更适合构建日志。

## 7. 命令输出第二层模型裁剪

`ExecCommandToolOutput` 默认模型预算 10,000 tokens，并取“调用方请求上限”和“模型 policy”中更小者。`truncated_output_with_policy`：

1. retained bytes 转 UTF-8 文本。
2. 执行期已省略则生成 bytes omitted marker。
3. 仍超预算时做中间文本截断。
4. 增加原始 token count 和 warning。
5. `response_text` 预留 metadata/header 空间，循环收缩 output budget，避免 history 再次破坏输出。

一次超大输出可能经历“1 MiB head/tail”和“约 10k token 模型裁剪”两次，但 marker 检测避免重复提示。

## 8. 通用 tool output 裁剪

[truncate_function_output_items_with_policy](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/utils/output-truncation/src/lib.rs#L109) 按剩余预算处理：

- 空 text 丢弃；
- 完整 text 先到先得；
- 第一个超限 text 保留头尾片段；
- 后续 text 只计 omitted；
- image 保留；
- audio 按估算 token 收费；
- encrypted content 原样保留；
- 最后追加 omitted item 数。

`ContextManager` 在历史入库时再执行该策略，默认预算乘 1.2 给 envelope 留空间。

## 9. 长文件读取

### 9.1 普通仓库源码

固定版本 core 没有统一的本地源码 `read_file` handler。常规阅读由 shell/exec 执行 `sed`、`cat`、`rg`，所以长文件先受 1 MiB head/tail，再受模型 token 裁剪。

可靠做法是模型主动分页，例如 `sed -n '1,240p'`，而不是一次 `cat` 后相信中段完整。prompt 提示这种行为，runtime 不会自动逐页读普通仓库文件。

### 9.2 Memories read

Memories extension 的 [read](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/ext/memories/src/local/read.rs#L12)：

1. 校验 `line_offset != 0`、`max_lines != 0`。
2. `resolve_scoped_path` 限定根目录。
3. 拒绝 symlink，要求 regular file。
4. 用 `read_to_string` 一次读取全文件。
5. 由行偏移算 start/end byte。
6. 对切片按 `max_tokens` 中间截断。
7. 返回 start line 和 `truncated`。

它支持分页语义但不是流式读取，超大文件仍有整文件内存成本。

### 9.3 History/Notes

history read 有 `limit_chars`，list items 有 `max_chars_per_item`；notes read 支持 1-based `start_line`，负数从末尾计数。它们属于扩展后端，不是 core 通用读工具。

## 10. 大输出是否落盘

普通 exec/tool output 没有统一“超长部分自动 spill 到 artifact，再给模型路径”的机制。exec 丢弃中间并保存 head/tail；history 保存裁剪后的模型版本。具体工具可自行返回 resource link/artifact，但不是 registry 通用行为。

因此 omission marker 只证明有内容丢失，不能恢复。需要完整结果时要分页、重定向到文件后分段读取，或使用工具自己的 artifact 能力。

## 11. 后台命令

命令在 yield 内结束则直接返回，否则返回 session id，后续 stdin/poll。buffer 跨 drain 保持上界。默认最多 64 个 unified exec process；事件侧最多 10,000 个 output delta，防止单命令无限淹没 UI。

## 12. 重复调用同一工具

### 12.1 已实现

- registry 拒绝重复工具定义；
- prompt 提醒成功 apply_patch 后不要无意义重读；
- call id 保证调用/结果配对；
- Guardian 对连续审批拒绝熔断；
- Goal 对跨 turn exec 失败设三次阈值；
- token/context/cancellation 最终限制运行。

### 12.2 未实现

没有找到通用 `hash(tool_name, normalized_args)` 计数器，也没有“同一普通工具连续失败 N 次自动停止”。模型可重复相同失败调用，直到改变策略、达到其他预算、被中断或 turn 出错。

### 12.3 应如何补强

不应简单禁止重复，因为轮询、分页和瞬时重试是合法的。应记录调用指纹、结果摘要、工作区版本和失败类别；只有“相同前置状态 + 相同参数 + 相同失败”连续出现时才警告或熔断。

## 13. 连续失败专项机制

Guardian 标准策略同 turn 连续拒绝 3 次，或最近窗口拒绝达到阈值时中断；cyber 模型更严格。

Goal 只统计默认 namespace 的 `exec` 且 handler 已执行的失败。活动 goal 的一个 turn 没有成功工具但发生 exec failure，则计一次；同一 goal 连续 3 个失败 turn 后可标记 blocked。任意成功工具重置计数。这不是通用 tool loop breaker。

## 14. 测试证据

- `head_tail_buffer_tests` 覆盖大 chunk、跨 chunk、零预算、merge 和 omission count。
- truncation tests 覆盖 UTF-8、bytes/tokens、image/audio/encrypted content。
- `output_collection_stays_bounded_across_repeated_drains` 证明多次 drain 仍有界。
- context tests 验证 response header 预留后不二次破坏输出。
- registry tests 覆盖 success/failure/blocked/aborted terminal outcome。

## 15. 设计评价

执行期内存上限与上下文 token 上限分离是正确的。主要缺口是没有统一 artifact spill 和无进展检测。InnoAgent 可增加 `ToolResultRef`：上下文保留摘要、head/tail、hash 和 artifact URI，原始内容按策略落盘；再增加状态感知的重复调用 detector。
