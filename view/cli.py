"""Interactive CLI for the unified runtime."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from core.runtime.agent import InnoAgentRuntime
from core.tool.approval import ApprovalRequest, parse_approval_decision
from view.commands import Command, CommandChoice, command_help_text, parse_command
from view.render import render_event, render_sessions, render_state, render_tools
from view.resume import (
    pick_session,
    replayable_session_events,
    resume_summary,
    session_choice_description,
)
from view.terminal import TerminalIO


HELP_TEXT = command_help_text()
_CLEAR_GOAL_VALUES = {"", "off", "none", "clear"}
_REASONING_EFFORTS = ["none", "low", "medium", "high"]
_REASONING_EFFORT_LABELS = {
    "none": "No reasoning",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
}
_REASONING_EFFORT_DESCRIPTIONS = {
    "none": "fastest, without deliberate reasoning",
    "low": "light reasoning for straightforward tasks",
    "medium": "balanced reasoning for normal coding work",
    "high": "deeper reasoning for complex tasks",
}


class InnoAgentCLI:
    """Stateful slash-command frontend with test-friendly adapters."""

    def __init__(self, runtime: InnoAgentRuntime, input_fn=None, output_fn=None) -> None:
        self.runtime = runtime
        self.current_session_id: str | None = None
        self.current_state: dict[str, Any] | None = None
        self.current_goal: str | None = None
        self.active_skills: list[dict[str, Any]] = []
        self.running = True
        self._tui_busy = False
        self._tui_accepts_steering = False
        self._quit_when_idle = False
        # Codex 的 submission loop 串行处理控制操作；TUI 至少要保证模态选择器
        # 只有一个所有者，避免设置选择与 ask_user/approval 相互抢占。
        self._selection_lock = asyncio.Lock()
        self.terminal: TerminalIO | None = None
        # prompt_toolkit 只用于真实交互终端；管道和 CI 使用普通 stdin/stdout。
        if (
            input_fn is None
            and output_fn is None
            and sys.stdin.isatty()
            and sys.stdout.isatty()
        ):
            self.terminal = TerminalIO(
                state_provider=self._tui_snapshot,
                command_option_provider=self._command_options,
            )
            self.input_fn = input
            self.output_fn = self.terminal.output
        else:
            self.input_fn = input_fn or input
            self.output_fn = output_fn or print
        if self.runtime.stream_handler is None:
            self.runtime.stream_handler = self._stream_event

    def run(self) -> None:
        """Run the REPL until EOF or `/quit`."""
        if self.terminal:
            self.terminal.banner(
                getattr(self.runtime.model, "model", "custom"),
                getattr(self.runtime.model, "reasoning_effort", ""),
                str(Path(self.runtime.config.workspace_root).resolve()),
                self.runtime.config.mode,
            )
            self.terminal.run(self._dispatch_tui_input)
            return

        self.output_fn(HELP_TEXT)
        while self.running:
            try:
                raw = self.input_fn("› ").strip()
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

    async def _dispatch_tui_input(self, text: str) -> None:
        """分发 TUI 输入；执行任务时后续输入自动成为 steering。"""
        assert self.terminal is not None
        command = parse_command(text)
        if self._tui_busy:
            if command and command.name == "quit":
                self._quit_when_idle = True
                self.terminal.add_user_message("/stop", steering=True)
                self._handle_live_input("/stop")
                return
            if not self._tui_accepts_steering:
                self.output_fn("当前命令仍在执行，请稍候。")
                return
            self.terminal.add_user_message(text, steering=True)
            self._handle_live_input(text)
            return

        if command and command.name == "quit":
            self._handle_command(command)
            self.terminal.stop()
            return

        pending = (self.current_state or {}).get("pending_confirmation")
        if pending:
            decision = self._approval_decision(text)
            if decision is None:
                await self._resolve_pending_interactions()
                return
            self.terminal.set_approval()
            await self._run_tui_work(
                lambda: self._resolve_approval(decision),
                "Running approved tool",
            )
            await self._resolve_pending_interactions()
            return

        pending_question = (self.current_state or {}).get("pending_user_question")
        if pending_question:
            handled = False
            async with self._selection_lock:
                # 获取选择器所有权后重新检查，避免另一个 pending handler 已经消费问题。
                if (self.current_state or {}).get("pending_user_question"):
                    handled = True
                    if command:
                        self.output_fn(
                            "A user question is pending; answer it before running commands."
                        )
                    else:
                        # 只会在模型结束与选择器接管输入之间的极短窗口进入这里。
                        self.terminal.add_user_message(text)
                        await self._run_tui_work(
                            lambda: self._handle_task(text, request_approval=False),
                            "Thinking",
                            accepts_steering=True,
                        )
            if handled:
                await self._resolve_pending_interactions()
                return

        if command:
            if command.name == "model":
                await self._select_model()
                return
            if command.name == "resume" and not command.args:
                await self._select_session()
                await self._resolve_pending_interactions()
                return
            if command.name == "skill" and not command.args:
                await self._select_skill()
                return
            if command.name == "mode" and not command.args:
                await self._select_mode()
                return
            if command.name in {"clear", "new"}:
                self.terminal.clear()
            runs_goal = (
                command.name == "goal"
                and bool(command.args)
                and not self._clears_goal(command.args)
            )
            await self._run_tui_work(
                lambda: self._handle_command(command, request_approval=False),
                "Thinking" if runs_goal else "Running command",
                accepts_steering=runs_goal,
            )
            await self._resolve_pending_interactions()
            return

        self.terminal.add_user_message(text)
        await self._run_tui_work(
            lambda: self._handle_task(text, request_approval=False),
            "Thinking",
            accepts_steering=True,
        )
        await self._resolve_pending_interactions()

    async def _select_model(self) -> None:
        """Select a provider model and reasoning effort as one configuration change."""
        assert self.terminal is not None
        async with self._selection_lock:
            self._tui_busy = True
            self._tui_accepts_steering = False
            self.terminal.set_busy(True, "Loading models")
            try:
                models = await asyncio.to_thread(self.runtime.list_models)
            except Exception as exc:  # noqa: BLE001
                self.output_fn(f"[error] 无法获取模型列表：{exc}")
                return
            finally:
                self._tui_busy = False
                self.terminal.set_busy(False, "Ready")

            current_model = str(getattr(self.runtime.model, "model", ""))
            selected_model = await self.terminal.select(
                "Select model",
                models,
                current=current_model,
            )
            if selected_model is None:
                self.terminal.refresh()
                return

            current_effort = str(
                getattr(self.runtime.model, "reasoning_effort", "none") or "none"
            )
            selected_effort = await self.terminal.select(
                "Select reasoning effort",
                _REASONING_EFFORTS,
                current=current_effort,
                labels=_REASONING_EFFORT_LABELS,
                descriptions=_REASONING_EFFORT_DESCRIPTIONS,
            )
            if selected_effort is None:
                self.terminal.refresh()
                return
            if selected_model == current_model and selected_effort == current_effort:
                self.terminal.refresh()
                return
            try:
                updated = await asyncio.to_thread(
                    self.runtime.update_model,
                    selected_model,
                    selected_effort,
                )
            except (TypeError, ValueError) as exc:
                self.output_fn(f"[error] {exc}")
                return
            self.terminal.set_model(updated.model, updated.reasoning_effort)
            self.output_fn(f"model: {updated.model} {updated.reasoning_effort}")

    async def _select_session(self) -> None:
        """Select and restore a persisted session without requiring its id."""
        assert self.terminal is not None
        records = await asyncio.to_thread(self.runtime.list_sessions, 100)
        if not records:
            self.output_fn("No resumable session found.")
            return
        labels = {record.session_id: record.name or record.session_id for record in records}
        descriptions = {
            record.session_id: self._session_choice_description(record)
            for record in records
        }
        async with self._selection_lock:
            selected = await self.terminal.select(
                "Resume session",
                [record.session_id for record in records],
                current=self.current_session_id,
                labels=labels,
                descriptions=descriptions,
            )
        if selected is None:
            return
        await self._run_tui_work(
            lambda: self._resume_session(selected),
            "Resuming session",
            accepts_steering=False,
        )

    async def _select_skill(self) -> None:
        assert self.terminal is not None
        skills = await asyncio.to_thread(self.runtime.skill_loader.discover)
        if not skills:
            self.output_fn("No Skills found.")
            return
        async with self._selection_lock:
            selected = await self.terminal.select(
                "Activate Skill",
                [skill.name for skill in skills],
                descriptions={skill.name: skill.description for skill in skills},
            )
        if selected is not None:
            await self._run_tui_work(
                lambda: self._handle_command(Command("skill", [selected], f"/skill {selected}")),
                "Activating Skill",
                accepts_steering=False,
            )

    async def _select_mode(self) -> None:
        assert self.terminal is not None
        async with self._selection_lock:
            selected = await self.terminal.select(
                "Permission mode",
                ["ask", "auto", "readonly"],
                current=self.runtime.config.mode,
                descriptions={
                    "ask": "confirm write-capable operations",
                    "auto": "run allowed operations without asking",
                    "readonly": "block write-capable operations",
                },
            )
        if selected is not None:
            self._handle_command(Command("mode", [selected], f"/mode {selected}"))

    async def _resolve_pending_interactions(self) -> None:
        """Resolve pending approvals/questions and continue the same session."""
        assert self.terminal is not None
        async with self._selection_lock:
            await self._resolve_pending_interactions_locked()

    async def _resolve_pending_interactions_locked(self) -> None:
        """Own the terminal selector while resolving server-initiated requests."""
        assert self.terminal is not None
        while True:
            approval = (self.current_state or {}).get("pending_confirmation") or {}
            if approval:
                request = ApprovalRequest.model_validate(approval)
                values = [option.value for option in request.options]
                decision = await self.terminal.select(
                    "Approve",
                    values,
                    labels={option.value: option.label for option in request.options},
                    descriptions={
                        option.value: option.description for option in request.options
                    },
                )
                if decision is None:
                    return
                self.terminal.set_approval()
                await self._run_tui_work(
                    lambda: self._resolve_approval(decision),
                    "Running approved tool",
                )
                continue

            pending = (self.current_state or {}).get("pending_user_question") or {}
            if not pending:
                return
            answer = await self.terminal.select(
                "Answer",
                [str(item) for item in pending.get("options", [])],
                allow_custom=bool(pending.get("allow_custom", True)),
            )
            if answer is None:
                return
            self.terminal.add_user_message(answer)
            await self._run_tui_work(
                lambda: self._handle_task(answer, request_approval=False),
                "Thinking",
                accepts_steering=True,
            )

    async def _run_tui_work(
        self,
        function,
        activity: str,
        *,
        accepts_steering: bool = True,
    ) -> None:
        """在后台线程执行同步 runtime，同时保持 TUI 可响应。"""
        assert self.terminal is not None
        self._tui_busy = True
        self._tui_accepts_steering = accepts_steering
        self.terminal.set_busy(True, activity)
        try:
            await asyncio.to_thread(function)
        except Exception as exc:
            self.output_fn(f"[error] {exc}")
        finally:
            self._tui_busy = False
            self._tui_accepts_steering = False
            self.terminal.set_busy(False, "Ready")
            self._sync_tui_approval()
            self.terminal.refresh()
            if self._quit_when_idle:
                self.running = False
                self.terminal.stop()

    def _sync_tui_approval(self) -> None:
        if self.terminal is None:
            return
        pending = (self.current_state or {}).get("pending_confirmation") or {}
        if not pending:
            self.terminal.set_approval()
            return
        request = ApprovalRequest.model_validate(pending)
        detail = ", ".join(
            f"{key}={self._compact_argument(value)}"
            for key, value in request.arguments.items()
        )
        if request.reason:
            detail = f"{detail}\n{request.reason}" if detail else request.reason
        self.terminal.set_approval(
            request.tool_name,
            detail,
        )

    @staticmethod
    def _approval_decision(text: str) -> str | None:
        return parse_approval_decision(text)

    def _handle_live_input(self, text: str) -> None:
        if not text:
            return
        if text == "/stop":
            if not self.runtime.request_stop(session_id=self.current_session_id):
                self.output_fn("No running turn to stop.")
            return
        delivery = "after_tool"
        content = text
        if text.startswith("/steer"):
            content = text[len("/steer") :].strip()
            if content.startswith("now "):
                delivery = "immediate"
                content = content[len("now ") :].strip()
        elif text.startswith("/"):
            self.output_fn("A turn is running; use /steer, /steer now, or /stop.")
            return
        if not content:
            self.output_fn("usage: /steer [now] <instruction>")
            return
        if not self.runtime.submit_steering(
            content,
            session_id=self.current_session_id,
            delivery=delivery,  # type: ignore[arg-type]
        ):
            self.output_fn("No running turn to steer.")

    def _stream_event(self, event: dict[str, Any]) -> None:
        if self.terminal:
            self.terminal.handle_event(event, render_event(event))
            return
        if event.get("type") == "item.delta" and event.get("item_type") in {
            "message",
            "reasoning",
        }:
            content = str(event.get("delta") or event.get("content") or "")
            self.output_fn(content)
            return
        rendered = render_event(event)
        if rendered:
            self.output_fn(rendered)

    def _handle_task(
        self,
        text: str,
        *,
        request_approval: bool = True,
        restart_goal: bool = False,
    ) -> None:
        """Submit natural language or resolve a pending approval."""
        if self.current_state and self.current_session_id and self.current_state.get(
            "pending_confirmation"
        ):
            mapped = parse_approval_decision(text)
            if mapped:
                try:
                    self._resolve_approval(mapped)
                except ValueError as exc:
                    self.output_fn(str(exc))
                except Exception as exc:  # noqa: BLE001
                    self.output_fn(f"[error] {exc}")
                return

        result = self.runtime.invoke(
            text,
            session_id=self.current_session_id,
            goal=self.current_goal,
            active_skills=self.active_skills,
            restart_goal=restart_goal,
        )
        self.current_session_id = str(result.get("session_id"))
        self.current_state = result
        self.active_skills = list(result.get("active_skills", []))
        if request_approval and result.get("pending_confirmation"):
            self._request_approval()

    def _resolve_approval(self, decision: str) -> None:
        if not self.current_session_id:
            return
        try:
            self.current_state = self.runtime.resolve_approval(
                self.current_session_id,
                decision,  # type: ignore[arg-type]
            )
        except Exception:
            self._refresh_current_state()
            raise

    def _refresh_current_state(self) -> None:
        if not self.current_session_id:
            self.current_state = None
            return
        try:
            self.current_state = self.runtime.session_store.load_state(
                self.current_session_id
            )
        except Exception:
            self.current_state = None

    def _request_approval(self) -> None:
        pending = (self.current_state or {}).get("pending_confirmation") or {}
        if not self.current_session_id:
            return
        if not self.terminal:
            self.output_fn(
                "需要权限确认：/approve once、/approve always 或 /approve deny"
            )

    def _handle_command(
        self,
        command: Command | None,
        *,
        request_approval: bool = True,
    ) -> None:
        if command is None:
            return
        name, args = command.name, command.args
        if name == "help":
            self.output_fn(HELP_TEXT)
        elif name == "goal":
            if not args:
                self.output_fn(f"goal: {self.current_goal or 'none'}")
                return
            value = " ".join(args).strip()
            if self._clears_goal(args):
                self.current_goal = None
                self.output_fn("goal: none")
                return
            self.current_goal = value
            self.output_fn(f"goal: {self.current_goal or 'none'}")
            self._handle_task(
                value,
                request_approval=request_approval,
                restart_goal=True,
            )
        elif name in {"plan", "tasks"}:
            self.output_fn(self._plan_text())
        elif name == "tools":
            self.output_fn(render_tools(self.runtime, self.current_state or {}))
        elif name == "skills":
            skills = self.runtime.skill_loader.discover()
            self.output_fn(
                "Available Skills:\n"
                + ("\n".join(f"  {skill.name}: {skill.description}" for skill in skills) or "  none")
            )
        elif name == "skill":
            if not args:
                self.output_fn("usage: /skill <name>")
                return
            try:
                skill = self.runtime.activate_skill(args[0])
            except KeyError as exc:
                self.output_fn(str(exc))
            else:
                self.active_skills = [
                    item
                    for item in self.active_skills
                    if item.get("name") != skill.get("name")
                ] + [skill]
                self.output_fn(f"Skill activated: {skill['name']}")
        elif name == "status":
            self.output_fn(self._status_text())
        elif name == "context":
            self._handle_context(args)
        elif name == "permissions":
            rules = self.runtime.permission_store.describe()
            self.output_fn("Permission rules:\n" + ("\n".join(f"  {rule}" for rule in rules) or "  none"))
        elif name == "compact":
            if not self.current_session_id:
                self.output_fn("No active session to compact.")
                return
            self.current_state = self.runtime.compact(
                self.current_session_id,
                " ".join(args).strip() or None,
            )
        elif name == "approve":
            if not self.current_session_id:
                self.output_fn("No active session.")
                return
            decision = parse_approval_decision(" ".join(args))
            if decision is None:
                self.output_fn("usage: /approve once|always|deny")
                return
            try:
                self._resolve_approval(decision)
            except Exception as exc:  # noqa: BLE001
                self.output_fn(str(exc))
        elif name == "steer":
            self.output_fn("No running turn. During execution use /steer [now] <instruction>.")
        elif name == "mode":
            if not args:
                self.output_fn(f"mode: {self.runtime.config.mode}")
                return
            mode = args[0]
            if mode not in {"ask", "auto", "readonly"}:
                self.output_fn("valid modes: ask, auto, readonly")
                return
            self.runtime.set_mode(mode)
            self.output_fn(f"mode: {mode}")
        elif name == "model":
            self._handle_model(args)
        elif name == "rename":
            value = " ".join(args).strip()
            if not value:
                self.output_fn("usage: /rename <name>")
                return
            if not self.current_session_id:
                self._create_current_session()
            record = self.runtime.rename_session(self.current_session_id, value)
            self.output_fn(f"session 已重命名为：{record.name}")
        elif name == "resume":
            self._resume_session(" ".join(args).strip() or None)
        elif name == "sessions":
            self.output_fn(render_sessions(self.runtime.list_sessions()))
        elif name in {"clear", "new"}:
            self.current_session_id = None
            self.current_state = None
            self.current_goal = None
            self.active_skills = []
            self.output_fn("Started a new session.")
        elif name == "stop":
            self.output_fn("The current turn is no longer active.")
        elif name == "quit":
            self.running = False
        else:
            self.output_fn("Unknown command. Use /help.")

    @staticmethod
    def _clears_goal(args: list[str]) -> bool:
        return " ".join(args).strip().lower() in _CLEAR_GOAL_VALUES

    def _handle_model(self, args: list[str]) -> None:
        if not args:
            self.output_fn(
                f"model: {getattr(self.runtime.model, 'model', 'custom')} "
                f"{getattr(self.runtime.model, 'reasoning_effort', '')}"
            )
            return
        try:
            updated = self.runtime.update_model(args[0], args[1] if len(args) > 1 else None)
        except (TypeError, ValueError) as exc:
            self.output_fn(str(exc))
        else:
            self.output_fn(f"model: {updated.model} {updated.reasoning_effort}")

    def _create_current_session(self) -> None:
        record = self.runtime.create_session(goal=self.current_goal)
        self.current_session_id = record.session_id
        self.current_state = record.state

    def _resume_session(self, query: str | None) -> None:
        record = pick_session(self.runtime, query)
        if record is None:
            self.output_fn("No matching resumable session found.")
            return
        self.current_session_id = record.session_id
        self.current_state = record.state
        self.current_goal = record.goal
        self.active_skills = list(record.state.get("active_skills", []))
        if self.terminal is not None:
            events = self.runtime.session_store.load_events(record.session_id)
            for event in replayable_session_events(events):
                if event.get("type") == "turn.started":
                    user_input = str((event.get("payload") or {}).get("user_input") or "")
                    if user_input:
                        self.terminal.add_user_message(user_input)
                    continue
                self.terminal.handle_event(event, render_event(event))
            self.output_fn(resume_summary(record))
            return
        self.output_fn(resume_summary(record))
        rendered = render_state(record.state)
        if rendered:
            self.output_fn(rendered)

    def _command_options(self, command_name: str) -> list[CommandChoice]:
        """Provide runtime-backed argument completions for slash commands."""
        if command_name == "resume":
            return [
                CommandChoice(
                    value=record.session_id,
                    display=record.name or record.session_id,
                    description=(
                        "current · " if record.session_id == self.current_session_id else ""
                    )
                    + self._session_choice_description(record),
                )
                for record in self.runtime.list_sessions(limit=100)
            ]
        if command_name == "skill":
            active = {item.get("name") for item in self.active_skills}
            return [
                CommandChoice(
                    value=skill.name,
                    display=skill.name,
                    description=("active · " if skill.name in active else "")
                    + skill.description,
                )
                for skill in self.runtime.skill_loader.discover()
            ]
        return []

    @staticmethod
    def _session_choice_description(record: Any) -> str:
        return session_choice_description(record)

    def _plan_text(self) -> str:
        if not self.current_state:
            return "No active plan."
        return render_state(self.current_state) or "No active plan."

    def _status_text(self) -> str:
        state = self.current_state or {}
        usage = state.get("usage") or {}
        context = state.get("context_usage") or {}
        return (
            f"session: {self.current_session_id or 'none'}\n"
            f"model: {getattr(self.runtime.model, 'model', 'custom')}\n"
            f"mode: {self.runtime.config.mode}\n"
            f"goal: {state.get('goal') or self.current_goal or 'none'}\n"
            f"goal_complete: {state.get('goal_complete', False)}\n"
            f"context: {context.get('used_tokens', 0)}/{context.get('max_tokens', self.runtime.config.max_context_tokens)} "
            f"({context.get('source', 'unavailable')})\n"
            f"compact_at: {context.get('threshold_tokens', self.runtime.config.compact_threshold_tokens)} "
            f"({self.runtime.config.compact_threshold * 100:.1f}%)\n"
            f"requests: {usage.get('requests', 0)}\n"
            f"tokens: total={usage.get('total_tokens', 0)} input={usage.get('input_tokens', 0)} "
            f"output={usage.get('output_tokens', 0)} cached={usage.get('cached_tokens', 0)} "
            f"reasoning={usage.get('reasoning_tokens', 0)}"
        )

    def _handle_context(self, args: list[str]) -> None:
        """Show or update context-window and compaction settings."""
        if not args:
            self.output_fn(self._context_text())
            return
        action = args[0].casefold()
        try:
            if action == "reset":
                self.runtime.configure_context(reset=True)
            elif action == "max":
                self.runtime.configure_context(max_tokens=self._parse_token_count(args, action))
            elif action == "keep":
                self.runtime.configure_context(
                    keep_recent_tokens=self._parse_token_count(args, action)
                )
            elif action == "threshold":
                if len(args) != 2:
                    raise ValueError("usage: /context threshold <percent|tokens>")
                raw = args[1].strip().casefold()
                if raw.endswith("%"):
                    threshold = float(raw[:-1]) / 100
                elif any(raw.endswith(suffix) for suffix in ("k", "m")) or raw.isdigit():
                    threshold = self._parse_token_value(raw) / self.runtime.config.max_context_tokens
                else:
                    threshold = float(raw)
                self.runtime.configure_context(compact_threshold=threshold)
            else:
                raise ValueError("usage: /context [max|threshold|keep|reset] [value]")
        except (TypeError, ValueError) as exc:
            self.output_fn(str(exc))
            return
        if self.current_state:
            self.runtime._ensure_state_defaults(self.current_state)
        self.output_fn(self._context_text())

    def _context_text(self) -> str:
        usage = (self.current_state or {}).get("context_usage") or {}
        total = (self.current_state or {}).get("usage") or {}
        max_tokens = self.runtime.config.max_context_tokens
        threshold_tokens = self.runtime.config.compact_threshold_tokens
        used = int(usage.get("used_tokens", 0))
        source = str(usage.get("source") or "unavailable")
        projected = int(usage.get("projected_tokens", used))
        return (
            "Context\n"
            f"  latest input: {used:,}/{max_tokens:,} tokens ({source})\n"
            f"  projected next request: {projected:,} tokens\n"
            f"  auto compact: {threshold_tokens:,} tokens "
            f"({self.runtime.config.compact_threshold * 100:.1f}%)\n"
            f"  keep recent: {self.runtime.config.compact_keep_recent_tokens:,} tokens\n"
            "Session usage (Responses API)\n"
            f"  requests: {int(total.get('requests', 0)):,}\n"
            f"  input/output: {int(total.get('input_tokens', 0)):,}/"
            f"{int(total.get('output_tokens', 0)):,}\n"
            f"  cached/reasoning: {int(total.get('cached_tokens', 0)):,}/"
            f"{int(total.get('reasoning_tokens', 0)):,}\n"
            f"  total: {int(total.get('total_tokens', 0)):,}"
        )

    @classmethod
    def _parse_token_count(cls, args: list[str], action: str) -> int:
        if len(args) != 2:
            raise ValueError(f"usage: /context {action} <tokens>")
        return cls._parse_token_value(args[1])

    @staticmethod
    def _parse_token_value(value: str) -> int:
        text = value.strip().casefold().replace("_", "").replace(",", "")
        multiplier = 1
        if text.endswith("k"):
            multiplier, text = 1000, text[:-1]
        elif text.endswith("m"):
            multiplier, text = 1_000_000, text[:-1]
        try:
            tokens = int(float(text) * multiplier)
        except ValueError as exc:
            raise ValueError(f"invalid token count: {value}") from exc
        if tokens < 1:
            raise ValueError("token count must be positive")
        return tokens

    def _tui_snapshot(self) -> dict[str, Any]:
        """提供只读快照，供内联输入框底栏按需渲染。"""
        return {
            "session_id": self.current_session_id,
            "model": getattr(self.runtime.model, "model", "custom"),
            "mode": self.runtime.config.mode,
            "goal": self.current_goal,
            "max_context_tokens": self.runtime.config.max_context_tokens,
            "state": self.current_state or {},
        }

    @staticmethod
    def _compact_argument(value: Any, limit: int = 120) -> str:
        text = " ".join(str(value).split())
        return text if len(text) <= limit else text[: limit - 1] + "…"
