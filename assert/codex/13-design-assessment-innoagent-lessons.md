# 13. 设计评价与 InnoAgent 借鉴

## 做得好的部分

1. **领域协议与表面分离**：TUI、exec、App Server 和 SDK 不各自实现 Agent loop。
2. **串行控制面**：submission loop 给 Session 一个清晰的状态修改入口，同时允许工具并行。
3. **分层安全**：approval、exec policy、Guardian、sandbox 和 hooks 解决不同问题，断连默认拒绝。
4. **工具生命周期完整**：校验、pre hook、started、handler、post hook、terminal outcome、telemetry 可审计。
5. **存储抽象务实**：ThreadStore 把运行时与 local/in-memory 实现隔离，rollout 可用于恢复。
6. **协议工程成熟**：typed request/notification、schema 生成、SDK artifact 和大量 integration test。
7. **传输韧性**：Responses WebSocket/HTTP fallback、retry、idle timeout 与错误分类明确。
8. **扩展方向正确**：typed contributor registry 正逐步减少 feature 直接侵入 core。

## 主要风险与技术债

1. `core`、`tui`、`app-server` 仍然巨大，拆 crate 没有消除中央编排复杂度。
2. `run_turn`、sampling、ToolRegistry dispatch、MessageProcessor dispatch 是高认知热点。
3. legacy SandboxPolicy 与 split permissions 并存，有损投影可能造成 UI 与实际权限不一致。
4. v1/v2 protocol、generated schemas、两种机制不同的 SDK 形成同步矩阵。
5. rollout JSONL、SQLite projection、trace bundle 增加迁移、重建和隐私治理成本。
6. PostToolUse 无法撤销副作用，若产品文案称“hook 可阻止工具”会产生错误安全预期。
7. 平台 sandbox 与 conditional compilation 让本地通过不代表其他平台正确。
8. plugin/MCP/skill/hook/extension 概念重叠，信任与供应链模型需要统一呈现。

## 面向 InnoAgent 的分阶段路线

### P0：先建立不变量

- 定义 `Thread/Turn/Item/Event/Op` 稳定协议，不让 CLI 类型进入 core。
- 让 LangGraph 主循环只通过单一 Session actor 修改状态。
- 为工具建立 registry、typed args/result 和 started/completed/failed 事件。
- 审批 channel 断开必须拒绝；approval 与 filesystem/network sandbox 分开建模。
- 选择一个 append-only 事实源，查询数据库只做可重建投影。

### P1：补齐可运营能力

- 配置值保存来源和作用域，thread/turn override 明确。
- 统一 request id、turn id、tool call id，贯穿日志、事件和持久化。
- 对 prompt/context assembly 做结构化快照和 token budget。
- 建立 App Server 风格 typed RPC，由 CLI/Web/SDK 复用。
- 增加恢复、fork、interrupt、compact 的端到端契约测试。

### P2：谨慎扩展

- 先提供 tool/context/lifecycle/storage 四类 contribution point，再考虑 plugin marketplace。
- MCP 工具必须进入统一 policy/telemetry 管道。
- 多 Agent 采用有界 DAG、父子预算、独立取消，避免递归自由生成。
- Memory consolidation 使用隔离权限，并防止把不可信历史升级为永久指令。

## 不建议照搬

InnoAgent 当前规模不需要复制 126 个 crate、双 SDK 机制、三套状态表示或完整云/实时能力。应复制不变量和边界，而不是目录数量。最优先的成功指标不是“功能数量”，而是任一 tool call 都能回答来源、权限、审批、隔离、结果、持久化和恢复语义。
