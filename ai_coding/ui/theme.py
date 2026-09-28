"""Dark theme colors inspired by Claude Code."""

from prompt_toolkit.styles import Style

STYLE = Style.from_dict({
    "header": "#00d2d3",
    "header.border": "#00d2d3",
    "header.text": "#dfe4ea",
    "header.label": "#2ed573",
    "header.model": "#ffa502",
    "header.cwd": "#a4b0be",
    "body": "#dfe4ea",
    "footer": "#2f3542",
    "footer.text": "#dfe4ea",
    "palette": "#1e272e",
    "palette.border": "#00d2d3",
    "palette.selected": "#00d2d3",
    "palette.selected.text": "#1e272e",
    "palette.item": "#dfe4ea",
    "palette.hint": "#a4b0be",
    "input": "#dfe4ea",
    "input.prefix": "#00d2d3",
})
