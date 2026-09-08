"""Semantic light and dark themes for the inline terminal UI."""

from __future__ import annotations

import os
import platform
import subprocess
from collections.abc import Callable, Mapping
from typing import Literal

from prompt_toolkit.styles import Style


ColorScheme = Literal["light", "dark"]
AppearanceReader = Callable[[], str | None]


# 两套模板保持相同的语义键，渲染层只表达含义，不感知具体颜色。
_LIGHT_TUI_RULES = {
    "frame.border": "bg:#eff6ff #60a5fa",
    "input": "bg:#eff6ff #0f172a",
    "prompt": "bg:#eff6ff #2563eb bold",
    "prompt.busy": "bg:#eff6ff #1d4ed8 bold",
    "prompt.approval": "bg:#eff6ff #1d4ed8 bold",
    "prompt.selection": "bg:#eff6ff #0369a1 bold",
    "placeholder": "bg:#eff6ff #64748b italic",
    "toolbar": "noreverse bg:default #64748b",
    "toolbar.model": "noreverse bg:default #1e3a5f bold",
    "toolbar.path": "noreverse bg:default #475569",
    "toolbar.separator": "noreverse bg:default #94a3b8",
    "toolbar.mode": "noreverse bg:default #2563eb bold",
    "toolbar.metric": "noreverse bg:default #64748b",
    "toolbar.progress": "noreverse bg:default #2563eb",
    "toolbar.activity": "noreverse bg:default #1d4ed8 bold",
    "toolbar.selection": "noreverse bg:default #2563eb bold",
    "bottom-toolbar": "noreverse bg:default #64748b",
    "bottom-toolbar.text": "noreverse bg:default #64748b",
    "completion-menu.completion": "bg:#eff6ff #1e3a5f",
    "completion-menu.completion.current": "bg:#dbeafe #1d4ed8 bold",
    "completion-menu.meta.completion": "bg:#eff6ff #64748b",
    "completion-menu.meta.completion.current": "bg:#dbeafe #475569",
}

_DARK_TUI_RULES = {
    "frame.border": "bg:#111827 #60a5fa",
    "input": "bg:#111827 #e5eefc",
    "prompt": "bg:#111827 #60a5fa bold",
    "prompt.busy": "bg:#111827 #93c5fd bold",
    "prompt.approval": "bg:#111827 #93c5fd bold",
    "prompt.selection": "bg:#111827 #67e8f9 bold",
    "placeholder": "bg:#111827 #94a3b8 italic",
    "toolbar": "noreverse bg:default #94a3b8",
    "toolbar.model": "noreverse bg:default #bfdbfe bold",
    "toolbar.path": "noreverse bg:default #94a3b8",
    "toolbar.separator": "noreverse bg:default #64748b",
    "toolbar.mode": "noreverse bg:default #60a5fa bold",
    "toolbar.metric": "noreverse bg:default #94a3b8",
    "toolbar.progress": "noreverse bg:default #60a5fa",
    "toolbar.activity": "noreverse bg:default #93c5fd bold",
    "toolbar.selection": "noreverse bg:default #60a5fa bold",
    "bottom-toolbar": "noreverse bg:default #94a3b8",
    "bottom-toolbar.text": "noreverse bg:default #94a3b8",
    "completion-menu.completion": "bg:#172033 #cbd5e1",
    "completion-menu.completion.current": "bg:#1e3a5f #eff6ff bold",
    "completion-menu.meta.completion": "bg:#172033 #94a3b8",
    "completion-menu.meta.completion.current": "bg:#1e3a5f #bfdbfe",
}

_LIGHT_OUTPUT_RULES = {
    "output.user": "#2563eb bold",
    "output.agent": "#2563eb bold",
    "output.thinking": "#64748b italic",
    "output.tool": "#2563eb bold",
    "output.success": "#15803d bold",
    "output.warning": "#b45309 bold",
    "output.error": "#dc2626 bold",
    "output.plan": "#0891b2 bold",
    "output.muted": "#64748b",
    "output.body": "#202124 nobold noitalic",
    "output.heading": "bold underline",
    "output.heading.marker": "#2563eb bold",
    "output.strong": "bold",
    "output.emphasis": "italic",
    "output.strike": "strike",
    "output.inline-code": "bg:#e2e8f0 #0f172a nobold noitalic",
    "output.link": "#2563eb underline",
    "output.link.url": "#64748b noitalic",
    "output.quote": "#94a3b8",
    "output.quote.text": "italic",
    "output.list.marker": "#2563eb bold",
    "output.rule": "#94a3b8",
    "output.table.border": "#60a5fa",
    "output.table.header": "bold",
    "output.table.cell": "nobold",
    "output.code.border": "#60a5fa",
    "output.code": "#1e3a5f nobold noitalic",
    "startup.border": "#94a3b8",
    "startup.title": "#2563eb bold",
    "startup.body": "#202124",
}

_DARK_OUTPUT_RULES = {
    "output.user": "#60a5fa bold",
    "output.agent": "#60a5fa bold",
    "output.thinking": "#94a3b8 italic",
    "output.tool": "#60a5fa bold",
    "output.success": "#4ade80 bold",
    "output.warning": "#fbbf24 bold",
    "output.error": "#f87171 bold",
    "output.plan": "#22d3ee bold",
    "output.muted": "#94a3b8",
    "output.body": "#e5e7eb nobold noitalic",
    "output.heading": "bold underline",
    "output.heading.marker": "#60a5fa bold",
    "output.strong": "bold",
    "output.emphasis": "italic",
    "output.strike": "strike",
    "output.inline-code": "bg:#1e293b #bfdbfe nobold noitalic",
    "output.link": "#60a5fa underline",
    "output.link.url": "#94a3b8 noitalic",
    "output.quote": "#64748b",
    "output.quote.text": "italic",
    "output.list.marker": "#60a5fa bold",
    "output.rule": "#64748b",
    "output.table.border": "#60a5fa",
    "output.table.header": "bold",
    "output.table.cell": "nobold",
    "output.code.border": "#60a5fa",
    "output.code": "#bfdbfe nobold noitalic",
    "startup.border": "#64748b",
    "startup.title": "#60a5fa bold",
    "startup.body": "#cbd5e1",
}


def detect_color_scheme(
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    macos_appearance_reader: AppearanceReader | None = None,
) -> ColorScheme:
    """Detect the terminal color scheme, with an explicit override for reliability."""
    current_environ = os.environ if environ is None else environ
    override = current_environ.get("INNOAGENT_THEME", "").strip().casefold()
    if override == "dark":
        return "dark"
    if override == "light":
        return "light"

    terminal_background = _colorfgbg_scheme(current_environ.get("COLORFGBG", ""))
    if terminal_background is not None:
        return terminal_background

    current_platform = platform.system() if platform_name is None else platform_name
    if current_platform == "Darwin":  # macos theme
        reader = macos_appearance_reader or _read_macos_appearance
        appearance = reader()
        if appearance and appearance.strip().casefold() == "dark":
            return "dark"
    elif current_platform == "Windows": # windows theme
        import winreg
        try:
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return "light" if value else "dark"
        except (FileNotFoundError, OSError):
            pass
    elif current_platform.lower().startswith("linux"):
        import subprocess
        try:
            result = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                capture_output=True,
                text=True,
                timeout=1,
            )
            value = result.stdout.strip().strip("'")
            if value == "prefer-dark":
                return "dark"
            if value == "prefer-light":
                return "light"
        except (OSError, subprocess.SubprocessError):
            pass
    return "light"


def build_tui_style(scheme: ColorScheme) -> Style:
    """Build the interactive prompt style for one explicit color scheme."""
    rules = _DARK_TUI_RULES if scheme == "dark" else _LIGHT_TUI_RULES
    return Style.from_dict(rules)


def build_output_style(scheme: ColorScheme) -> Style:
    """Build the scrollback/output style for one explicit color scheme."""
    rules = _DARK_OUTPUT_RULES if scheme == "dark" else _LIGHT_OUTPUT_RULES
    return Style.from_dict(rules)


def _colorfgbg_scheme(value: str) -> ColorScheme | None:
    """Infer brightness from COLORFGBG's final ANSI background index."""
    try:
        index = int(value.rsplit(";", 1)[-1])
    except (TypeError, ValueError):
        return None
    if not 0 <= index <= 255:
        return None
    red, green, blue = _ansi_color(index)
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    return "dark" if luminance < 128 else "light"


def _ansi_color(index: int) -> tuple[int, int, int]:
    base_colors = (
        (0, 0, 0),
        (128, 0, 0),
        (0, 128, 0),
        (128, 128, 0),
        (0, 0, 128),
        (128, 0, 128),
        (0, 128, 128),
        (192, 192, 192),
        (128, 128, 128),
        (255, 0, 0),
        (0, 255, 0),
        (255, 255, 0),
        (0, 0, 255),
        (255, 0, 255),
        (0, 255, 255),
        (255, 255, 255),
    )
    if index < 16:
        return base_colors[index]
    if index < 232:
        cube_index = index - 16
        levels = (0, 95, 135, 175, 215, 255)
        return (
            levels[cube_index // 36],
            levels[(cube_index % 36) // 6],
            levels[cube_index % 6],
        )
    level = 8 + (index - 232) * 10
    return level, level, level


def _read_macos_appearance() -> str | None:
    """Read macOS appearance without invoking a shell; a missing key means light mode."""
    try:
        result = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True,
            check=False,
            text=True,
            timeout=0.5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


# 进程启动时选择一次主题，避免运行中切换导致同一 transcript 混用颜色。
ACTIVE_COLOR_SCHEME = detect_color_scheme()
TUI_STYLE = build_tui_style(ACTIVE_COLOR_SCHEME)
OUTPUT_STYLE = build_output_style(ACTIVE_COLOR_SCHEME)
