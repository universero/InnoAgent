"""Interactive CLI for the unified runtime."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from core.runtime.agent import InnoAgentRuntime
from view.commands import Command, CommandChoice, command_help_text, parse_command
from view.render import render_event, render_sessions, render_state, render_tools
from view.resume import pick_session, resume_summary
from view.terminal import TerminalIO


HELP_TEXT = command_help_text()


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
                self.output_fn("请选择 1（允许一次）、2（当前目录一直允许）或 3（拒绝）。")
                return
            self.terminal.add_user_message(f"Approval: {decision}")
            self.terminal.set_approval()
            await self._run_tui_work(lambda: self._resolve_approval(decision), "Running approved tool")
            await self._prompt_for_user_question()
            return

        if command:
            self.terminal.add_user_message(text)
            if command.name == "model":
                await self._select_model()
                return
            if command.name == "resume" and not command.args:
                await self._select_session()
                await self._prompt_for_user_question()
                return
            if command.name == "skill" and not command.args:
                await self._select_skill()
                return
            if command.name == "mode" and not command.args:
                await self._select_mode()
                return
            if command.name in {"clear", "new"}:
                self.terminal.clear()
            await self._run_tui_work(
                lambda: self._handle_command(command),
                "Running command",
                accepts_steering=False,
            )
            await self._prompt_for_user_question()
            return

        self.terminal.add_user_message(text)
        await self._run_tui_work(
            lambda: self._handle_task(text, request_approval=False),
            "Thinking",
            accepts_steering=True,
        )
        await self._prompt_for_user_question()

    async def _select_model(self) -> None:
        """Fetch provider models, then switch using the shared inline selector."""
        assert self.terminal is not None
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

        current = str(getattr(self.runtime.model, "model", ""))
        selected = await self.terminal.select("Select model", models, current=current)
        if selected is None or selected == current:
            self.terminal.refresh()
            return
        try:
            updated = await asyncio.to_thread(self.runtime.update_model, selected, None)
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

    async def _prompt_for_user_question(self) -> None:
        """Resolve structured model questions and continue the same session."""
        assert self.terminal is not None
        while True:
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
        detail = ", ".join(
            f"{key}={self._compact_argument(value)}"
            for key, value in (pending.get("arguments") or {}).items()
        )
        self.terminal.set_approval(
            str(pending.get("tool_name") or "tool"),
            detail,
        )

    @staticmethod
    def _approval_decision(text: str) -> str | None:
        normalized = text.strip().lower()
        if normalized.startswith("/approve "):
            normalized = normalized.removeprefix("/approve ").strip()
        return {
            "1": "allow_once",
            "once": "allow_once",
            "allow_once": "allow_once",
            "2": "allow_always",
            "always": "allow_always",
            "allow_always": "allow_always",
            "3": "deny",
            "deny": "deny",
        }.get(normalized)

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

    def _handle_task(self, text: str, *, request_approval: bool = True) -> None:
        """Submit natural language or resolve a pending approval."""
        if self.current_state and self.current_session_id and self.current_state.get(
            "pending_confirmation"
        ):
            mapped = {
                "1": "allow_once",
                "once": "allow_once",
                "允许一次": "allow_once",
                "2": "allow_always",
                "always": "allow_always",
                "一直允许": "allow_always",
                "3": "deny",
                "deny": "deny",
                "拒绝": "deny",
            }.get(text.strip().lower())
            if mapped:
                self.current_state = self.runtime.resolve_approval(
                    self.current_session_id,
                    mapped,  # type: ignore[arg-type]
                )
                return

        result = self.runtime.invoke(
            text,
            session_id=self.current_session_id,
            goal=self.current_goal,
            active_skills=self.active_skills,
        )
        self.current_session_id = str(result.get("session_id"))
        self.current_state = result
        self.active_skills = list(result.get("active_skills", []))
        if request_approval and result.get("pending_confirmation"):
            self._request_approval()

    def _resolve_approval(self, decision: str) -> None:
        if not self.current_session_id:
            return
        self.current_state = self.runtime.resolve_approval(
            self.current_session_id,
            decision,  # type: ignore[arg-type]
        )

    def _request_approval(self) -> None:
        pending = (self.current_state or {}).get("pending_confirmation") or {}
        if not self.current_session_id:
            return
        if self.terminal:
            detail = ", ".join(
                f"{key}={value}" for key, value in (pending.get("arguments") or {}).items()
            )
            decision = self.terminal.choose_approval(
                str(pending.get("tool_name") or "tool"),
                detail,
            )
            self.current_state = self.runtime.resolve_approval(
                self.current_session_id,
                decision,  # type: ignore[arg-type]
            )
        else:
            self.output_fn(
                "需要权限确认：/approve once、/approve always 或 /approve deny"
            )

    def _handle_command(self, command: Command | None) -> None:
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
            self.current_goal = None if value.lower() in {"", "off", "none", "clear"} else value
            self.output_fn(f"goal: {self.current_goal or 'none'}")
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
            usage = (self.current_state or {}).get("context_usage") or {}
            self.output_fn(
                f"context: {usage.get('used_tokens', 0)}/{usage.get('max_tokens', self.runtime.config.max_context_tokens)} "
                f"tokens ({usage.get('percent_used', 0)}% used)"
            )
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
            choice = args[0].lower() if args else ""
            decisions = {"once": "allow_once", "always": "allow_always", "deny": "deny"}
            if choice not in decisions:
                self.output_fn("usage: /approve once|always|deny")
                return
            try:
                self.current_state = self.runtime.resolve_approval(
                    self.current_session_id,
                    decisions[choice],  # type: ignore[arg-type]
                )
            except ValueError as exc:
                self.output_fn(str(exc))
        elif name == "steer":
            self.output_fn("No running turn. During execution use /steer [now] <instruction>.")
        elif name == "mode":
            if not args:
                self.output_fn(f"mode: {self.runtime.config.mode}")
                return
            mode = "ask" if args[0] == "confirm" else args[0]
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
        updated = record.updated_at.astimezone().strftime("%m-%d %H:%M")
        goal = str(record.goal or "no goal")
        return f"{updated} · {record.session_id} · {goal}"

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
            f"context: {context.get('used_tokens', 0)}/{context.get('max_tokens', self.runtime.config.max_context_tokens)}\n"
            f"session_tokens: {usage.get('total_tokens', 0)}"
        )

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
