# 10. MCP、Skills、Plugins、Hooks 与 Memory

## 1. 模块职责

扩展系统让第三方能力进入 Agent，同时保持发现、提示、执行、生命周期、权限、持久化边界。Codex 不只有 MCP；skills、plugins、hooks、context contributors 和 memory 分别解决不同问题。

## 2. MCP 生命周期

```text
读取 MCP 配置 -> 启动 server -> initialize/能力协商
  -> list_tools/list_resources -> 转 ToolSpec
  -> 稳定排序并缓存 -> 暴露给 ToolRouter
  -> call_tool -> content/result 转 FunctionCallOutput
```

工具排序必须稳定，否则每轮 `tools` 数组变化会破坏 prompt cache。刷新 MCP server 会更新 tool list，因此属于显式上下文变化。

核心对象包括 MCP runtime/client、server config、tool metadata cache、`McpHandler`、`ExtensionRegistry`、`PluginManifest`、`SkillMetadata`、`PromptFragment` 和 lifecycle contributors。它们分别拥有连接、发现结果、执行适配和 prompt 注入状态。

## 3. MCP 结果

MCP content 可含 text、image、audio、resource link 和 embedded resource。Codex 转成统一 content items，再应用 output truncation。MCP begin/end 事件服务 UI；执行权限取决于 MCP server 进程，不能假设 Codex shell sandbox 自动覆盖。

MCP 调用失败要区分 transport error、protocol error、server tool error 和结果转换失败。只有最后形成的 function output 才进入模型历史；begin/end 事件还要在失败时成对闭合。

## 4. Skills

Skill 是按需加载的指令与资源包。初始 prompt 注入名称、描述、路径和使用规则；模型判断匹配后读取 `SKILL.md`。两阶段机制避免所有正文常驻上下文。

Skill scope、重名和优先级必须确定。路径是读取入口，也意味着要限制 skill 来源和更新。

## 5. Plugins 与 ExtensionRegistry

Plugin 可打包 skills、MCP 配置、hooks 和贡献项。`PluginsManager` 负责发现、启用和缓存，plugin manifest 是安装契约，不是执行授权。

`ExtensionRegistry` 聚合 tool、context、lifecycle、configuration contributor。核心面向 trait，不对每个扩展硬编码。每个 contribution 应保留 provenance，否则无法定位是谁注入工具或 prompt。

实现入口：[ExtensionRegistry](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/ext/extension-api/src/registry.rs)、[McpRuntime](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/codex-mcp/src/runtime.rs)、[PluginsManager](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/core-plugins/src/manager.rs#L524)。

## 6. Hooks

- PreToolUse：副作用前允许/阻断。
- PostToolUse：终态审计和结果处理。
- turn start/stop/interrupt：观察任务生命周期。
- executor hooks：对子 Agent 或特定执行器运行。

hook 会失败、超时或取消。运行时记录 started/completed，并明确 fail-open 或 fail-closed；安全 hook 默认应 fail-closed。

tool lifecycle contributor 无论 handler 是否被调用都收到 terminal outcome，其中 `handler_executed` 区分前置阻断与执行后失败。Goal、analytics 和 Guardian 等扩展依靠这个字段避免错误计数。

## 7. Memory

Memory 与 conversation history 不同。history 是当前 thread 的因果记录；memory 是跨 turn/thread 提炼的长期信息，通过 contributor 选择性注入。

本地 memory read 有 scoped path、symlink 拒绝、line offset、max lines 和 token 截断。生成 memory 时不能把不完整工具输出或未验证推断当事实。

## 8. History/Notes

history 工具提供 list windows/items、read item、search；notes 提供 list/read/search/append/write。参数有显式 limit，结果还经过统一 truncation。这说明“让模型查询历史”比把全部历史常驻 prompt 更可扩展。

## 9. 安全边界

- schema 不等于参数安全；
- plugin 启用不等于所有动作授权；
- MCP server 系统权限是独立信任边界；
- skill 文本应显示 provenance；
- 事后 hook block 不能当撤销；
- memory 要区分用户确认事实、模型摘要和环境观察。

## 10. 测试与评价

测试覆盖 MCP discovery/call、名称归一化、tool list 稳定性、plugin contributor、skill render、hook terminal outcome 和 memory scoped read。InnoAgent 应统一为带 provenance 的 contribution graph，并在 prompt/tool inspector 中展示最终合并结果。
