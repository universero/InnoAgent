# 19. 存储与恢复内部实现

## 1. 数据模型与事实源

Codex 将“可恢复事件”“可查询索引”“调试轨迹”分开：

- `rollout` 保存追加式 JSONL，包含 session metadata、用户/模型 item、工具结果、compact 和状态事件。
- `state` 保存 SQLite 表，包括 thread metadata、projects、sections、queue、goal、memory、artifact、migration/audit/log。
- `thread-store` 将二者组合成业务接口，并提供 in-memory 后端。
- `rollout-trace` 记录调试 trace 和 reducer 状态，不是 resume 的唯一来源。
- `message-history` 是跨会话输入历史，不包含完整 Agent trajectory。

设计原则是 rollout 接近 append-only source of truth，SQLite 为可重建查询投影。否则任一写入一半成功都会出现双主冲突。

## 2. ThreadStore 契约

[`ThreadStore`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/thread-store/src/store.rs#L68) 是 async trait，能力可按五组理解：

| 能力 | 操作 | 实现关注点 |
|---|---|---|
| 生命周期 | create/resume/fork/discard/shutdown | writer 唯一性与资源关闭 |
| 写入 | append/flush/metadata update | 顺序、durability、投影更新 |
| 读取 | read/history/turns/items | live writer 路径优先、分页稳定性 |
| 组织 | archive/delete/project/section | 文件与 DB 的跨资源一致性 |
| 检索 | list/search/occurrences | SQLite 索引、cursor、过滤条件 |

trait 还暴露 capability，例如是否支持 paginated history。App Server 在创建 thread 前检查能力，不把 unsupported 延迟到运行中失败。

## 3. LiveThread

[`LiveThread`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/thread-store/src/live_thread.rs#L36) 将 rollout writer 与 metadata observer 绑定。`LiveThreadInitGuard` 处理初始化失败时的清理，防止 writer lock 泄漏。append items 后 observer 更新 SQLite metadata；raw append 可明确绕过投影，因此不能作为普通业务写入。

关键状态：

- **unmaterialized**：thread 已准备但尚无需要落盘的内容；shutdown 不应制造空会话。
- **materialized**：rollout 文件和 metadata 已建立。
- **live writer held**：同一 thread 防止跨进程或同进程重复 writer。
- **shutdown**：flush buffer、释放 lock，之后不可追加。
- **discard**：未物化 thread 可无痕丢弃。

测试明确覆盖 duplicate writer、cross-process writer、buffered shutdown、external rollout path 和 inherited items 不污染 metadata。

## 4. Local Store 子模块

| 子模块 | 具体职责 |
|---|---|
| `create_thread` | 校验 cwd、创建 pending metadata/live writer |
| `live_writer` | append、flush、lock、rollout path |
| `writer_lock` | 防多 writer，处理 stale/跨进程冲突 |
| `read_thread` | 合并 metadata、history 与 live path |
| `thread_history` | 历史解析与分页 |
| `thread_history_materialization` | 从 rollout 投影可查询 history |
| `thread_rollout_resolver` | 显式路径、索引路径和 fallback 选择 |
| `rollout_lineage` | resume/fork 来源关系 |
| `paginated_fork` | 从分页历史边界创建 fork |
| `pending_thread_metadata` | 首次实际写入前暂存 metadata |
| `update_thread_metadata` | 标题、时间、来源等投影更新 |
| `list_threads`, `search_threads` | cursor/filter/full-text 查询 |
| `archive_thread`, `unarchive_thread`, `delete_thread` | 生命周期与文件搬移/清理 |
| `revert_thread` | 截断/恢复到历史点并保持一致性 |
| `projects`, `thread_sections` | 组织层级与排序 |
| `rollout_migration` | 旧布局/schema 迁移与恢复 |
| `model_context` | 可恢复的模型上下文读取 |

## 5. Rollout 文件

[`RolloutRecorder`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/rollout/src/recorder.rs#L86) 负责创建、追加、flush 和关闭 JSONL。`rollout_file_name` 编码时间/id，`metadata` 读写头部信息，`ordinal` 提供稳定排序，`reverse_jsonl_scanner` 从尾部高效查找，`seekable_reader` 支持分页，`search` 和 `list` 提供发现。

`compression` 处理历史文件压缩，`maintenance` 清理/迁移，`session_index` 与 `rollout_reference_index` 加速定位，`state_db` 把 rollout 信息同步到 SQLite，`persistence_metrics`/`sqlite_metrics` 记录写入与查询健康。

崩溃一致性要求 reader 容忍尾部半行，但中间损坏必须显式报告；append 成功而 metadata 投影失败时，恢复应以 rollout 重建，而不是丢弃事实。

## 6. SQLite State

`state/sqlite.rs` 建立连接与 pragmas，`migrations.rs` 管 schema version，`runtime.rs` 暴露业务 API。model 子目录包含 thread metadata、project、queued item、goal、memory、artifact、graph、log 和 rollout migration state。

runtime 子模块分别处理 threads、projects、sections/order、queued items、goals、memories、logs、remote control、external migration、rollout migration、recovery/backfill。`audit` 记录关键状态变更，`telemetry` 记录 DB 延迟/错误，`extract` 从 rollout 提取投影数据。

SQLite 事务保护单库操作，但文件移动与 DB update 不是天然原子事务，因此 archive/revert/migration 需要幂等步骤和 recovery 标记。

## 7. Resume 算法

1. 根据显式 rollout path、thread metadata 或扫描结果定位文件；显式路径优先，避免 stale SQLite path。
2. 读取 session metadata 和 history，校验 cwd/来源/schema。
3. 必要时迁移旧 rollout，或从 JSONL 重建缺失 metadata。
4. 获取 live writer lock，禁止同时恢复两次。
5. 先加载历史，再启用 metadata observer，避免 inherited history 被误认为新活动。
6. 创建 Session/CodexThread，恢复 UI/app-server metadata 与 runtime config。
7. 新事件继续追加到同一逻辑 thread。

恢复绝不能重放历史 command/tool；它只恢复对话状态。未完成外部 operation 必须通过 operation id 查询/协调，而不是再次提交。

## 8. Fork、Revert、Rollback

- **fork**：复制选定历史前缀到新 thread，保留 lineage，新旧线程后续独立。
- **revert**：改变 thread 可见历史/rollout 到指定点，通常需要新持久化边界。
- **rollback**：面向运行态/工作区的回退操作，语义由上层 handler 决定。

三者不能复用一个“截断数组”实现，因为 id、文件副作用、metadata、搜索索引与 UI 恢复语义不同。

## 9. 其他存储 crate

`attachment-store` 以内容/引用方式管理媒体，避免 rollout 膨胀；`agent-graph-store` 保存父子 Agent 和状态；`message-history` 使用单次 append write 降低多进程写交错；`memories/read` 与 `memories/write` 通过 state DB lease/lock 协调长期记忆。

## 10. 必测不变量

- 任一 thread 同时最多一个 live writer。
- flush 返回后数据可被新进程读取。
- SQLite 删除/损坏后可从 rollout 重建核心 metadata。
- pagination cursor 在并发追加时不重复、不跳项。
- archive/unarchive 可重试且不产生双份 thread。
- fork 不继承 active task/pending approval。
- resume 不重放工具副作用。
- delete 明确处理附件、trace、graph 和 project 引用。

## 11. Thread Store 根模块逐项

| 模块 | 具体职责 |
|---|---|
| `error` | 定义 unsupported、not found、writer conflict、I/O/DB 等稳定错误。 |
| `in_memory` | 用内存数据结构实现同一 trait，服务无磁盘测试和嵌入场景。 |
| `live_thread` | 绑定 writer、observer、metadata 和显式关闭协议。 |
| `local` | 组合 rollout 文件、SQLite state 和本地锁的生产实现。 |
| `projects` | project 对象、排序与 thread 归属接口。 |
| `queue_store` | thread queue item 的 CRUD、排序与启动状态。 |
| `store` | 定义 ThreadStore trait、capabilities 和工厂。 |
| `thread_metadata_sync` | 从 rollout item 同步 title、时间、cwd 等 metadata。 |
| `thread_sections` | section CRUD、顺序和 thread move。 |
| `types` | page/cursor/filter/thread summary 等共享类型。 |
