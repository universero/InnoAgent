# 工具与权限

## 可信执行链

```text
Model Tool Intent
-> Pydantic 参数校验
-> Guardrail preflight
-> Permission decision / user approval
-> Tool execution
-> Guardrail postflight
-> bounded ToolResult
-> Event + AgentState
```

模型只生成工具名和参数。`ToolRegistry` 是可信边界，统一处理未知工具、guardrail、并发、结果截断和错误归一化。

## 工具契约

每个工具应明确：

- Pydantic 输入 schema。
- 是否写操作、是否需要确认、是否允许并行。
- 简洁且可执行的模型描述。
- 结构化 `ToolResult`：`status`、`output`、`data`、`warnings`、`metadata`。
- 可观察的失败，而不是抛出模型无法处理的裸异常。

所有内置工具输入都拒绝未知字段，并为模型 schema 中的每个参数提供用途和边界说明。`read` 返回内容摘要哈希；`write` 可携带该哈希做乐观并发检查，并通过同目录临时文件原子替换目标。

首期工具包括 `read`、`write`、`ls`、`grep`、`shell`、`plan`、`task`、`skill` 和 `subagent`。提示词与工具实现放在同一文件的模块级常量中，便于能力和约束一起维护。

## 权限模型

| 模式 | 行为 |
|---|---|
| `ask` | 写文件和 shell 默认询问，命中持久 allow 时跳过 |
| `auto` | 工作区内自动执行，但 deny、路径边界和 guardrail 仍生效 |
| `readonly` | 阻止写工具和 shell |

审批选项：

- `allow_once`：只批准当前 pending call。
- `allow_always`：将规则写入当前仓库 `.innoagent/permissions.json`。
- `deny`：不执行，并把拒绝作为工具结果反馈给模型。

规则采用 deny 优先。文件规则按工具和规范化相对路径匹配；shell 规则按解析后的 argv 前缀匹配，避免纯字符串前缀误判。

## 并行策略

只有标记为 `parallel_safe` 且非写操作的工具进入线程池。写工具、shell、未知工具和可能共享状态的调用保持串行。执行结果按模型原始 call 顺序回填，而不是按完成顺序返回，保证模型上下文和事件稳定。

当前实现的“并行安全”是工具作者声明，不是自动冲突检测。新增工具时必须检查文件、进程、缓存和外部服务是否存在共享写入。

## 安全边界

- 文件工具通过 `allowed_roots` 限制工作区，解析路径后再判断，防止 `..` 和符号链接逃逸。
- `write` 执行前要求读取目标，降低盲覆盖风险。
- `write.expected_sha256` 会在替换前校验文件版本，发现并发修改时拒绝覆盖并要求重新读取。
- shell 使用本机 shell，不是安全沙箱；`auto` 只适用于可信仓库。
- shell 在 POSIX 系统中按进程组执行，超时后清理进程组，并保留已产生的标准输出、错误输出和退出状态。
- 工具输出有字符上限，避免一次结果耗尽上下文。
- API 返回成功不等于业务效果正确。高风险写操作未来需要稳定 `operation_id`、unknown-effect 对账和独立 verifier。
