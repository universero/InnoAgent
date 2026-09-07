# 02. 入口与进程拓扑

## CLI 分派

Rust CLI 在 [`cli/src/main.rs`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/cli/src/main.rs#L1) 解析子命令和 arg0 多路复用。交互入口 `run_interactive_tui` 位于该文件约 2686 行；非交互执行进入 [`exec::run_main`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/exec/src/lib.rs#L258)。App Server、MCP server、sandbox helper、exec server、stdio/UDS 桥接也可作为独立进程表面。

npm 包 `@openai/codex` 主要负责平台二进制选择与启动，不重新实现 Agent。这个薄包装降低 Node 用户安装门槛，但真正兼容性由 Rust binary 决定。

## 三种主要拓扑

### 直接内嵌 core

`codex exec` 直接构建配置、认证和 thread runtime，消费事件并输出普通文本或 JSONL。适合 CI，进程结束即释放 runtime。

### TUI + 本地 App Server

当前 TUI 部分会启动/连接 App Server 来处理 session picker、恢复和归档等能力，[`start_app_server`](https://github.com/openai/codex/blob/694b6319d3ad2399f6e435760a22d9b9357f0697/codex-rs/tui/src/lib.rs#L496) 支持 local/remote target。TUI 仍保留大量本地显示状态，因此不是纯 App Server thin client。

### 外部客户端 + App Server

App Server 接受 stdio JSONL，实验性支持 WebSocket，也可通过 Unix socket/daemon 运行。连接必须先 `initialize`，服务端记录 client name/version/capabilities，后续请求按 connection session 分发。双向 JSON-RPC 的“服务端请求客户端”能力用于审批、MCP elicitation 等交互，不只是 REST 风格 request/response。

## 生命周期

进程初始化通常依次完成：读取 layered config -> 初始化 auth/provider -> 打开 state/thread store -> 构建 extension registry/MCP/plugin -> 创建 `ThreadManager` -> 接收 start/resume/fork -> 为 thread 创建 `Session` -> 消费 submission -> shutdown 时 flush store、停止 runtime 和发 thread lifecycle。异常关闭下持久化采用尽力 flush，审批 channel 消失则默认拒绝。

## 设计判断

多表面共用 core 是正确边界；但 TUI 同时直连 core 与依赖 App Server 能力，会形成迁移期双栈。InnoAgent 若采用服务化 UI，应尽早指定唯一“领域协议入口”，避免 CLI、Web 和 SDK 分别拼装 runtime。
