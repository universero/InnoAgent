"""Full-screen terminal UI built with prompt_toolkit."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from queue import Empty, SimpleQueue
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import AnyFormattedText, StyleAndTextTuples
from prompt_toolkit.input.base import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.output.base import Output
from prompt_toolkit.widgets import Frame, TextArea

from view.tui_render import (
    approval_fragments,
    header_fragments,
    present_event,
    sidebar_fragments,
    status_fragments,
)
from view.tui_theme import TUI_STYLE, TranscriptLexer


COMMANDS = [
    "/help", "/status", "/context", "/goal", "/plan", "/tasks",
    "/compact", "/permissions", "/mode", "/model", "/tools", "/skills",
    "/skill", "/approve", "/steer", "/new", "/resume", "/sessions",
    "/rename", "/clear", "/stop", "/quit",
]

MAX_TRANSCRIPT_CHARS = 120_000


SubmitHandler = Callable[[str], Awaitable[None] | None]
StateProvider = Callable[[], dict[str, Any]]


class TerminalIO:
    """A compact full-screen TUI with transcript, context rail and fixed input."""

    def __init__(
        self,
        footer: Callable[[], str] | None = None,
        state_provider: StateProvider | None = None,
        app_input: Input | None = None,
        app_output: Output | None = None,
    ) -> None:
        self.footer = footer or (lambda: "")
        self.state_provider = state_provider or (lambda: {})
        self._submit_handler: SubmitHandler | None = None
        self._updates: SimpleQueue[tuple[str, Any]] = SimpleQueue()
        self._busy = False
        self._activity = "Ready"
        self._approval: dict[str, str] | None = None
        self._stream_kind: str | None = None
        self._transcript_text = ""
        self._transcript_dirty = False
        self._model = "custom"
        self._effort = ""
        self._cwd = "."
        self._mode = "ask"

        self.output_field = TextArea(
            text="",
            lexer=TranscriptLexer(),
            scrollbar=True,
            focusable=True,
            focus_on_click=True,
            wrap_lines=True,
            read_only=True,
            style="class:transcript",
        )
        self.input_field = TextArea(
            height=1,
            multiline=False,
            prompt=self._input_prompt,
            completer=WordCompleter(COMMANDS, sentence=True),
            complete_while_typing=True,
            accept_handler=self._accept_input,
            style="class:input",
        )
        self.header = Window(
            content=FormattedTextControl(self._header_fragments),
            height=1,
            style="class:header",
        )
        self.sidebar = Window(
            content=FormattedTextControl(self._sidebar_fragments),
            width=Dimension(min=24, preferred=30, max=34),
            wrap_lines=True,
            style="class:sidebar",
        )
        self.approval_bar = ConditionalContainer(
            Window(
                content=FormattedTextControl(self._approval_fragments),
                height=3,
                wrap_lines=True,
                style="class:approval",
            ),
            filter=Condition(lambda: self._approval is not None),
        )
        self.status = Window(
            content=FormattedTextControl(self._status_fragments),
            height=1,
            style="class:status",
        )

        sidebar_panel = ConditionalContainer(
            Frame(
                self.sidebar,
                title=" Context ",
                style="class:frame.border",
                height=Dimension(weight=1),
            ),
            filter=Condition(self._sidebar_visible),
        )
        body = VSplit(
            [
                Frame(
                    self.output_field,
                    title=" Conversation ",
                    style="class:frame.border",
                    height=Dimension(weight=1),
                ),
                sidebar_panel,
            ],
            padding=1,
            padding_char=" ",
        )
        root = HSplit(
            [
                self.header,
                body,
                self.approval_bar,
                Frame(self.input_field, title=self._input_title, style="class:frame.border"),
                self.status,
            ],
            style="class:root",
        )
        self.application: Application[None] = Application(
            layout=Layout(root, focused_element=self.input_field),
            key_bindings=self._key_bindings(),
            style=TUI_STYLE,
            full_screen=True,
            mouse_support=True,
            enable_page_navigation_bindings=True,
            refresh_interval=0.1,
            before_render=self._before_render,
            input=app_input,
            output=app_output,
        )

    def run(self, submit_handler: SubmitHandler) -> None:
        """Run one persistent application for the lifetime of the CLI."""
        self._submit_handler = submit_handler
        self.application.run()

    def stop(self) -> None:
        if self.application.is_running:
            self.application.exit()

    def banner(self, model: str, effort: str, cwd: str, mode: str) -> None:
        self._model = model
        self._effort = effort
        self._cwd = cwd
        self._mode = mode
        self._queue(
            "block",
            (
                "agent",
                "InnoAgent",
                "LangGraph coding agent ready. Type a task or /help.",
            ),
        )

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
        self.application.invalidate()

    def clear(self) -> None:
        self._queue("clear", None)

    def _queue(self, action: str, payload: Any) -> None:
        # Runtime 可能位于后台线程；所有控件更新统一回到 TUI render cycle。
        self._updates.put((action, payload))
        self.application.invalidate()

    def _before_render(self, _: Application[Any]) -> None:
        while True:
            try:
                action, payload = self._updates.get_nowait()
            except Empty:
                break
            if action == "block":
                self._append_block(*payload)
            elif action == "output":
                self._append_output(str(payload))
            elif action == "stream":
                self._append_stream(*payload)
            elif action == "event":
                self._apply_event(*payload)
            elif action == "busy":
                self._busy, self._activity = payload
            elif action == "approval":
                self._approval = payload
                if payload is not None:
                    self._activity = "Approval required"
            elif action == "clear":
                self._transcript_text = ""
                self._transcript_dirty = True
                self._stream_kind = None
        if self._transcript_dirty:
            self.output_field.buffer.set_document(
                Document(
                    self._transcript_text,
                    cursor_position=len(self._transcript_text),
                ),
                bypass_readonly=True,
            )
            self._transcript_dirty = False

    def _accept_input(self, buffer: Buffer) -> bool:
        text = buffer.text.strip()
        buffer.set_document(Document(""), bypass_readonly=True)
        if not text or self._submit_handler is None:
            return True
        result = self._submit_handler(text)
        if inspect.isawaitable(result):
            self.application.create_background_task(result)
        return True

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("c-c")
        def _cancel(_event) -> None:
            if self._busy and self._submit_handler is not None:
                result = self._submit_handler("/stop")
                if inspect.isawaitable(result):
                    self.application.create_background_task(result)
            else:
                self.input_field.buffer.set_document(Document(""), bypass_readonly=True)

        @bindings.add("c-l")
        def _clear(_event) -> None:
            self.clear()

        @bindings.add("c-q")
        @bindings.add("c-d")
        def _quit(event) -> None:
            if self.input_field.text:
                self.input_field.buffer.delete_before_cursor(len(self.input_field.text))
                return
            if self._submit_handler is not None:
                result = self._submit_handler("/quit")
                if inspect.isawaitable(result):
                    self.application.create_background_task(result)
            else:
                event.app.exit()

        @bindings.add("f1")
        def _help(_event) -> None:
            if self._submit_handler is not None:
                result = self._submit_handler("/help")
                if inspect.isawaitable(result):
                    self.application.create_background_task(result)

        @bindings.add("escape")
        def _focus_input(event) -> None:
            event.app.layout.focus(self.input_field)

        return bindings

    def _append_output(self, value: str) -> None:
        text = value.strip()
        if not text:
            return
        if text.startswith("[error]") or text.lower().startswith("error"):
            self._append_block("error", "Error", text.removeprefix("[error]").strip())
        elif text.startswith("InnoAgent commands"):
            self._append_block("plan", "Commands", text.split("\n", 1)[-1].strip())
        else:
            self._append_block("muted", "Info", text)

    def _append_stream(self, channel: str, value: str) -> None:
        if not value:
            return
        kind = {
            "reasoning": "thinking",
            "plan": "plan",
            "reflect": "plan",
        }.get(channel, "agent")
        if self._stream_kind != kind:
            self._close_stream()
            marker, title = {
                "thinking": ("◌", "Thinking"),
                "plan": ("▦", "Planning" if channel == "plan" else "Reflection"),
            }.get(kind, ("◇", "Agent"))
            self._insert_text(f"{marker} {title}\n  ")
            self._stream_kind = kind
        self._insert_text(value)

    def _close_stream(self) -> None:
        if self._stream_kind is not None:
            self._insert_text("\n\n")
            self._stream_kind = None

    def _append_block(self, tone: str, title: str, body: str = "") -> None:
        self._close_stream()
        marker = {
            "user": "◆",
            "agent": "◇",
            "thinking": "◌",
            "tool": "●",
            "success": "✓",
            "warning": "!",
            "error": "×",
            "plan": "▦",
            "muted": "·",
        }.get(tone, "·")
        lines = [f"{marker} {title}"]
        if body:
            lines.extend(f"  {line}" for line in body.splitlines())
        self._insert_text("\n".join(lines) + "\n\n")

    def _insert_text(self, value: str) -> None:
        self._transcript_text += value
        if len(self._transcript_text) > MAX_TRANSCRIPT_CHARS:
            trimmed = self._transcript_text[-MAX_TRANSCRIPT_CHARS:]
            first_break = trimmed.find("\n")
            if first_break >= 0:
                trimmed = trimmed[first_break + 1 :]
            self._transcript_text = trimmed
        self._transcript_dirty = True

    def _apply_event(self, event: dict[str, Any], rendered: str) -> None:
        presentation = present_event(event, rendered)
        if presentation.activity:
            self._activity = presentation.activity
        if presentation.stream:
            self._append_stream(*presentation.stream)
        if presentation.block:
            self._append_block(*presentation.block)

    def _input_prompt(self) -> AnyFormattedText:
        if self._approval is not None:
            label = "approve › "
        elif self._busy:
            label = "steer › "
        else:
            label = "message › "
        return [("class:input.prompt", label)]

    def _input_title(self) -> AnyFormattedText:
        if self._approval is not None:
            return " Decision "
        if self._busy:
            return " Steer current turn "
        return " Message "

    def _header_fragments(self) -> StyleAndTextTuples:
        return header_fragments(self._cwd, self._activity, self._busy)

    def _sidebar_fragments(self) -> StyleAndTextTuples:
        return sidebar_fragments(self._safe_snapshot(), self._model, self._mode)

    def _approval_fragments(self) -> StyleAndTextTuples:
        return approval_fragments(self._approval)

    def _status_fragments(self) -> StyleAndTextTuples:
        return status_fragments()

    def _safe_snapshot(self) -> dict[str, Any]:
        try:
            return self.state_provider() or {}
        except Exception:
            return {}

    def _sidebar_visible(self) -> bool:
        try:
            return self.application.output.get_size().columns >= 72
        except AttributeError:
            return True
