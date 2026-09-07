# 08. 存储、Rollout 与恢复

## 三种状态表示

1. **rollout JSONL**：按时间追加会话事件/item，适合恢复、审计和迁移，是最接近事实日志的表示。
2. **SQLite state/thread projection**：提供 thread 列表、搜索、分页、项目、section、metadata 等查询，不必每次扫描 JSONL。
3. **rollout trace bundle**：面向调试/回放的独立 trace/reducer，不等于用户会话存储。

把三者混称“历史记录”会导致错误设计。JSONL 与 SQLite 之间存在重建、migration、fallback 和一致性窗口；trace 可包含更细调试信息，生命周期与隐私策略也应不同。

## ThreadStore

[`ThreadStore`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/thread-store/src/store.rs#L1) 是 storage-neutral trait，约 43 个能力方法，覆盖 create/open、append/flush/shutdown、history/page、metadata、archive/delete、projects/sections/search。local 和 in-memory 实现让 core 测试不依赖真实磁盘，也为未来远端 store 留出边界。

`LiveThread` 封装活动 writer、metadata 更新、flush/shutdown/discard/read。显式 shutdown 很重要：仅依赖 Drop 无法可靠等待异步写入。Session teardown 即使由 channel 异常触发也尝试关闭持久层。

## Resume 与 Fork

resume 读取 rollout，恢复 thread metadata、历史 items、模型/环境相关状态，再创建新活动 Session。恢复不应重放历史工具副作用，只重建模型上下文和 UI 状态。fork 选择历史前缀产生新 thread，保留可追溯来源但后续持久化独立。

最新基线还使用 App Server metadata 改善 TUI session restoration，说明“恢复 UI 所需字段”不应从自由文本历史猜测，而应存成结构化 metadata。

## Message history 与附件

`message-history` 另维护 `~/.codex/history.jsonl`，面向输入历史而不是完整 rollout；单次 append write 降低多进程交错风险。`attachment-store` 管图片/文件生命周期，避免大二进制内嵌每条事件。`agent-graph-store` 保存多 Agent 关系，不应塞入普通 message history。

## 故障模型

- JSONL 尾部可能因崩溃截断，reader 应容忍最后一条损坏。
- SQLite 投影可能落后于 rollout，需要可重建而非双主写入。
- archive/delete/move 必须协调文件与数据库，失败要可重试。
- schema migration 要覆盖旧版本 rollout 和 metadata 缺失。
- 并行进程追加历史只能降低交错，不能替代完整事务语义。

InnoAgent 应明确一个事实源，其余皆为可重建投影，并给每个写入事件稳定 id 和 schema version。
