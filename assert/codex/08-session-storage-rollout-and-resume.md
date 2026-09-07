# 08. 会话存储、Rollout 与恢复

## 1. 模块职责

持久化模块要在进程退出、客户端断连、版本升级和 thread fork 后恢复 Agent 语义，同时不尝试序列化不可恢复的 runtime 句柄。

## 2. 三种状态表示

- 内存 Session：实时、含 task/channel/mutex，不可直接持久化。
- rollout JSONL：按时间记录 session metadata、ResponseItem 和事件，是重放来源。
- SQLite/thread store：面向 list/search/read/archive/project 的查询投影。

三者不是副本。内存负责运行，rollout 负责因果历史，SQLite 负责索引和产品查询。

核心对象包括 `RolloutRecorder`、`RolloutItem`、`ThreadStore`、`LiveThread`、thread/turn/item records 和 pagination cursor。存储 API 不暴露 Session mutex，而通过不可变记录和投影读取。

## 3. 写入链路

```text
用户输入 / model item / tool output / lifecycle
  -> Session record_conversation_items / send_event
  -> rollout recorder 追加 JSONL
  -> thread-store 更新 metadata/index
  -> App Server 通知客户端
```

只有已经越过语义提交点的 item 才应进入可恢复历史。流式 delta 是展示状态，完整 `ResponseItem` 才是重放基础。

## 4. rollout

rollout 使用追加式记录，避免每次 turn 重写整个会话。记录包含 thread/session metadata、响应 item、compaction、配置变化和必要事件。追加模型适合崩溃恢复，但需要 schema version 和对未知 record 的兼容策略。

## 5. ThreadStore 与 LiveThread

[ThreadStore](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/thread-store/src/store.rs#L68) 提供 thread、turn、item、section、project 和分页查询。`LiveThread` 把活动 session 与持久化 thread 视图连接起来。

store 接口允许不同调用表面共享列表和搜索，不必各自扫描所有 JSONL。

## 6. resume

resume 读取 rollout 和 metadata，重建历史、配置基线和 thread identity，再创建新的 Session runtime。它不能恢复：

- 已断开的 SSE/WebSocket；
- 内存中的 tool future；
- pending oneshot approval；
- 未持久化的 UI delta；
- 已丢失的外部进程句柄。

归一化会为不完整 tool call 补可接受输出，避免下一次 Responses 请求违反 call/output 配对。

恢复链路还要重新解析当下配置和模型目录。历史事实沿用旧记录，未来行为使用新 Session resolved config；若模型或工具目录变化，必须生成显式 configuration update，不能假装 prompt 前缀未变。

## 7. fork、archive、delete

- resume：沿用 thread identity 和历史继续。
- fork：复制选定历史到新 thread identity，之后独立发展。
- archive：可恢复的可见性/状态变更。
- delete：破坏性删除，语义不同于 archive。

客户端不能把四者折叠为“打开/关闭会话”。

## 8. rollback 与工作区

thread rollback 只删除最近 N 个用户 turn 的上下文，不回滚磁盘。真正的文件撤销需要版本控制或显式 patch。若 UI 把 rollback 描述成“撤销操作”，会制造危险误解。

## 9. compaction 持久化

compaction 安装后写入 rollout，并发 `ContextCompacted`。恢复时应从 compaction 边界构造当前模型历史，同时保留审计所需旧记录。存储层可保留全量，prompt 投影只使用压缩后窗口。

## 10. 一致性风险

- rollout 已写但 SQLite 未更新：历史可恢复，列表暂时陈旧。
- SQLite 有 thread 但 rollout 缺失：可列出但无法完整 resume。
- 通知先发、持久化后失败：客户端看到不可恢复 item。
- 多连接同时 mutation：必须通过 Session submission 顺序化。

理想顺序是先提交语义记录，再发布可恢复通知；若为低延迟先流式展示，则 completed 必须在持久化后确认。

附件和本地图片不能只保存临时路径。持久化层需要记录可重新定位的 URI、复制策略或明确“恢复后不可用”；否则历史文本可恢复但多模态输入失效。

## 11. 测试与评价

测试覆盖 resume、fork identity、archive/unarchive、分页、rollout parsing、compaction 和 schema compatibility。JSONL 加 SQLite 的组合实用但有双写成本；InnoAgent 应定义单一 authoritative log，再把索引视为可重建投影，并提供一致性修复命令。
