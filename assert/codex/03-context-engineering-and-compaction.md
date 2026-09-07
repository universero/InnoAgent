# 03. 上下文工程与压缩

## 1. 模块职责

上下文模块解决的不是“把聊天记录拼成字符串”，而是把来源不同、优先级不同、生命周期不同的内容投影为一次 Responses 请求，同时满足指令优先级、工具配对、历史恢复、容量控制、动态更新和 prompt cache 稳定性。

## 2. 上下文分布在哪里

### 2.1 模型基础指令

来自 model catalog 的 `base_instructions`，也可由 `model_instructions_file` 覆盖，最终进入 Responses 的 `instructions`。

### 2.2 权限与运行模式

sandbox、approval policy、collaboration mode 被渲染为 developer 级上下文，告诉模型哪些动作可执行、何时请求升级。这只是提示层约束，真正强制仍在 runtime。

### 2.3 用户 developer instructions

`config.toml` 的 developer instructions 作为独立 developer message 注入，不能与用户消息混为同一角色。

### 2.4 项目指令

`AGENTS.override.md`、`AGENTS.md` 和 fallback 文件按项目根到 cwd 的层级发现。越接近 cwd 的内容越具体，组合时放在后面。项目文档有总字节上限，防止仓库规则占满窗口。

### 2.5 Skills 和扩展上下文

默认只注入 skill 元数据和使用说明，不把所有 `SKILL.md` 正文都塞进 prompt。真正使用时再读取技能文件。plugins、memory、extensions 可通过 context contributor 向指定 slot 注入片段。

### 2.6 环境上下文

cwd、shell、平台能力、日期、workspace roots、权限 profile 等以结构化标签渲染，并随 cwd 或权限变化更新。

### 2.7 会话历史

`ContextManager` 持有 `ResponseItemEnvelope` 列表，包括用户消息、assistant message、reasoning、tool calls、tool outputs、compaction item 和 metadata。

### 2.8 当前 turn 动态输入

用户新输入、steering、mailbox、图片、tool output、配置更新和 world-state delta 在运行中追加，成为后续 sampling 的输入。

## 3. 组装顺序

```text
Session 配置
  -> record_context_updates_and_set_reference_context_item
  -> 用户输入转 ResponseItem
  -> ContextManager.record_items
  -> clone_history().for_prompt(input_modalities)
  -> normalize_history
  -> build_prompt(input, StepContext, BaseInstructions)
  -> Prompt { input, tools, parallel_tool_calls, instructions, output_schema }
  -> ModelClientSession::stream
```

[build_prompt](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/session/turn.rs#L1387) 本身很短，复杂工作发生在此前的 context contributor 和 history normalization。

## 4. `ContextManager` 的状态

`ContextManager` 不只是 `Vec<ResponseItem>`，还维护带 harness metadata 的 items、review history、retained user context、用户消息 revision、compaction 边界和 prompt normalization 标识。

[record_items_with_metadata](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core/src/context_manager/history.rs#L335) 先过滤非 API message，再复制 item 和 metadata。遇到 function/custom tool output 时立即按 truncation policy 裁剪，之后才写入 history。

## 5. 工具输出何时进入上下文

工具 handler 完成后生成 `FunctionCallOutput` 或 custom output。结果经过两层处理：

1. handler/output 对象先按自己的 max output tokens 生成模型版本。
2. `ContextManager::record_items_with_metadata` 再按当前模型 policy 裁剪后持久化。

默认 history 裁剪预算是模型 policy 的 `1.2` 倍，为序列化标记留余量；metadata 可给 fallback token limit override。裁剪发生在写入历史时，所以 resume 看到的是受控版本，而不是重新载入原始无限输出。

## 6. history normalization

`for_prompt` 复制 history 后调用 `normalize_history`，修复 Responses API 的结构不变量：

- tool call 应有对应 output；
- 不支持的 media 从模型投影移除；
- local shell/tool 表示转成 provider 接受的形式；
- 缺失输出生成 prompt-only synthetic output；
- 合成输出 id 稳定，重复投影不能改变顺序或 id；
- 内部 item 不发送给模型。

稳定 synthetic id 会直接影响 prompt cache。如果 retry 时生成随机 id，即使文本相同也不再是精确前缀。

## 7. 上下文优先级

```text
system/provider controlled
  > developer/base instructions
  > developer/permissions and user config
  > user/project instructions and skills metadata
  > user/environment context
  > conversation user/assistant/tool history
```

“数组中更靠后”只在相同角色和来源体系内表示更具体，不能覆盖更高角色。运行时安全也绝不能依赖角色优先级。

## 8. 运行中如何变更

### 8.1 新用户输入

转换为 `ResponseItem::Message(role=user)`，记录 history；有授权意义的消息还进入 retained user context。

### 8.2 工具结果

按 call id 回填，裁剪后追加，触发下一次 sampling。

### 8.3 steering 和 mailbox

先进入队列，在 step 边界并入上下文，不修改已经发给 provider 的 request。

### 8.4 cwd、模型、权限

新的 StepContext 反映更新值并生成 configuration/world-state update。改变模型、工具列表、cwd、权限或 sandbox 会破坏稳定前缀，必须作为显式上下文变化记录。

### 8.5 rollback

`ThreadRollback` 丢弃最后 N 个 user turn 的内存历史，但不撤销文件系统副作用。这是历史操作，不是工作区事务回滚。

## 9. 三层裁剪算法

### 9.1 单段文本

`truncate_text` 支持 bytes 或 approximate tokens，采用中间截断，保留 head 和 tail，并插入 `???N chars/tokens truncated???`。保留尾部是为了不丢命令最终错误和总结。

### 9.2 多内容 tool output

`truncate_function_output_items_with_policy` 顺序处理 text/image/audio/encrypted content：

- 空文本丢弃；
- 文本在预算内完整保留，超限则对当前文本中间截断；
- image 不按文本预算简单删除；
- audio 按估算 token 成本收费，超预算计入 omitted；
- encrypted content 保留，客户端不能安全重写；
- 最后追加 omitted text/audio item 摘要。

### 9.3 整体上下文

历史接近模型窗口或用户显式 `Compact` 时，系统运行本地或远端 compaction。它不是删除最老 N 条，而是生成可继续工作的摘要/compaction item，并安装为新历史边界。

## 10. compaction 生命周期

```text
估算 prompt tokens
  -> 达到 auto-compact threshold
  -> 构造 compaction prompt / remote request
  -> 生成摘要或 ContextCompaction item
  -> 保留必要前缀和用户约束
  -> install 到 ContextManager
  -> 写 rollout
  -> EventMsg::ContextCompacted
  -> 用新 history 继续采样
```

“install”是语义提交点：摘要生成但未安装，不应改变 live history。远程压缩实现专门保留重复 developer/context prefix，使 reducer 能表达模型实际看到的顺序。

## 11. 压缩保留项

- 当前任务目标和最新用户约束；
- 已完成的关键修改与验证结果；
- 尚未完成的计划；
- 已授权或拒绝的安全决定；
- 工具调用与输出的因果摘要；
- 恢复所需路径、命令、错误和外部句柄；
- 最近 turn 的精确信息。

raw stdout、大段已读源码、重复 reasoning 和过时计划适合压缩，但不能只保留“做过了”，否则 resume 无法验证。

## 12. prompt cache 约束

Codex 不依赖 `previous_response_id`，每次发送完整 input，以支持无状态 provider 和 ZDR。性能依赖精确前缀缓存，所以静态指令、工具定义和旧历史应稳定放前面。

工具列表或顺序、模型/base instructions、cwd、sandbox、approval policy、normalization id、compaction 都可能造成 cache miss。

## 13. 超限恢复

- 本地估算接近阈值：采样前主动 compact。
- provider 返回 context-length error：受限 compact/retry。
- 单工具输出过大：item 级裁剪，不必压缩整个 thread。
- compaction 自身失败：发明确错误，不能无限 compact loop。

## 14. 测试证据

- `record_items_respects_custom_token_limit` 验证工具输出按自定义 token limit 入库。
- `format_exec_output_reports_omitted_lines_and_keeps_head_and_tail` 验证头尾和省略提示。
- `for_prompt_assigns_stable_id_to_synthetic_output_without_reordering_history` 验证重复投影稳定。
- media normalization tests 验证模型不支持输入模态时的剔除。
- local/remote compact tests 验证摘要安装、前缀保留和 rollout 事件。

## 15. 设计评价

Codex 把“历史真相”和“模型可见投影”分开是正确方向。风险是裁剪发生在多个层，调试时很难回答模型究竟看到什么。InnoAgent 应保存 prompt manifest：每个 fragment 的来源、角色、优先级、原始/保留 token、裁剪原因和 hash。
