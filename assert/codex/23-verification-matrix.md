# 23. 全模块验证矩阵

## 1. 完成标准

“覆盖模块”不以提到目录名为准。每个模块至少要回答：它负责什么；核心对象/入口是什么；输入输出如何流动；依赖与被依赖边界是什么；失败/安全语义是什么；哪里有测试或仍需动态验证。大型模块还要展开内部子模块。

## 2. 运行时与协议

| 模块 | 实现证据 | 已核验行为 | 剩余动态验证 |
|---|---|---|---|
| `core` | ThreadManager、CodexThread、Session、run_turn | 单 active task、串行 submission、turn/tool/model 生命周期 | 编译与全量 core tests |
| `core-api` | 公共 API 类型与稳定边界 | 从具体 Session 实现中隔离调用方 | semver/API compatibility |
| `protocol` | `Op`、`EventMsg`、items、session metadata | 内部命令/事件穷尽枚举 | serialization property tests |
| `app-server` | MessageProcessor 与 domain processors | initialize gate、双向 RPC、thread/turn 映射 | stdio/WebSocket E2E |
| `app-server-protocol*` | request/notification macros、v1/v2、export | schema/SDK 生成链 | generated artifact clean build |
| `cli`/`tui`/`exec` | 各入口与 event processors | 多表面复用 core、JSONL 与 human output 分离 | terminal/platform snapshots |

## 3. 模型与上下文

| 模块 | 实现证据 | 已核验行为 | 剩余动态验证 |
|---|---|---|---|
| `core/client` | `ModelClientSession::stream` | Responses、WS 优先/HTTP fallback | 真实 provider failure injection |
| `codex-api` | endpoint/request/SSE modules | typed wire events/error mapping | schema conformance against service |
| `codex-client` | retry/SSE/telemetry | bounded retry、idle timeout | network chaos |
| `http-client`/`websocket-client` | TLS/proxy/redirect/pool | transport 与领域分离 | proxy/custom CA matrix |
| `model-provider*` | provider/auth/shared state | endpoint/auth/catalog 组合 | token refresh concurrency |
| `models-manager` | cache/presets/overrides | 动态目录与 fallback | stale cache/offline matrix |
| context/prompts | context fragments/manager/templates | 来源化拼装、规范化、预算 | token estimator/model parity |
| compact modules | local/remote/v2/token/image | compact 与普通 retry 分离 | 长会话恢复测试 |

## 4. 工具与安全

| 模块 | 实现证据 | 已核验行为 | 剩余动态验证 |
|---|---|---|---|
| ToolRouter/Registry | dispatch terminal outcome | typed payload、hook、telemetry、terminal event | cancellation races |
| unified exec | command/session handlers | exec/write_stdin、TTY、timeout | process-tree kill |
| apply-patch | parser + core handler | 结构化 patch、路径/审批 | filesystem race |
| execpolicy | Starlark + parsed segments | 全 segment allow 才 bypass | shell corpus fuzzing |
| approvals | pending oneshot map | 先注册后通知、断连 Abort | UI disconnect race |
| sandboxing | transform/platform backends | 无法映射时 fail closed | 三平台逃逸测试 |
| exec-server | local/remote process+FS+Noise | accepted/completed 分离、恢复 | remote fault injection |
| network/secrets/hardening | proxy/broker/redaction | defense in depth | host penetration tests |

## 5. 状态与恢复

| 模块 | 实现证据 | 已核验行为 | 剩余动态验证 |
|---|---|---|---|
| `thread-store` | trait/local/in-memory/live thread | writer 唯一、能力探测、分页 | crash consistency |
| `rollout` | recorder/scanner/index/migration | append JSONL、尾部扫描 | fsync/power-loss tests |
| `state` | SQLite models/runtime/migrations | 查询投影、recovery/backfill | 大版本迁移 |
| `rollout-trace` | trace writer/reducer | 与用户 rollout 分离 | 大 trace 压力 |
| `message-history` | append history file | 输入历史独立 | 多进程压力 |
| attachment/agent graph | stores | 大对象与关系独立持久化 | GC/引用完整性 |

## 6. 扩展生态

| 模块 | 实现证据 | 已核验行为 | 剩余动态验证 |
|---|---|---|---|
| extension-api | builder/immutable registry | typed contribution、turn snapshot | third-party compatibility |
| `ext/*` | 13 个领域扩展 | 经统一 lifecycle/core 服务接入 | feature combination matrix |
| `codex-mcp` | runtime/connection/catalog/resources | 配置快照、tool filtering、status | OAuth/HTTP interoperability |
| `rmcp-client` | transports/OAuth/EMA | bounded stdio、redirect/refresh locks | external server matrix |
| skills | parser/loading/selection | mention、动态选择、预算渲染 | 大 catalog quality |
| plugin/core-plugins | manifest/marketplace/store/loader | install/load/policy/cache/attribution | supply-chain adversarial tests |
| hooks | event schemas/dispatcher/runners | pre/post 时序、command/MCP handlers | timeout/fail-policy matrix |
| memories | extract/lease/consolidate/read | 两阶段、全局锁、隔离 Agent | poisoning/deletion tests |
| goal/queue/connectors | state/tools/lifecycle | 预算、状态迁移、后续任务 | restart/concurrency tests |

## 7. 远端与产品能力

| 模块 | 实现证据 | 已核验行为 | 剩余动态验证 |
|---|---|---|---|
| agent-* | control/roles/identity/graph | 子 thread、角色收敛、父子关系 | deadlock/cost stress |
| cloud-* | client/domain/mock/config | 开源客户端，不含云服务端 | live service contract |
| worktree/git | creation/discovery/diff | 工作区隔离不等于 sandbox | concurrent Git operations |
| realtime/voice/audio | WebRTC/host/media utils | 独立实时生命周期 | device/network matrix |
| code-mode-* | protocol/runtime/host/broker | 按需 worker、代理工具调用 | V8 resource limits |
| migration | detect/import/history | 显式转换外部配置 | malicious config fixtures |

## 8. 工程基础设施

| 模块 | 实现证据 | 已核验行为 | 剩余动态验证 |
|---|---|---|---|
| otel/analytics/diagnostics | spans/metrics/events | request-turn-tool 关联 | exporter/privacy audit |
| feedback/debug context | bundle/context | 独立收集路径 | redaction audit |
| build/install context | embedded metadata | 版本与安装来源 | release artifact smoke |
| utilities | 26 个单责 crate | 路径、PTY、cache、format 等边界 | 平台级 unit tests |
| npm/Python runtime | binary packaging | 平台选择、pinned runtime | clean-machine install |

## 9. 源码覆盖审计

基线包含约 126 个 Rust Cargo manifest，以及根 npm workspace、CLI wrapper、TypeScript SDK、Python SDK/runtime 和脚本 package。所有这些条目均列入 [14-crate-catalog.md](14-crate-catalog.md)。大型目录的内部模块分别在 15 至 22 章展开；README 提供主题交叉导航。

静态证据能够证明结构、类型、调用和测试意图，但不能证明平台运行结果。本机缺少 `cargo`，因此三平台 sandbox、真实 provider、OAuth、WebRTC、远端 exec 和 release artifact 仍明确标记为动态验证缺口，而不是被错误宣称为已通过。

## 10. 总体判定

实现覆盖已经从“功能概览”提升到“crate 全覆盖 + 大型 crate 子模块展开 + 关键调用链 + 失败不变量 + 测试/动态缺口”。对任何新增上游版本，复审顺序应为：workspace diff -> protocol/schema diff -> core state machine -> tool/security -> persistence migrations -> SDK generated artifacts -> platform CI。
