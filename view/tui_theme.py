"""Semantic colors for the inline terminal UI."""

from prompt_toolkit.styles import Style


# 只给输入区轻量浅色底，输出区仍使用用户终端背景。
TUI_STYLE = Style.from_dict(
    {
        "": "bg:#eeeeee #242424",
        "frame.border": "bg:#eeeeee #c8c8c8",
        "prompt": "bg:#eeeeee #315f86 bold",
        "prompt.busy": "bg:#eeeeee #315f86 bold",
        "prompt.approval": "bg:#eeeeee #9a6700 bold",
        "placeholder": "bg:#eeeeee #8a8a8a italic",
        "toolbar": "bg:#f7f7f7 #777777",
        "toolbar.model": "bg:#f7f7f7 #b06000",
        "toolbar.path": "bg:#f7f7f7 #3b7a3d",
        "toolbar.ready": "bg:#f7f7f7 #2f7d4a bold",
        "toolbar.busy": "bg:#f7f7f7 #315f86 bold",
        "bottom-toolbar": "bg:#f7f7f7 #777777",
        "bottom-toolbar.text": "bg:#f7f7f7 #777777",
        "completion-menu.completion": "bg:#ffffff #333333",
        "completion-menu.completion.current": "bg:#dce8f2 #1f4f73 bold",
        "completion-menu.meta.completion": "bg:#ffffff #777777",
        "completion-menu.meta.completion.current": "bg:#dce8f2 #1f4f73",
    }
)


OUTPUT_STYLE = Style.from_dict(
    {
        "output.user": "bold",
        "output.agent": "",
        "output.thinking": "ansibrightblack italic",
        "output.tool": "ansiblue bold",
        "output.success": "ansigreen bold",
        "output.warning": "ansiyellow bold",
        "output.error": "ansired bold",
        "output.plan": "ansicyan bold",
        "output.muted": "ansibrightblack",
        "output.body": "",
    }
)
