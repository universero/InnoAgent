# 10. 扩展、MCP、Skills、Plugins、Hooks 与 Memory

## Extension Registry

[`ExtensionRegistry`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/ext/extension-api/src/registry.rs#L1) 是安装完成后的 immutable typed registry，builder 在启动阶段收集贡献。贡献面包括 thread/turn/tool lifecycle、config、token usage、skills、context、MCP server、turn input/item 和 tools。immutable 快照让一轮 turn 的能力集合稳定，也减少扩展运行中修改全局表的竞态。

`ext/*` 已包含 agent、connectors、goal、guardian-v2、history-notes、image-generation、items、mcp、memories、queue、skills、web-search 等产品能力。它不是完全第三方 ABI，而是 workspace 内的结构化模块化机制。

## MCP

`codex-mcp` 管 runtime、catalog、connection set、tool discovery/call、resources、events 和 client extension；`rmcp-client` 管 stdio、streamable HTTP、OAuth 和 elicitation。工具 catalog 会结合 server metadata、environment generation、分页和可见性过滤；连接失败与单个 server 工具错误需隔离，不能让全部 turn 启动失败。

MCP 工具最终进入统一 ToolRouter，因此仍经过工具生命周期、审批/权限和 telemetry，而不是旁路执行。MCP resource 与 tool 是不同能力，资源可以进入 context，工具产生显式 call/output。

## Skills

旧 `skills` crate 提供解析、安装资源和示例；新 `ext/skills` 引入 host/executor catalogs、动态选择、BM25 selector、资源 list/read 与预算化渲染。Skill 本质是可发现的指令/资源包，不应自动获得高权限。选择器减少把所有 skill 全量塞进 prompt 的 token 成本。

## Plugins

`plugin` 定义公共模型，`core-plugins` 管 manifest、marketplace、remote bundle、installed store、policy projection、skills/MCP/hooks/tools、script attribution 与 cache。plugin 可聚合多种贡献，因此安装、启用、信任和运行权限必须分开。manifest fallback 与 remote catalog cache 增加兼容性，也增加供应链校验需求。

## Hooks

`hooks` 支持 pre/post tool、permission request、compact、session/turn lifecycle，handler 可为命令或 MCP。Pre hook 可阻断；Post hook 不可回滚副作用。hook timeout、输出 schema、错误策略和来源 attribution 必须可观察，否则 hook 会成为隐蔽控制面。

## Memories

Memory pipeline 分两阶段：Phase 1 并发从 rollout 提取候选，使用 DB lease/backoff；Phase 2 获取全局锁，同步 memory workspace、生成 diff，并启动无网络、无审批的内部 consolidation agent。这个设计避免多个进程同时改全局 memory，但必须防 prompt injection 从历史提升为永久指令。

## Goal 与 Queue

Goal extension 保存状态、token budget、continuation/steering，并暴露 create/update 工具；ephemeral/review subagent 隐藏 Goal 工具，避免子任务改写顶层目标。Queue 将 thread 后续任务持久化和排序，独立于当前 turn 输入。

## 评价

Codex 正从“core 内 feature”迁移到 contributor registry，但 plugin、extension、MCP、skill、hook 五套概念的边界仍复杂。InnoAgent 可先统一成 Capability manifest，再按 tool/context/lifecycle/storage 四类贡献点展开。
