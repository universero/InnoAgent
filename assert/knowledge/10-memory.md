# Memory

## 模块定位

Memory 保存跨 Session、低频、可复用的用户信息。它不保存当前任务执行状态，也不承担 Session 恢复。

主要模块：

| 文件 | 职责 |
|---|---|
| `core/memory/profile.py` | UserFact、UserProfile 和 JSON ProfileStore |
| `core/memory/recall.py` | 从 ProfileStore 读取并提供 Context 文本 |
| `core/memory/thresholds.py` | 更新触发阈值 |
| `core/memory/update.py` | 启发式更新和后台持久化 |

## Memory 与其他状态的区别

| 数据 | 生命周期 | 示例 |
|---|---|---|
| AgentState | 当前 Session/Turn | pending tools、Plan、usage |
| Context Summary | 当前 Session 压缩历史 | 已修改文件、未完成步骤 |
| Memory | 跨 Session | 用户偏好、工作风格、近期主题 |
| Repository | 长期项目真值 | 代码、配置、测试 |

“本轮测试失败”属于 State，不应写成长期用户事实；“用户偏好中文注释”可以进入 Memory，但仍不能覆盖仓库规范。

## Profile 数据模型

`UserProfile` 包含：

- `user_id`
- `preferences: dict[str, str]`
- `facts: list[UserFact]`
- `recent_topics`
- `working_style`
- `version`
- `last_updated_at`

`UserFact` 带 0 到 1 的 confidence，但当前 recall 不按 confidence 过滤。该字段为后续冲突和衰减策略预留。

## Recall

`MemoryRecall` 只是 ProfileStore 的薄读取层。`UserProfile.recall_text()` 将信息渲染为最多 600 字符的短文本：

- 用户偏好。
- 最多 8 条长期事实。
- 最多 6 个近期话题。
- 工作风格。

使用短文本而不是完整 Profile JSON，可以控制 token，并避免模型过度关注历史偏好。Memory 在 Context 中位于当前用户输入之后，但其信任等级低于当前请求和系统策略。

## 更新触发

`MemoryThresholds` 使用两个条件的 OR：

- 累计 turn 达到 `turn_threshold`，默认 4。
- 累计输入 token 估算达到 `input_token_threshold`，默认 1200。

Runtime 每个完成 turn 调用 `register_turn()`。达到阈值后启动 daemon thread，不阻塞主图和用户最终输出。

阈值更新而不是每轮写入的原因：

- 降低磁盘和模型成本。
- 避免把瞬时请求都固化为长期偏好。
- Memory 不应位于任务关键路径。

## 当前更新策略

`heuristic_update()` 是确定性 fallback：

- 保存截断后的 `last_request`。
- 将请求前 40 字作为近期主题。
- 当回答较短时保存 `last_summary`。

`UserProfile.apply_update()`：

- preferences 直接合并。
- facts 按 key 更新，confidence 取较高值。
- recent topics 去重并保留 10 条。
- working style 有值时覆盖。
- version 增加并更新 UTC 时间。

该策略主要证明 Memory 生命周期和异步边界，不代表成熟的长期记忆质量。

## ProfileStore

每个用户保存为 `<profile_root>/<user_id>.json`。不存在时返回空 Profile。

选择 JSON 文件是为了保持本地项目简单、可检查和无数据库依赖。当前保存使用直接 `write_text()`，没有原子替换、锁和损坏恢复，因此只适合单进程轻量使用。

## 异步边界

`MemoryUpdater` 创建 daemon thread 执行 load -> update -> save。主 Agent 不等待它完成，进程退出时 daemon thread 也可能未落盘。

当前 counters 和 ProfileStore 没有线程锁。如果多个 turn 并发注册或多个进程写同一 profile，可能出现计数竞争或覆盖。当前 CLI 假设单个活跃 Runtime，这一限制必须写入设计边界。

## 为什么 Memory 默认低信任

Memory 可能来源于模型归纳、启发式摘要或旧对话，存在过期、误解和冲突。因此它只能用于：

- 调整表达和工作偏好。
- 提醒可能相关的长期背景。
- 减少重复询问低风险信息。

它不能用于：

- 自动授权工具。
- 判断文件或业务状态。
- 覆盖当前用户指令。
- 保存 Secret。
- 单独证明 Goal 完成。

## 隐私与生命周期

成熟 Memory 需要：

- 用户可查看、编辑和删除。
- 来源和证据引用。
- TTL 与衰减。
- 冲突检测和更正。
- 敏感字段分类。
- 项目级与用户级隔离。
- 不写入凭据和原始大段对话。

当前实现只有版本号和更新时间，尚未提供上述治理能力，因此应保持 `memory_enabled` 可关闭。

## 演进方案

推荐顺序：

1. 原子保存和文件锁。
2. 为 Fact 增加 source、created_at、expires_at 和 sensitivity。
3. 增加显式删除与更正命令。
4. 用证据驱动 extractor 替换最近请求启发式。
5. 增加冲突合并与置信度衰减。
6. 数据量真正增长后再考虑索引或向量检索。

不建议一开始引入向量数据库，因为当前主要问题是事实质量、权限和生命周期，而不是召回性能。

## 扩展约束

- Memory 更新失败不能影响当前 turn 完成。
- Memory 内容必须有严格长度上限。
- 当前请求与仓库规则始终高于 Memory。
- 新字段必须考虑旧 JSON 兼容。
- 异步更新需要显式并发测试。
- 删除用户数据时必须删除对应 Profile 文件和派生索引。

## 关键测试

- `test/test_memory.py`：Profile round-trip、阈值触发和同步更新。
- `test/test_compression.py`：Memory 与 Session summary 不重复承担同一职责。
- 后续应增加并发写、损坏文件、删除和 TTL 测试。
