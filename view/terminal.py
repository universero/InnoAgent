"""Codex-style inline terminal UI built with prompt_toolkit."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Any

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.completion import Completer, Completion, CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import AnyFormattedText, FormattedText, StyleAndTextTuples
from prompt_toolkit.input.base import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension
from prompt_toolkit.keys import Keys
from prompt_toolkit.output.base import Output
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.utils import get_cwidth

from core import __version__
from view.commands import COMMAND_SPECS, CommandChoice, static_command_choices
from view.tui_render import present_event
from view.tui_theme import OUTPUT_STYLE, TUI_STYLE


MAX_TRANSCRIPT_CHARS = 120_000
MAX_COMPLETION_ROWS = 10


SubmitHandler = Callable[[str], Awaitable[None] | None]
StateProvider = Callable[[], dict[str, Any]]
CommandOptionProvider = Callable[[str], list[CommandChoice]]
PrintOperation = tuple[StyleAndTextTuples, str]
CUSTOM_INPUT_OPTION = "自行输入…"


class SelectionCancelled(Exception):
    """Internal signal used when an inline selector is cancelled."""


@dataclass
class SelectionState:
    """State shared by the prompt loop and one waiting selector caller."""

    title: str
    options: list[str]
    current: str | None
    allow_custom: bool
    future: asyncio.Future[str | None]
    labels: dict[str, str] | None = None
    descriptions: dict[str, str] | None = None
    custom_input: bool = False
    selected_index: int = 0

    def values(self) -> list[str]:
        values = list(self.options)
        if self.allow_custom:
            values.append(CUSTOM_INPUT_OPTION)
        return values


class SelectionCompleter(Completer):
    """Render selector rows in the same completion menu used by slash commands."""

    def __init__(self, state: SelectionState) -> None:
        self.state = state

    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent,
    ):
        if self.state.custom_input:
            return
        prefix = document.text_before_cursor.casefold()
        values = self._ordered_values()
        for value in values:
            display = (self.state.labels or {}).get(value, value)
            description = (self.state.descriptions or {}).get(value, "")
            if prefix and not any(
                candidate.casefold().startswith(prefix)
                for candidate in (value, display)
            ):
                continue
            current = value == self.state.current
            metadata = f"current · {description}" if current and description else (
                "current" if current else description
            )
            yield Completion(
                value,
                start_position=-len(document.text_before_cursor),
                display=f"{display}  (current)" if current else display,
                display_meta=metadata,
            )

    def _ordered_values(self) -> list[str]:
        options = list(self.state.options)
        if self.state.selected_index < len(options):
            index = self.state.selected_index
            options = options[index:] + options[:index]
            return options + ([CUSTOM_INPUT_OPTION] if self.state.allow_custom else [])
        return [CUSTOM_INPUT_OPTION, *options] if self.state.allow_custom else options


class SlashCommandCompleter(Completer):
    """Complete known slash commands by prefix and show concise descriptions."""

    def __init__(self, option_provider: CommandOptionProvider | None = None) -> None:
        self.option_provider = option_provider or (lambda _: [])

    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent,
    ):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        if not any(char.isspace() for char in text):
            for spec in COMMAND_SPECS:
                command = f"/{spec.name}"
                if not command.startswith(text.lower()):
                    continue
                yield Completion(
                    command,
                    start_position=-len(text),
                    display=command,
                    display_meta=spec.description,
                )
            return

        parts = text.split(maxsplit=1)
        command_text = parts[0]
        argument = parts[1] if len(parts) > 1 else ""
        command_name = command_text.lstrip("/").lower()
        try:
            dynamic_choices = self.option_provider(command_name)
        except Exception:  # noqa: BLE001 - completion must never break the prompt
            dynamic_choices = []
        choices = static_command_choices(command_name) + dynamic_choices
        normalized = argument.casefold()
        seen: set[str] = set()
        for choice in choices:
            if choice.value in seen:
                continue
            seen.add(choice.value)
            display = choice.display or choice.value
            if normalized and not any(
                candidate.casefold().startswith(normalized)
                for candidate in (choice.value, display)
            ):
                continue
            yield Completion(
                choice.value,
                start_position=-len(argument),
                display=display,
                display_meta=choice.description,
            )


class TerminalIO:
    """Keep terminal scrollback while presenting one persistent input prompt."""

    def __init__(
        self,
        state_provider: StateProvider | None = None,
        command_option_provider: CommandOptionProvider | None = None,
        app_input: Input | None = None,
        app_output: Output | None = None,
    ) -> None:
        self.state_provider = state_provider or (lambda: {})
        self._slash_completer = SlashCommandCompleter(command_option_provider)
        self._app_output = app_output
        self._submit_handler: SubmitHandler | None = None
        self._updates: SimpleQueue[tuple[str, Any]] = SimpleQueue()
        self._print_lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._dispatch_tasks: set[asyncio.Task[Any]] = set()
        self._running = False
        self._busy = False
        self._activity = "Ready"
        self._approval: dict[str, str] | None = None
        self._selection: SelectionState | None = None
        self._stream_kind: str | None = None
        self._stream_pending = ""
        self._transcript_text = ""
        self._model = "custom"
        self._effort = ""
        self._cwd = "."
        self._mode = "ask"

        self.session: PromptSession[str] = PromptSession(
            message=self._input_prompt,
            bottom_toolbar=self._bottom_toolbar,
            placeholder=self._placeholder,
            completer=self._slash_completer,
            complete_while_typing=True,
            complete_style=CompleteStyle.COLUMN,
            reserve_space_for_menu=10,
            # prompt-toolkit 会在 history search 开启时禁用输入时补全。
            # 历史记录仍由下方 Up/Down 绑定负责导航。
            enable_history_search=False,
            key_bindings=self._key_bindings(),
            style=TUI_STYLE,
            multiline=False,
            wrap_lines=True,
            mouse_support=False,
            erase_when_done=True,
            show_frame=True,
            input=app_input,
            output=app_output,
        )
        # PromptSession 没有公开的输入窗口样式参数，只给当前输入窗口绑定局部主题，
        # 避免通过默认样式给整个终端空白区域着色。
        self.session.app.layout.current_window.style = "class:input"
        self.session.app.layout.current_window.height = self._input_height

    def _input_height(self) -> Dimension:
        """Keep the editor compact, but reserve rows while a menu is visible."""
        completion = self.session.default_buffer.complete_state
        if completion is None or not completion.completions:
            return Dimension.exact(2)
        rows = min(MAX_COMPLETION_ROWS, len(completion.completions))
        return Dimension.exact(rows + 1)

    @property
    def transcript_text(self) -> str:
        """Expose bounded rendered text for tests and debug bundles."""
        return self._transcript_text

    def run(self, submit_handler: SubmitHandler) -> None:
        """Run an inline prompt without switching to the alternate screen."""
        self._submit_handler = submit_handler
        try:
            asyncio.run(self._run_async())
        except KeyboardInterrupt:
            # 防御极短时序内未被 prompt_toolkit 键位接管的系统 SIGINT。
            self._running = False

    async def _run_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._running = True
        self._flush_direct()
        prompt_task: asyncio.Task[str] | None = None
        stop_task: asyncio.Task[bool] | None = None

        try:
            with patch_stdout(raw=True):
                while self._running:
                    selection = self._selection
                    prompt_task = asyncio.create_task(
                        self.session.prompt_async(
                            completer=(
                                SelectionCompleter(selection)
                                if selection is not None
                                else self._slash_completer
                            ),
                            complete_while_typing=selection is None,
                            pre_run=(
                                lambda: self._start_selection(selection)
                                if selection is not None
                                else None
                            ),
                        )
                    )
                    stop_task = asyncio.create_task(self._stop_event.wait())
                    done, _ = await asyncio.wait(
                        {prompt_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stop_task in done:
                        prompt_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await prompt_task
                        break

                    stop_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await stop_task
                    try:
                        text = prompt_task.result().strip()
                    except SelectionCancelled:
                        if selection is not None:
                            self._finish_selection(selection, None)
                        continue
                    except EOFError:
                        break
                    except KeyboardInterrupt:
                        if self._busy:
                            self._submit("/stop")
                        continue
                    if selection is not None:
                        self._finish_selection(selection, text or None)
                    elif text:
                        self._submit(text)
                        # 让 dispatch 先更新 busy/approval，
                        # 下一次 prompt 会立即切换语义。
                        await asyncio.sleep(0)
        finally:
            self._running = False
            for task in (prompt_task, stop_task):
                if task is not None and not task.done():
                    task.cancel()
            for task in (prompt_task, stop_task):
                if task is not None:
                    with suppress(asyncio.CancelledError, KeyboardInterrupt):
                        await task
            if self._flush_task and not self._flush_task.done():
                await self._flush_task
            if self._dispatch_tasks:
                await asyncio.gather(*tuple(self._dispatch_tasks), return_exceptions=True)
            self._flush_direct()
            self._loop = None
            self._stop_event = None

    def _submit(self, text: str) -> None:
        if self._submit_handler is None:
            return
        try:
            result = self._submit_handler(text)
        except Exception as exc:  # noqa: BLE001
            self.output(f"[error] {exc}")
            return
        if not inspect.isawaitable(result):
            return
        task = asyncio.create_task(result)
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_finished)

    def _dispatch_finished(self, task: asyncio.Task[Any]) -> None:
        self._dispatch_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.output(f"[error] {error}")

    def stop(self) -> None:
        """Stop the current prompt from the event loop or a worker thread."""
        self._running = False
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    async def select(
        self,
        title: str,
        options: list[str],
        *,
        current: str | None = None,
        allow_custom: bool = False,
        labels: dict[str, str] | None = None,
        descriptions: dict[str, str] | None = None,
    ) -> str | None:
        """Open an inline Up/Down/Enter selector and wait for its result."""
        if self._selection is not None:
            raise RuntimeError("another terminal selection is already active")
        normalized = list(dict.fromkeys(item.strip() for item in options if item.strip()))
        if not normalized and not allow_custom:
            return None
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        self._selection = SelectionState(
            title=title,
            options=normalized,
            current=current if current in normalized else None,
            allow_custom=allow_custom,
            future=future,
            labels=labels,
            descriptions=descriptions,
            custom_input=not normalized and allow_custom,
            selected_index=(
                normalized.index(current)
                if current is not None and current in normalized
                else 0
            ),
        )
        if self.session.app.is_running:
            # 结束当前空输入 prompt，让主循环立刻以选择模式重开同一个输入框。
            self.session.app.exit(result="")
        self.session.app.invalidate()
        return await future

    def set_model(self, model: str, effort: str = "") -> None:
        self._model = model
        self._effort = effort
        self.refresh()

    def banner(self, model: str, effort: str, cwd: str, mode: str) -> None:
        self._model = model
        self._effort = effort
        self._cwd = cwd
        self._mode = mode
        self._queue("startup", (model, effort, cwd))

    def output(self, value: str = "") -> None:
        self._queue("output", value)

    def stream(self, value: str, channel: str = "message") -> None:
        self._queue("stream", (channel, value))

    def handle_event(self, event: dict[str, Any], rendered: str = "") -> None:
        self._queue("event", (dict(event), rendered))

    def add_user_message(self, value: str, *, steering: bool = False) -> None:
        title = "Steer" if steering else "You"
        self._queue("block", ("warning" if steering else "user", title, value))

    def set_busy(self, busy: bool, activity: str = "Working") -> None:
        self._queue("busy", (busy, activity))

    def set_approval(self, tool: str | None = None, detail: str = "") -> None:
        self._queue(
            "approval",
            None if tool is None else {"tool": tool, "detail": detail},
        )

    def refresh(self) -> None:
        if not self._loop:
            self._flush_direct()
            return
        self._loop.call_soon_threadsafe(self.session.app.invalidate)

    def clear(self) -> None:
        self._queue("clear", None)

    def _queue(self, action: str, payload: Any) -> None:
        # Runtime 可能位于工作线程；输出统一切回 prompt_toolkit 的事件循环。
        self._updates.put((action, payload))
        if self._loop and self._running:
            self._loop.call_soon_threadsafe(self._schedule_flush)
        else:
            self._flush_direct()

    def _schedule_flush(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_async())

    async def _flush_async(self) -> None:
        # 合并同一模型 chunk 附近的事件，减少 prompt 重绘和输出闪烁。
        try:
            await asyncio.sleep(0.01)
            operations = self._drain_updates()
            if operations:
                await run_in_terminal(lambda: self._render_operations(operations))
            self.session.app.invalidate()
        finally:
            self._flush_task = None
            # 事件可能在 run_in_terminal() 期间到达；
            # 必须再次调度，避免尾部输出滞留。
            if self._running and not self._updates.empty():
                self._schedule_flush()

    def _flush_direct(self) -> None:
        operations = self._drain_updates()
        if operations:
            self._render_operations(operations)

    def _drain_updates(self) -> list[PrintOperation]:
        operations: list[PrintOperation] = []
        while True:
            try:
                action, payload = self._updates.get_nowait()
            except Empty:
                break
            if action == "block":
                operations.extend(self._append_block(*payload))
            elif action == "startup":
                operations.extend(self._append_startup(*payload))
            elif action == "output":
                operations.extend(self._append_output(str(payload)))
            elif action == "stream":
                operations.extend(self._append_stream(*payload))
            elif action == "event":
                operations.extend(self._apply_event(*payload))
            elif action == "busy":
                self._busy, self._activity = payload
            elif action == "approval":
                operations.extend(self._apply_approval(payload))
            elif action == "clear":
                self._transcript_text = ""
                self._stream_kind = None
                self._stream_pending = ""
                operations.append(([('', "\x1b[2J\x1b[H")], ""))
        return operations

    def _append_startup(self, model: str, effort: str, cwd: str) -> list[PrintOperation]:
        columns = self.session.app.output.get_size().columns
        width = max(40, min(72, columns - 2))
        inner_width = width - 2
        effort_text = effort.strip()
        model_text = (
            model
            if not effort_text or effort_text == "none"
            else f"{model} · {effort_text}"
        )

        def fit(value: str) -> str:
            available = inner_width - 4
            if get_cwidth(value) <= available:
                return value
            tail = value
            while tail and get_cwidth(tail) > available - 1:
                tail = tail[1:]
            return f"…{tail}"

        def row(value: str, style: str) -> PrintOperation:
            content = f"  {fit(value)}"
            padding = " " * max(0, inner_width - get_cwidth(content))
            return (
                [
                    ("class:startup.border", "│"),
                    (style, content),
                    ("", padding),
                    ("class:startup.border", "│"),
                ],
                "\n",
            )

        border = "─" * inner_width
        return [
            ([("class:startup.border", f"╭{border}╮")], "\n"),
            row(f">_ InnoAgent  (v{__version__})", "class:startup.title"),
            row("", "class:startup.body"),
            row(f"model      {model_text}", "class:startup.body"),
            row(f"directory  {self._display_path(cwd)}", "class:startup.body"),
            ([("class:startup.border", f"╰{border}╯")], "\n\n"),
        ]

    def _append_output(self, value: str) -> list[PrintOperation]:
        text = value.strip()
        if not text:
            return []
        if text.startswith("[error]") or text.lower().startswith("error"):
            return self._append_block("error", "Error", text.removeprefix("[error]").strip())
        if text.startswith("InnoAgent commands"):
            return self._append_block("plan", "Commands", text.split("\n", 1)[-1].strip())
        return self._append_block("muted", "Info", text)

    def _append_stream(self, channel: str, value: str) -> list[PrintOperation]:
        if not value:
            return []
        operations: list[PrintOperation] = []
        kind = {
            "reasoning": "thinking",
            "plan": "plan",
            "reflect": "plan",
        }.get(channel, "agent")
        if self._stream_kind != kind:
            operations.extend(self._close_stream())
            marker, title = {
                "thinking": ("·", "Thinking"),
                "plan": ("▦", "Planning" if channel == "plan" else "Reflection"),
            }.get(kind, ("•", "Agent"))
            operations.append(([(f"class:output.{kind}", f"{marker} {title}")], "\n"))
            self._stream_kind = kind
            self._stream_pending = ""

        # run_in_terminal 每次恢复 Prompt 后不会保留上一批输出的水平光标位置。
        # 只输出完整行，尾部片段留到下一批或完成事件，避免增量 token 相互覆盖。
        self._stream_pending += value
        body_style = "class:output.body" if kind == "agent" else f"class:output.{kind}"
        while "\n" in self._stream_pending:
            line, self._stream_pending = self._stream_pending.split("\n", 1)
            operations.append(([(body_style, f"  {line}")], "\n"))
        return operations

    def _close_stream(self) -> list[PrintOperation]:
        if self._stream_kind is None:
            return []
        kind = self._stream_kind
        pending = self._stream_pending
        self._stream_kind = None
        self._stream_pending = ""
        operations: list[PrintOperation] = []
        if pending:
            body_style = "class:output.body" if kind == "agent" else f"class:output.{kind}"
            operations.append(([(body_style, f"  {pending}")], "\n"))
        operations.append(([('', "")], "\n"))
        return operations

    def _append_block(self, tone: str, title: str, body: str = "") -> list[PrintOperation]:
        operations = self._close_stream()
        marker = {
            "user": "›",
            "agent": "•",
            "thinking": "·",
            "tool": "↳",
            "success": "✓",
            "warning": "!",
            "error": "×",
            "plan": "▦",
            "muted": "·",
        }.get(tone, "·")
        fragments: StyleAndTextTuples = [
            (f"class:output.{tone}", f"{marker} {title}"),
        ]
        if body:
            fragments.append(("", "\n"))
            for index, line in enumerate(body.splitlines()):
                if index:
                    fragments.append(("", "\n"))
                fragments.append(("class:output.body", f"  {line}"))
        operations.append((fragments, "\n\n"))
        return operations

    def _apply_event(self, event: dict[str, Any], rendered: str) -> list[PrintOperation]:
        presentation = present_event(event, rendered)
        operations: list[PrintOperation] = []
        if presentation.activity:
            self._activity = presentation.activity
        if presentation.stream:
            operations.extend(self._append_stream(*presentation.stream))
        if presentation.block:
            operations.extend(self._append_block(*presentation.block))
        if event.get("type") in {
            "response.completed",
            "turn.completed",
            "turn.failed",
            "response.failed",
        }:
            operations.extend(self._close_stream())
        return operations

    def _apply_approval(self, approval: dict[str, str] | None) -> list[PrintOperation]:
        if approval is None:
            self._approval = None
            return []
        if approval == self._approval:
            return []
        self._approval = approval
        self._activity = "Approval required"
        detail = approval.get("detail") or "This operation needs permission."
        return self._append_block(
            "warning",
            f"Approval · {approval.get('tool', 'tool')}",
            f"{detail}\n1 Allow once   2 Always here   3 Deny",
        )

    def _render_operations(self, operations: list[PrintOperation]) -> None:
        with self._print_lock:
            for fragments, end in operations:
                plain = "".join(text for _, text in fragments) + end
                self._record(plain)
                print_formatted_text(
                    FormattedText(fragments),
                    style=OUTPUT_STYLE,
                    end=end,
                    output=self._app_output,
                )

    def _record(self, value: str) -> None:
        self._transcript_text += value
        if len(self._transcript_text) <= MAX_TRANSCRIPT_CHARS:
            return
        trimmed = self._transcript_text[-MAX_TRANSCRIPT_CHARS:]
        first_break = trimmed.find("\n")
        self._transcript_text = trimmed[first_break + 1 :] if first_break >= 0 else trimmed

    def _input_prompt(self) -> AnyFormattedText:
        if self._selection is not None:
            label = "answer" if self._selection.custom_input else self._selection.title
            return [("class:prompt.selection", f"{label} › ")]
        if self._approval is not None:
            return [("class:prompt.approval", "approve › ")]
        if self._busy:
            return [("class:prompt.busy", "steer › ")]
        return [("class:prompt", "› ")]

    def _placeholder(self) -> AnyFormattedText:
        if self._selection is not None:
            if self._selection.custom_input:
                return [("class:placeholder", "Type your answer")]
            return [("class:placeholder", "Choose an option")]
        if self._approval is not None:
            return [("class:placeholder", "Choose 1, 2, or 3")]
        if self._busy:
            return [("class:placeholder", "Correct the current turn or type /stop")]
        return [("class:placeholder", "Ask InnoAgent to do anything")]

    def _bottom_toolbar(self) -> AnyFormattedText:
        if self._selection is not None:
            hint = (
                " Type your answer  ·  Enter confirm  ·  Esc back"
                if self._selection.custom_input
                else (
                    " ↑↓ navigate  ·  Enter confirm  ·  Esc cancel"
                    "  ·  "
                    + (self._selection.labels or {}).get(
                        self._selection.values()[self._selection.selected_index],
                        self._selection.values()[self._selection.selected_index],
                    )
                )
            )
            return [("class:toolbar.selection", hint)]
        snapshot = self._safe_snapshot()
        state = snapshot.get("state") or {}
        context = state.get("context_usage") or {}
        usage = state.get("usage") or {}
        model = str(snapshot.get("model") or self._model)
        mode = str(snapshot.get("mode") or self._mode)
        percent = float(context.get("percent_used", 0))
        tokens = int(usage.get("total_tokens", 0))
        goal = state.get("goal") or snapshot.get("goal")
        tasks = state.get("tasks") or []
        completed_tasks = sum(1 for task in tasks if task.get("status") == "done")
        effort = self._effort.strip()
        activity = self._activity.strip()
        fragments: StyleAndTextTuples = [
            ("class:toolbar.model", f" {model}"),
            ("class:toolbar.model", f" · {effort}" if effort and effort != "none" else ""),
            ("class:toolbar.separator", "  ·  "),
            ("class:toolbar.mode", mode.upper()),
            ("class:toolbar.separator", "  ·  "),
            ("class:toolbar.path", self._display_path(self._cwd)),
            ("class:toolbar.separator", "    "),
            ("class:toolbar.metric", f"{tokens:,} tokens  ·  {percent:.0f}% context"),
        ]
        if goal:
            fragments.append(("class:toolbar.progress", "  ·  Goal"))
        if tasks:
            fragments.append(
                ("class:toolbar.progress", f"  ·  Tasks {completed_tasks}/{len(tasks)}")
            )
        # Ready 只是空闲默认值，不占用有限的状态栏空间。
        if activity and activity.casefold() != "ready":
            fragments.extend(
                [
                    ("class:toolbar.separator", "  ·  "),
                    ("class:toolbar.activity", activity),
                ]
            )
        fragments.append(("class:toolbar", " "))
        return fragments

    def _safe_snapshot(self) -> dict[str, Any]:
        try:
            return self.state_provider() or {}
        except Exception:  # noqa: BLE001
            return {}

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("c-c", eager=True)
        @bindings.add(Keys.SIGINT, eager=True)
        def _interrupt(event) -> None:
            if self._selection is not None:
                selection = self._selection
                event.current_buffer.reset()
                self._finish_selection(selection, None)
                event.app.exit(result="")
                self._submit("/quit")
                return
            # 无论空闲还是执行中都走 /quit；CLI 会在执行中先请求安全停止。
            event.current_buffer.reset()
            event.app.exit(result="/quit")

        @bindings.add("c-l")
        def _clear(event) -> None:
            event.app.renderer.clear()

        @bindings.add("f1")
        def _help(event) -> None:
            event.app.exit(result="/help")

        @bindings.add("c-q")
        def _quit(event) -> None:
            if self._selection is not None:
                selection = self._selection
                event.current_buffer.reset()
                self._finish_selection(selection, None)
                event.app.exit(result="")
                self._submit("/quit")
                return
            event.app.exit(result="/quit")

        @bindings.add("enter", eager=True)
        def _accept_selection(event) -> None:
            selection = self._selection
            if selection is None:
                event.current_buffer.validate_and_handle()
                return
            buffer = event.current_buffer
            if selection.custom_input:
                if buffer.text.strip():
                    event.app.exit(result=buffer.text.strip())
                return
            values = selection.values()
            if not values:
                return
            value = values[selection.selected_index]
            if value == CUSTOM_INPUT_OPTION:
                selection.custom_input = True
                buffer.reset()
                event.app.invalidate()
                return
            event.app.exit(result=value)

        @bindings.add("tab", eager=True)
        def _complete_argument(event) -> None:
            if self._selection is not None:
                self._move_selection(event, 1)
                return
            buffer = event.current_buffer
            completions = list(
                self._slash_completer.get_completions(
                    buffer.document,
                    CompleteEvent(completion_requested=True),
                )
            )
            if completions:
                buffer.apply_completion(completions[0])

        @bindings.add("up", eager=True)
        def _previous_selection(event) -> None:
            self._move_selection(event, -1)

        @bindings.add("down", eager=True)
        def _next_selection(event) -> None:
            self._move_selection(event, 1)

        @bindings.add("escape", eager=True)
        def _cancel_selection(event) -> None:
            selection = self._selection
            if selection is None:
                event.current_buffer.cancel_completion()
                return
            if selection.custom_input and selection.options:
                selection.custom_input = False
                event.current_buffer.reset()
                self._start_selection(selection)
                event.app.invalidate()
                return
            event.app.exit(exception=SelectionCancelled())

        return bindings

    def _start_selection(self, selection: SelectionState) -> None:
        if selection.custom_input:
            return
        buffer = self.session.app.current_buffer
        buffer.start_completion(select_first=True)

    def _move_selection(self, event: Any, offset: int) -> None:
        selection = self._selection
        if selection is None:
            if event.current_buffer.complete_state is not None:
                if offset < 0:
                    event.current_buffer.complete_previous()
                else:
                    event.current_buffer.complete_next()
                return
            if offset < 0:
                event.current_buffer.history_backward()
            else:
                event.current_buffer.history_forward()
            return
        if selection.custom_input:
            return
        values = selection.values()
        if not values:
            return
        selection.selected_index = (selection.selected_index + offset) % len(values)
        event.current_buffer.reset()
        event.current_buffer.start_completion(select_first=True)
        event.app.invalidate()

    def _finish_selection(
        self,
        selection: SelectionState,
        result: str | None,
    ) -> None:
        if self._selection is selection:
            self._selection = None
        if not selection.future.done():
            selection.future.set_result(result)
        self.session.app.invalidate()

    @staticmethod
    def _display_path(value: str) -> str:
        path = Path(value).expanduser()
        home = Path.home()
        try:
            relative = path.relative_to(home)
        except ValueError:
            return str(path)
        return "~" if not relative.parts else f"~/{relative}"
