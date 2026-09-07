# 09. CLI、TUI、Exec 与 SDK

## CLI/TUI

CLI 是命令分派与安装表面；TUI 承担输入编辑、stream render、diff、approval、session picker、状态栏、通知等复杂状态。TUI 代码量与 core 接近，说明终端 UI 已是独立应用而非 print loop。它消费事件而不应决定工具安全语义；审批 UI 只提交决策，policy/sandbox 仍在 core。

## 非交互 Exec

`codex exec` 面向脚本和 CI，支持 JSONL 输出、resume、schema output、model/sandbox/approval 参数。JSONL 事件使调用方能保留 thread id、items、usage 和错误，而不是解析终端文本。非交互模式没有人可回答审批时，必须使用预先确定的 approval policy；不能无限等待。

## TypeScript SDK

TypeScript `@openai/codex-sdk` 是 CLI process adapter。它通过内部 exec runner spawn `codex exec --json`，把输入、图片、model、cwd、sandbox、approval、schema 文件等映射为 CLI 参数。[`runStreamedInternal`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/sdk/typescript/src/thread.ts#L70) 逐行解析 JSON，遇到 `thread.started` 保存 id，最终清理临时 output schema 文件。

优点是实现薄、与 CLI 行为一致；限制是每次运行存在子进程边界，双向审批、长驻订阅和丰富 App Server API 不如原生 RPC。

## Python SDK

Python `openai-codex` 控制本地 App Server JSON-RPC，published build 由 `python-runtime` 带固定 CLI runtime。它维护 request waiter 和 notification router，按 login/turn/thread goal 分流。[`route_notification`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/sdk/python/src/openai_codex/_message_router.py#L176) 会缓存“注册前到达”的通知，`turn/completed` 对未知 turn 做终止清理，避免竞态和无界残留。

因此两套 SDK 不应描述为同一种实现：TypeScript 是 exec JSONL adapter，Python 是长驻 App Server client。它们共享产品语义，但生命周期、错误、并发和可用 API 不同。

## 兼容风险

- CLI flags、JSONL events、App Server schema 和两套 SDK generated types 需要同步发布。
- 子进程 stderr/exit code 与 JSON-RPC error object 的错误模型不同。
- SDK 应暴露版本/能力探测，不能假设 bundled binary 永远匹配全局 binary。
- output schema 临时文件、取消 signal、进程退出和通知缓存都需清理测试。
