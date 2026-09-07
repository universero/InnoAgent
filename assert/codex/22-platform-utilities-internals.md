# 22. 平台、工程与 Utilities 内部实现

## 1. Cloud 与环境

`cloud-tasks-client` 封装云任务 HTTP API，`cloud-tasks` 提供 CLI/UI、新任务和 diff 展示，`cloud-tasks-mock-client` 支撑确定性测试。`cloud-config` 加载服务端下发配置并进入普通 config layer stack，因此受 provenance 和 managed requirements 约束。

环境能力由 `exec-server` 的 environment registry/provider/config 表达。本地或远端环境都要给出 cwd、workspace roots、filesystem/process capability、网络策略和状态。App Server 的 environment API 只操作 descriptor；实际执行仍由 exec server client 选择 local/remote backend。

`external-agent-migration` 的 detect/import/session importer 将其他 Agent 配置映射到 Codex config，并记录导入历史。它必须过滤不可表达的字段、秘密和任意脚本，而不是原样复制。

## 2. Worktree 与 Git

`worktree` 创建、发现和清理任务工作树；`git-utils` 封装 repo root、branch、diff 等；`utils/git-discovery` 负责向上发现仓库。TUI 的 worktree browser/startup、exec 的 worktree adapter 是表面层。

worktree 的一致性对象是 Git branch/index/worktree，不负责进程权限。清理前需确认没有活动 thread/未保存修改；多个 Agent 操作同一 repo 时用独立 worktree 降低冲突，但最终 merge 仍需显式验证。

## 3. Realtime 与媒体

`realtime-webrtc` 管 session description、peer connection、data/audio channel、连接/关闭状态；`voice-host` 连接设备与 realtime API；`utils/audio` 做格式/设备辅助；`utils/image` 和 core image preparation 做尺寸/detail/编码处理。

实时 turn 需要把语音转写、assistant audio/text、sideband control 与普通 thread history 对齐。断线重连不能重复提交已经确认的音频段；设备权限失败必须与模型错误分开呈现。

## 4. 可观测性

| Crate | 数据 | 关键控制 |
|---|---|---|
| `otel` | spans、metrics、trace context | exporter 配置、采样、脱敏 |
| `otel-trace-websocket` | 实时 trace stream | 连接断开和背压 |
| `analytics` | 产品/行为事件 | opt-out、字段最小化 |
| `diagnostics` | gauge、运行健康 | 不包含秘密/prompt 原文 |
| `feedback` | logs/config/thread bundle | 用户确认和 redaction |
| `response-debug-context` | 模型请求调试信息 | 默认关闭、严格保留期 |

trace context 从 App Server request 传播到 turn、model request、tool call、exec server/HTTP。异步 worker 必须显式继承 context，否则同一 turn 会断链。

## 5. 构建与发布模块

`build-info` 编入版本、commit、target；`install-context` 标记 npm/brew/source 等安装来源；`codex-cli` npm 包选择平台 binary；Python runtime 把固定 binary 打包。Bazel 与 Cargo 双构建要求 workspace manifests、features 和 generated files保持一致。

仓库脚本检查 Cargo workspace manifests、TUI/core boundary、Bazel clippy、V8 canary、macOS signing/notarization 和 R2 release。发布产物应验证 checksum/signature、平台架构、`--version` 和最小 smoke turn。

## 6. Utilities 逐项实现边界

| Utility | 被上层使用的原语 | 不应承担的职责 |
|---|---|---|
| `absolute-path` | 已验证绝对路径 newtype | 文件授权 |
| `approval-presets` | 预定义 approval/permission 组合 | 最终策略裁决 |
| `audio` | 音频格式/设备辅助 | 会话状态 |
| `cache` | 文件/内存缓存原语 | 业务 source of truth |
| `cargo-bin` | 定位 workspace binary | 下载任意 executable |
| `cli` | 通用参数/输出辅助 | 产品命令分派 |
| `elapsed` | 时间区间与展示 | trace storage |
| `fuzzy-match` | 候选评分 | 权限过滤 |
| `git-discovery` | repo root/marker 发现 | 执行 Git 修改 |
| `home-dir` | 跨平台 home 解析 | Codex config merge |
| `image` | decode/resize/format | provider policy |
| `json-to-toml` | 数据格式转换 | schema 校验全部语义 |
| `oss` | 开源构建差异开关 | 产品授权 |
| `output-truncation` | 按字节/行限制输出 | 语义摘要 |
| `path-uri` | path 与 URI 双向转换 | sandbox allowlist |
| `path-utils` | canonicalize/路径辅助 | 独立安全边界 |
| `plugins` | plugin namespace 等共享类型 | 安装与信任决策 |
| `pty` | PTY 创建、resize、I/O | 命令审批 |
| `readiness` | 启动就绪协调 | 长期服务发现 |
| `redacted-string` | Debug/Display 脱敏包装 | secret 生命周期管理 |
| `rustls-provider` | TLS crypto provider 初始化 | endpoint policy |
| `sandbox-summary` | 人类可读权限摘要 | 实际 enforcement |
| `sleep-inhibitor` | 长任务期间抑制休眠 | task durability |
| `stream-parser` | 增量帧/文本解析 | protocol 业务状态 |
| `string` | 字符串通用函数 | prompt policy |
| `template` | 模板替换/渲染 | 不可信代码执行 |

## 7. 其他支撑 crate

`ansi-escape` 清理/解释终端序列；`terminal-detection` 判断 TTY 能力；`stdio-to-uds` 和 `uds` 处理本地进程连接；`async-utils` 提供通用并发原语；`test-binary-support` 定位和启动测试 binary；`thread-manager-sample` 给嵌入方展示最小调用方式；`v8-poc` 验证 code mode runtime，不应视为稳定产品 API。

## 8. 平台验证矩阵

| 维度 | 必须验证 |
|---|---|
| OS | macOS Seatbelt、Linux bwrap/Landlock、Windows restricted/elevated |
| Arch | npm/Python binary 选择、签名、动态库 |
| Transport | stdio、UDS、WebSocket、HTTP/SSE、代理/CA |
| Offline | model catalog cache、local provider、无网络启动 |
| Upgrade | config/schema/rollout/plugin cache migration |
| Shutdown | 子进程、PTY、MCP、WebRTC、writer、trace 全部释放 |
| Privacy | prompt/tool/path/credential 在 log/feedback/trace 中脱敏 |
