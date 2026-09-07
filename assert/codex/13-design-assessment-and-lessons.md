# 13. 综合设计评价与 InnoAgent 落地建议

## 1. Codex 实际是什么

Codex 是以 thread/session 为状态边界、以 Responses 为模型语义、以 typed event 为协议、以 registry 承载工具、以多层权限和平台 sandbox 控制副作用、以 rollout 保持可恢复性的 Agent harness。

它不是单一 ReAct loop，也不是把 shell 暴露给模型的 CLI 包装。

## 2. 最值得复用

### 2.1 控制面串行，执行面并发

submission loop 给状态变化单一顺序，tool future 保留吞吐。InnoAgent 应采用相同分离，而不是给所有对象加锁。

### 2.2 四层语义分离

provider event、history item、domain event、client protocol 分层，减少后端和 UI 耦合。

### 2.3 历史与 prompt 投影分离

历史保留审计语义，prompt 根据模型能力归一化、裁剪和压缩。不能把“发给模型的数组”当唯一数据库。

### 2.4 权限多道门

tool visibility、PreToolUse、approval、Guardian、exec policy、sandbox 各自解决不同问题。

### 2.5 输出有界

采集期 head/tail、模型期 token 裁剪、上下文期 compaction 分层保护内存与窗口。

## 3. 不应照搬

- `try_run_sampling_request` 和 App Server dispatcher 过大，应拆为纯 reducer 加 effect executor。
- rollout、SQLite、trace、UI 多套表示存在漂移风险，应明确 authoritative log。
- 普通工具相同参数连续失败没有统一熔断。
- 普通 exec 丢弃中间输出且不统一落 artifact。
- legacy sandbox 与 granular permission 并存，应收敛为 capability lattice。

## 4. 推荐模块

```text
AgentRuntime
  SubmissionQueue / TurnCoordinator / SamplingReducer
  ToolScheduler / ContextEngine / EventBus

ToolPlatform
  ToolCatalog / ToolRegistry / AuthorizationEngine
  SandboxBackend / ResultStore / ProgressDetector

Persistence
  AppendOnlyEventLog / ThreadProjection / ArtifactStore

Surfaces
  AppServer / CLI-TUI / SDK
```

## 5. 落地顺序

### P0：正确性骨架

typed `Op/Event/ResponseItem`、submission 串行化、turn/step context、tool terminal outcome、approval fail-closed、append-only rollout、cancel 状态机。

### P1：安全和容量

platform sandbox、exec policy、head/tail buffer、item truncation、prompt accounting、auto compaction、artifact spill。

### P2：可扩展性

MCP、skill lazy load、plugin contributors、hooks、App Server、generated SDK。

### P3：高级治理

Guardian、多 Agent mailbox、goal accounting、state-aware no-progress detector、prompt inspector。

## 6. 重复调用治理

构造 `ToolAttemptKey`：

```text
tool namespace/name
+ canonical JSON args hash
+ workspace/environment revision
+ permission profile hash
```

保存最近结果的 success、error class、output hash、side-effect revision。只有 key 和结果相同、环境无变化时累计 no-progress；对 wait/poll/read pagination 豁免。达到阈值先把诊断反馈给模型，再次重复才中断。

## 7. 大输出治理

统一 `ToolResultRef`：summary、retained head/tail、original bytes/tokens/lines、sha256、artifact URI、MIME、truncation policy、sensitivity。模型默认拿摘要和引用，需要中段时再分页读取 artifact。

## 8. 上下文可解释性

每次 sampling 生成 prompt manifest：fragment id/source/role、ordering key、original/retained tokens、truncation reason、redaction 和 stable hash。出现行为差异时比较 manifest，而不是猜提示词。

## 9. 事件与持久化

domain event 作为 append-only authority，SQLite/UI 由 reducer 构建。每个 event 声明 correlation ids、durable/ephemeral、v1/v2 projection、redaction class、idempotency key 和 terminal relationship。

## 10. 最终评价

Codex 最成熟的部分是把 Agent 当成长期运行、会产生真实副作用的分布式状态机。InnoAgent 不需复制全部 crate，但必须复制这些不变量；只有 prompt + tool loop，而没有 submission、事件、权限、持久化和输出治理，无法达到同等级可靠性。
