"""Theme and semantic lexer for the full-screen TUI."""

from __future__ import annotations

from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style


TUI_STYLE = Style.from_dict(
    {
        "root": "bg:#0b1016 #d8dee9",
        "header": "bg:#111923 #9aa7b7",
        "header.brand": "bg:#e5a33b #111923 bold",
        "header.path": "#8aa6b8",
        "header.ready": "#79c99e bold",
        "header.busy": "#e5a33b bold",
        "frame.border": "#314454",
        "transcript": "bg:#0b1016 #d8dee9",
        "transcript.user": "#e5a33b bold",
        "transcript.agent": "#79c99e bold",
        "transcript.thinking": "#8aa6b8 italic",
        "transcript.tool": "#73a7d8 bold",
        "transcript.success": "#79c99e bold",
        "transcript.warning": "#e5a33b bold",
        "transcript.error": "#e06c75 bold",
        "transcript.plan": "#d2b46f bold",
        "transcript.muted": "#748393",
        "sidebar": "bg:#0f161f #bac4cf",
        "sidebar.heading": "#e5a33b bold",
        "sidebar.label": "#748393",
        "sidebar.value": "#d8dee9",
        "sidebar.good": "#79c99e",
        "sidebar.active": "#e5a33b",
        "sidebar.blocked": "#e06c75",
        "approval": "bg:#2a2113 #f0d49b",
        "approval.title": "bg:#e5a33b #111923 bold",
        "approval.key": "bg:#3a2e19 #f6c85f bold",
        "input": "bg:#111923 #e5e9f0",
        "input.prompt": "bg:#111923 #e5a33b bold",
        "status": "bg:#111923 #748393",
        "status.key": "#8aa6b8 bold",
        "completion-menu.completion": "bg:#17222d #d8dee9",
        "completion-menu.completion.current": "bg:#e5a33b #111923 bold",
        "scrollbar.background": "bg:#111923",
        "scrollbar.button": "bg:#405467",
    }
)


class TranscriptLexer(Lexer):
    """Color transcript headings without storing style metadata in the Buffer."""

    _PREFIX_STYLES = {
        "◆": "class:transcript.user",
        "◇": "class:transcript.agent",
        "◌": "class:transcript.thinking",
        "●": "class:transcript.tool",
        "✓": "class:transcript.success",
        "!": "class:transcript.warning",
        "×": "class:transcript.error",
        "▦": "class:transcript.plan",
        "·": "class:transcript.muted",
    }

    def lex_document(self, document: Document):
        def get_line(lineno: int) -> StyleAndTextTuples:
            line = document.lines[lineno]
            style = self._PREFIX_STYLES.get(line[:1], "class:transcript")
            return [(style, line)]

        return get_line
