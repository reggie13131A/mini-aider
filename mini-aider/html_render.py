from pathlib import Path
import webbrowser
import html
from typing import List
from ioio import InputOutput as IO

def render_unified_diff_html(diff_text: str) -> str:
    """
    将 unified diff 文本渲染成带颜色的 HTML。
    """
    rendered_lines: List[str] = []
    for line in diff_text.splitlines():
        escaped = html.escape(line)
        if line.startswith("--- ") or line.startswith("+++ "):
            rendered_lines.append(f'<span class="diff-file">{escaped}</span>')
        elif line.startswith("@@"):
            rendered_lines.append(f'<span class="diff-hunk">{escaped}</span>')
        elif line.startswith("+") and not line.startswith("+++ "):
            rendered_lines.append(f'<span class="diff-add">{escaped}</span>')
        elif line.startswith("-") and not line.startswith("--- "):
            rendered_lines.append(f'<span class="diff-del">{escaped}</span>')
        else:
            rendered_lines.append(f'<span class="diff-ctx">{escaped}</span>')
    return "\n".join(rendered_lines)

def build_diff_page(body_html: str, changed_count: int, total_count: int) -> str:
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8" />
    <title>Mini-Codex Diff Review</title>
    <style>
        body {{
            font-family: Consolas, "Courier New", monospace;
            margin: 24px;
            background: #0f1115;
            color: #e6edf3;
        }}

        h1 {{
            margin-bottom: 8px;
            font-size: 28px;
        }}

        .summary {{
            color: #9da7b3;
            margin-bottom: 24px;
            font-size: 14px;
        }}

        .file-block {{
            margin-bottom: 28px;
            border: 1px solid #30363d;
            border-radius: 10px;
            overflow: hidden;
            background: #161b22;
        }}

        .file-block h2 {{
            margin: 0;
            padding: 14px 16px;
            font-size: 18px;
            border-bottom: 1px solid #30363d;
            background: #1c2128;
        }}

        .meta {{
            padding: 14px 16px;
            color: #9da7b3;
            font-size: 14px;
        }}

        .diff-block {{
            margin: 0;
            padding: 16px;
            overflow-x: auto;
            white-space: pre-wrap;
            line-height: 1.5;
            font-size: 13px;
        }}

        .diff-file {{
            color: #7ee787;
            font-weight: bold;
        }}

        .diff-hunk {{
            color: #79c0ff;
            font-weight: bold;
        }}

        .diff-add {{
            display: block;
            background: rgba(46, 160, 67, 0.18);
            color: #aff5b4;
        }}

        .diff-del {{
            display: block;
            background: rgba(248, 81, 73, 0.18);
            color: #ffd8d3;
        }}

        .diff-ctx {{
            display: block;
            color: #c9d1d9;
        }}
    </style>
</head>
<body>
    <h1>Diff Review</h1>
    <div class="summary">
        共 {total_count} 个文件，{changed_count} 个文件存在实际改动。<br/>
        只展示 patch 风格的变更片段，方便用户确认是否接受修改。
    </div>
    {body_html}
</body>
</html>
"""

def try_open_in_browser(html_path: Path) -> bool:
    try:
        return webbrowser.open(html_path.resolve().as_uri())
    except Exception as e:
        IO.tool_output(f"打开浏览器失败：{e}")
        return False