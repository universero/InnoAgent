"""Interactive CLI entry point."""

from __future__ import annotations

import sys
from typing import Any

from core.runtime.agent import InnoAgentRuntime
from view.commands import parse_command
from view.render import render_event, render_sessions, render_state, render_tools
from view.resume import pick_session, resume_summary


HELP_TEXT = """InnoAgent CLI

直接输入自然语言即可执行任务。可用命令：
  /help                 显示本帮助
  /goal <text>          设置当前目标
  /plan                 查看当前计划与任务
  /tools                查看工具及动态描述
  /status               查看当前状态摘要
  /mode auto|confirm|readonly  切换运行模式
  /model <model> [effort]  修改模型和思考强度(none/low/high/max)
  /rename <name>         重命名当前 session
  /resume [session_id]  恢复最近或指定 session
  /sessions             列出可恢复 session
  /clear                清空当前会话
  /quit                 退出
"""


class InnoAgentCLI:
    """Interactive REPL frontend for InnoAgent."""

    def __init__(self, runtime: InnoAgentRuntime, input_fn=None, output_fn=None) -> None:
        """Store the runtime and optional test-friendly IO callbacks."""
        self.runtime = runtime
        self.input_fn = input_fn or input
        self.output_fn = output_fn or print
        self.current_session_id: str | None = None
        self.current_state: dict[str, Any] | None = None
        self.current_goal: str | None = None
        self.running = True
        if self.runtime.stream_handler is None:
            self.runtime.stream_handler = self._stream_event

    def _stream_event(self, event: dict[str, Any]) -> None:
        """Render one runtime event through the configured output callback."""
        if event.get("type") in {"text.delta", "reasoning.delta"}:
            self.output_fn(str(event.get("content", "")))
            return
        rendered = render_event(event)
        if rendered:
            self.output_fn(rendered)

    def run(self) -> None:
        """Run the main REPL loop until EOF or /quit."""
        self.output_fn(HELP_TEXT)
        while self.running:
            try:
                raw = self.input_fn("InnoAgent> ").strip()
            except (EOFError, KeyboardInterrupt):
                self.output_fn("")
                return
            if not raw:
                continue
            command = parse_command(raw)
            if command:
                self._handle_command(command)
            else:
                self._handle_task(raw)

    def _handle_task(self, text: str) -> None:
        """Submit a natural-language task to the runtime."""
        if (
            self.current_state
            and self.current_session_id
            and self.current_state.get("pending_confirmation")
            and text.strip().lower() in {"确认", "是", "yes", "y"}
        ):
            result = self.runtime.approve_pending(self.current_session_id)
            self.current_state = result
            return

        result = self.runtime.invoke(
            text,
            session_id=self.current_session_id,
            goal=self.current_goal,
        )
        self.current_session_id = str(result.get("session_id", self.current_session_id))
        self.current_state = result
        if result.get("pending_confirmation"):
            self.output_fn("该操作需要权限确认。请回复“确认”批准，或使用 /quit 退出。")

    def _handle_command(self, command) -> None:
        """Dispatch a parsed slash command."""
        name = command.name
        args = command.args
        if name == "help":
            self.output_fn(HELP_TEXT)
        elif name == "goal":
            self.current_goal = " ".join(args).strip()
            self.output_fn(f"目标已设置为：{self.current_goal}")
        elif name == "plan":
            self.output_fn(self._plan_text())
        elif name == "tools":
            self.output_fn(render_tools(self.runtime, self.current_state or {}))
        elif name == "status":
            self.output_fn(self._status_text())
        elif name == "mode":
            if not args:
                self.output_fn(f"当前模式：{self.runtime.config.mode}")
                return
            mode = args[0]
            if mode not in {"auto", "confirm", "readonly"}:
                self.output_fn("无效模式，可选值：auto、confirm、readonly")
                return
            self.runtime.set_mode(mode)
            self.output_fn(f"模式已切换为：{mode}")
        elif name == "model":
            if not args:
                model = getattr(self.runtime.model, "model", "unknown")
                effort = getattr(self.runtime.model, "reasoning_effort", "none")
                self.output_fn(f"当前模型：{model}，思考强度：{effort}")
                return
            model_name = args[0]
            effort = args[1] if len(args) > 1 else None
            try:
                updated = self.runtime.update_model(model_name, effort)
                self.output_fn(
                    f"模型已切换为：{updated.model}，思考强度：{updated.reasoning_effort}"
                )
            except (TypeError, ValueError) as exc:
                self.output_fn(str(exc))
        elif name == "rename":
            if not self.current_session_id:
                self.output_fn("当前没有 session 可以重命名。")
                return
            new_name = " ".join(args).strip()
            if not new_name:
                self.output_fn("请提供新的 session 名称。")
                return
            record = self.runtime.rename_session(self.current_session_id, new_name)
            self.output_fn(f"session 已重命名为：{record.name}")
        elif name == "resume":
            record = pick_session(self.runtime, args[0] if args else None)
            if record is None:
                self.output_fn("未找到可恢复的 session。")
                return
            self.current_session_id = record.session_id
            self.current_state = record.state
            self.current_goal = record.goal
            self.output_fn(resume_summary(record))
            self.output_fn(render_state(record.state))
        elif name == "sessions":
            self.output_fn(render_sessions(self.runtime.list_sessions()))
        elif name == "clear":
            self.current_session_id = None
            self.current_state = None
            self.current_goal = None
            self.output_fn("当前会话已清空。")
        elif name == "stop":
            self.output_fn("当前任务已停止；如有需要可 /resume 恢复。")
        elif name == "quit":
            self.running = False
        else:
            self.output_fn("未知命令，输入 /help 查看帮助。")

    def _plan_text(self) -> str:
        """Return the current plan/task state for /plan."""
        state = self.current_state
        if not state:
            return "当前没有已执行的任务。"
        plan = state.get("plan")
        tasks = state.get("tasks") or []
        if not plan and not tasks:
            return "当前没有计划或任务。"
        return render_state(state)

    def _status_text(self) -> str:
        """Return the current session status for /status."""
        if not self.current_state:
            return "当前没有已执行的任务。"
        state = self.current_state
        session_label = self.current_session_id or "无"
        if self.current_session_id:
            try:
                record = self.runtime.session_store.load(self.current_session_id)
                session_label = record.name or record.session_id
            except KeyError:
                pass
        return (
            f"session={session_label}\n"
            f"mode={state.get('mode', self.runtime.config.mode)}\n"
            f"goal={state.get('goal') or '无'}\n"
            f"goal_complete={state.get('goal_complete', False)}\n"
            f"iteration={state.get('iteration', 0)}"
        )
