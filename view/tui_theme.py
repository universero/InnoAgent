"""Semantic colors for the inline terminal UI."""

from prompt_toolkit.styles import Style


# 不定义全局背景色，避免 PromptSession 把终端空白区整体染色。
TUI_STYLE = Style.from_dict(
    {
        "frame.border": "bg:#eff6ff #60a5fa",
        "input": "bg:#eff6ff #0f172a",
        "prompt": "bg:#eff6ff #2563eb bold",
        "prompt.busy": "bg:#eff6ff #1d4ed8 bold",
        "prompt.approval": "bg:#eff6ff #1d4ed8 bold",
        "placeholder": "bg:#eff6ff #64748b italic",
        "toolbar": "bg:#0f2747 #bfdbfe",
        "toolbar.model": "bg:#0f2747 #ffffff bold",
        "toolbar.path": "bg:#0f2747 #93c5fd",
        "toolbar.separator": "bg:#0f2747 #60a5fa",
        "toolbar.mode": "bg:#1d4ed8 #ffffff bold",
        "toolbar.metric": "bg:#0f2747 #dbeafe",
        "toolbar.progress": "bg:#0f2747 #93c5fd bold",
        "toolbar.activity": "bg:#2563eb #ffffff bold",
        "bottom-toolbar": "bg:#0f2747 #bfdbfe",
        "bottom-toolbar.text": "bg:#0f2747 #bfdbfe",
        "completion-menu.completion": "bg:#eff6ff #1e3a5f",
        "completion-menu.completion.current": "bg:#2563eb #ffffff bold",
        "completion-menu.meta.completion": "bg:#eff6ff #64748b",
        "completion-menu.meta.completion.current": "bg:#2563eb #dbeafe",
    }
)


OUTPUT_STYLE = Style.from_dict(
    {
        "output.user": "ansiblue bold",
        "output.agent": "ansiblue bold",
        "output.thinking": "ansibrightblack italic",
        "output.tool": "ansiblue bold",
        "output.success": "ansigreen bold",
        "output.warning": "ansiblue bold",
        "output.error": "ansired bold",
        "output.plan": "ansicyan bold",
        "output.muted": "ansibrightblack",
        "output.body": "",
    }
)
