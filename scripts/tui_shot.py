"""Render the TUI to SVGs for eyeballing against the reference.

Run: .venv/bin/python scripts/tui_shot.py
Output: docs/tui-preview.svg (conversation) + docs/tui-palette.svg (palette)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ai_coding.ui.textual_app import AICodingApp

DOCS = Path(__file__).resolve().parent.parent / "docs"
OUT = DOCS / "tui-preview.svg"
OUT_PALETTE = DOCS / "tui-palette.svg"


async def main() -> None:
    app = AICodingApp(
        loop=None,
        session=object(),
        sessions=object(),
        workspace="~/projects/ai-app",
        model_name="DeepSeek-V3",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        transcript = app.query_one("#transcript")
        transcript.append_user("请帮我分析一下这个项目的代码结构，并给出优化建议。", "10:24")
        transcript.begin_assistant("10:25")
        transcript.stream(
            "好的，我将先分析当前项目的代码结构，然后从可维护性、性能、"
            "可扩展性等方面给出优化建议。\n\n## 项目结构概览\n\n"
            "- backend/  # 后端服务\n- frontend/  # 前端页面\n"
        )
        await transcript.end_assistant()
        transcript.append_block(_block("tool-end", "✓ 已完成代码分析"))
        await pilot.pause()
        app.save_screenshot(str(OUT))

        # The palette (reference panel 2): type "/" to open, then filter.
        app.query_one("#prompt").value = "/"
        await pilot.pause()
        await pilot.press("p", "l", "a", "n")
        await pilot.pause()
        app.save_screenshot(str(OUT_PALETTE))


def _block(kind: str, text: str):
    class _B:
        def __init__(self) -> None:
            self.kind, self.text = kind, text

        def render(self):
            from rich.text import Text
            return Text(self.text)

    return _B()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"wrote {OUT}")
