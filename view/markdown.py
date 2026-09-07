"""Small terminal-oriented Markdown renderer for model output."""

from __future__ import annotations

import re

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.utils import get_cwidth


_INLINE_PATTERN = re.compile(
    r"(?<!\\)(`[^`\n]+`|\*\*[^*\n]+?\*\*|__[^_\n]+?__|~~[^~\n]+?~~|"
    r"\[[^\]\n]+\]\([^\)\n]+\)|(?<!\*)\*[^*\n]+?\*(?!\*)|"
    r"(?<![\w\\])_[^_\n]+?_(?!\w))"
)
_LINK_PATTERN = re.compile(r"^\[([^\]]+)\]\(([^)]+)\)$")
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_TASK_PATTERN = re.compile(r"^\s*[-+*]\s+\[([ xX])\]\s+(.+)$")
_UNORDERED_PATTERN = re.compile(r"^(\s*)[-+*]\s+(.+)$")
_ORDERED_PATTERN = re.compile(r"^(\s*)\d+[.)]\s+(.+)$")
_RULE_PATTERN = re.compile(r"^\s{0,3}(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$")
_TABLE_SEPARATOR_PATTERN = re.compile(r"^:?-{3,}:?$")

MAX_TABLE_CONTENT_WIDTH = 88
MAX_TABLE_CELL_WIDTH = 32


def render_markdown(body: str, *, base_style: str) -> StyleAndTextTuples:
    """Render a Markdown document into prompt-toolkit styled fragments."""
    lines = body.splitlines()
    rendered: list[StyleAndTextTuples] = []
    code_fence: str | None = None
    index = 0

    while index < len(lines):
        line = lines[index]
        fence = _fence(line)
        if code_fence is not None:
            if fence and fence[0] == code_fence:
                rendered.append([("class:output.code.border", "  ╰─")])
                code_fence = None
            else:
                rendered.append(
                    [
                        ("class:output.code.border", "  │ "),
                        ("class:output.code", line),
                    ]
                )
            index += 1
            continue

        if fence:
            code_fence, language = fence
            rendered.append(
                [("class:output.code.border", f"  ╭─ {language or 'code'}")]
            )
            index += 1
            continue

        header = split_table_row(line)
        separator = split_table_row(lines[index + 1]) if index + 1 < len(lines) else None
        if header and separator and _is_table_separator(separator):
            rows: list[list[str]] = []
            next_index = index + 2
            while next_index < len(lines):
                row = split_table_row(lines[next_index])
                if row is None:
                    break
                rows.append(row)
                next_index += 1
            rendered.extend(_render_table(header, separator, rows, base_style))
            index = next_index
            continue

        rendered.append(render_markdown_line(line, base_style=base_style))
        index += 1

    if code_fence is not None:
        rendered.append([("class:output.code.border", "  ╰─")])
    return _join_lines(rendered)


def render_markdown_line(line: str, *, base_style: str) -> StyleAndTextTuples:
    """Render one non-fenced Markdown line."""
    if not line:
        return []
    stripped = line.strip()
    heading = _HEADING_PATTERN.match(stripped)
    if heading:
        return [
            ("class:output.heading.marker", "  ▸ "),
            *_render_inline(heading.group(2), _styled(base_style, "output.heading")),
        ]
    if _RULE_PATTERN.match(line):
        return [("class:output.rule", "  " + "─" * 40)]
    task = _TASK_PATTERN.match(line)
    if task:
        checked = task.group(1).casefold() == "x"
        marker_style = "output.success" if checked else "output.list.marker"
        marker = "✓" if checked else "○"
        return [
            (f"class:{marker_style}", f"  {marker} "),
            *_render_inline(task.group(2), base_style),
        ]
    quote = stripped.removeprefix(">").lstrip() if stripped.startswith(">") else None
    if quote is not None:
        return [
            ("class:output.quote", "  │ "),
            *_render_inline(quote, _styled(base_style, "output.quote.text")),
        ]
    unordered = _UNORDERED_PATTERN.match(line)
    if unordered:
        indent = " " * min(6, len(unordered.group(1)))
        return [
            ("class:output.list.marker", f"  {indent}• "),
            *_render_inline(unordered.group(2), base_style),
        ]
    ordered = _ORDERED_PATTERN.match(line)
    if ordered:
        indent = " " * min(6, len(ordered.group(1)))
        marker = stripped.split(maxsplit=1)[0]
        return [
            ("class:output.list.marker", f"  {indent}{marker} "),
            *_render_inline(ordered.group(2), base_style),
        ]
    leading = len(line) - len(line.lstrip())
    return [
        (base_style, "  " + " " * min(6, leading)),
        *_render_inline(line.lstrip(), base_style),
    ]


def is_table_row(line: str) -> bool:
    """Return whether a line can belong to a Markdown table."""
    return split_table_row(line) is not None


def split_table_row(line: str) -> list[str] | None:
    """Split a pipe row while preserving escaped pipes and inline code spans."""
    stripped = line.strip()
    if "|" not in stripped:
        return None
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    code_open = False
    for char in stripped:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            current.append(char)
            continue
        if char == "`":
            code_open = not code_open
            current.append(char)
            continue
        if char == "|" and not code_open:
            cells.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    cells.append("".join(current).strip())
    if stripped.startswith("|"):
        cells = cells[1:]
    if stripped.endswith("|"):
        cells = cells[:-1]
    return cells if len(cells) >= 2 else None


def _render_inline(text: str, base_style: str) -> StyleAndTextTuples:
    fragments: StyleAndTextTuples = []
    position = 0
    for match in _INLINE_PATTERN.finditer(text):
        if match.start() > position:
            fragments.append((base_style, _unescape(text[position : match.start()])))
        token = match.group(0)
        if token.startswith("`"):
            fragments.append(
                (_styled(base_style, "output.inline-code"), token[1:-1])
            )
        elif token.startswith(("**", "__")):
            fragments.extend(
                _render_inline(token[2:-2], _styled(base_style, "output.strong"))
            )
        elif token.startswith("~~"):
            fragments.extend(
                _render_inline(token[2:-2], _styled(base_style, "output.strike"))
            )
        elif token.startswith("["):
            link = _LINK_PATTERN.match(token)
            if link:
                fragments.extend(
                    _render_inline(link.group(1), _styled(base_style, "output.link"))
                )
                fragments.append(
                    (_styled(base_style, "output.link.url"), f" ({link.group(2)})")
                )
        else:
            fragments.extend(
                _render_inline(token[1:-1], _styled(base_style, "output.emphasis"))
            )
        position = match.end()
    if position < len(text):
        fragments.append((base_style, _unescape(text[position:])))
    return fragments


def _render_table(
    header: list[str],
    separator: list[str],
    rows: list[list[str]],
    base_style: str,
) -> list[StyleAndTextTuples]:
    column_count = max(len(header), *(len(row) for row in rows), len(separator))
    normalized_header = _normalize_row(header, column_count)
    normalized_rows = [_normalize_row(row, column_count) for row in rows]
    alignments = _table_alignments(separator, column_count)
    cell_limit = max(8, min(MAX_TABLE_CELL_WIDTH, MAX_TABLE_CONTENT_WIDTH // column_count))
    widths = [
        min(
            cell_limit,
            max(
                3,
                *(
                    _inline_width(row[column])
                    for row in [normalized_header, *normalized_rows]
                ),
            ),
        )
        for column in range(column_count)
    ]
    border_style = "class:output.table.border"
    result: list[StyleAndTextTuples] = [
        [(border_style, "  ┌" + "┬".join("─" * (width + 2) for width in widths) + "┐")]
    ]
    result.append(
        _render_table_row(
            normalized_header,
            widths,
            alignments,
            _styled(base_style, "output.table.header"),
        )
    )
    result.append(
        [(border_style, "  ├" + "┼".join("─" * (width + 2) for width in widths) + "┤")]
    )
    for row in normalized_rows:
        result.append(
            _render_table_row(
                row,
                widths,
                alignments,
                _styled(base_style, "output.table.cell"),
            )
        )
    result.append(
        [(border_style, "  └" + "┴".join("─" * (width + 2) for width in widths) + "┘")]
    )
    return result


def _render_table_row(
    row: list[str],
    widths: list[int],
    alignments: list[str],
    style: str,
) -> StyleAndTextTuples:
    fragments: StyleAndTextTuples = [("class:output.table.border", "  │ ")]
    for index, (cell, width) in enumerate(zip(row, widths, strict=True)):
        if index:
            fragments.append(("class:output.table.border", " │ "))
        cell_fragments = _clip_fragments(_render_inline(cell.strip(), style), width)
        visible = _fragments_width(cell_fragments)
        padding = max(0, width - visible)
        if alignments[index] == "right":
            fragments.append((style, " " * padding))
            fragments.extend(cell_fragments)
        elif alignments[index] == "center":
            left = padding // 2
            fragments.append((style, " " * left))
            fragments.extend(cell_fragments)
            fragments.append((style, " " * (padding - left)))
        else:
            fragments.extend(cell_fragments)
            fragments.append((style, " " * padding))
    fragments.append(("class:output.table.border", " │"))
    return fragments


def _clip_fragments(fragments: StyleAndTextTuples, width: int) -> StyleAndTextTuples:
    if _fragments_width(fragments) <= width:
        return fragments
    result: StyleAndTextTuples = []
    remaining = max(0, width - 1)
    truncated = False
    for style, text in fragments:
        kept: list[str] = []
        for char in text:
            char_width = get_cwidth(char)
            if char_width > remaining:
                truncated = True
                break
            kept.append(char)
            remaining -= char_width
        if kept:
            result.append((style, "".join(kept)))
        if truncated or remaining <= 0:
            break
    result.append((fragments[-1][0] if fragments else "", "…"))
    return result


def _inline_width(text: str) -> int:
    return _fragments_width(_render_inline(text.strip(), ""))


def _fragments_width(fragments: StyleAndTextTuples) -> int:
    return sum(get_cwidth(text) for _, text in fragments)


def _normalize_row(row: list[str], size: int) -> list[str]:
    return [*row[:size], *("" for _ in range(max(0, size - len(row))))]


def _table_alignments(separator: list[str], size: int) -> list[str]:
    result: list[str] = []
    for cell in _normalize_row(separator, size):
        stripped = cell.strip()
        if stripped.startswith(":") and stripped.endswith(":"):
            result.append("center")
        elif stripped.endswith(":"):
            result.append("right")
        else:
            result.append("left")
    return result


def _is_table_separator(cells: list[str]) -> bool:
    return bool(cells) and all(_TABLE_SEPARATOR_PATTERN.match(cell.strip()) for cell in cells)


def _fence(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    for marker in ("```", "~~~"):
        if stripped.startswith(marker):
            suffix = stripped[len(marker) :].strip()
            return marker, suffix.split(maxsplit=1)[0] if suffix else ""
    return None


def _styled(base_style: str, modifier: str) -> str:
    return " ".join(filter(None, [base_style, f"class:{modifier}"]))


def _unescape(text: str) -> str:
    return re.sub(r"\\([\\`*_[\]{}()#+.!|~-])", r"\1", text)


def _join_lines(lines: list[StyleAndTextTuples]) -> StyleAndTextTuples:
    fragments: StyleAndTextTuples = []
    for index, line in enumerate(lines):
        if index:
            fragments.append(("", "\n"))
        fragments.extend(line)
    return fragments
