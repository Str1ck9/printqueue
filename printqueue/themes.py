"""Custom Textual themes for the PrintQueue TUI.

``c64`` and ``wildcat`` are registered as real Textual themes, so they appear
in the command palette (Ctrl+P → "Change theme") alongside the built-ins and
get live preview when highlighted. Each carries the extra ``pq-*`` CSS
variables consumed by ``PrintQueueApp.CSS``; built-in themes fall back to the
stock values in ``PQ_VARIABLE_DEFAULTS`` (via ``get_theme_variable_defaults``),
which reproduce the original hardcoded look exactly.

Rich markup styles used inside panel renderables can't come from CSS
variables, so each retro theme also has a ``Markup`` entry here.

Imports of ``textual`` are kept inside functions so the CLI can import this
module for validation without dragging in the TUI stack.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Themes that get the full retro treatment: .pq-themed CSS overrides
#: (inverse-video bars, exact table colors) and emoji-free ASCII labels.
RETRO_THEMES = ("c64", "wildcat")


# -- Rich markup styles (panel text) ----------------------------------------
@dataclass(frozen=True)
class Markup:
    title_style: str          # panel titles
    dim_style: str            # secondary text
    ok_style: str             # ONLINE
    warn_style: str           # warnings / NOT CONFIGURED
    err_style: str            # OFFLINE / errors
    emph_style: str           # emphasized values
    ascii_labels: bool = False  # emoji-free tables, 8-bit style

    def title(self, text: str) -> str:
        return f"[{self.title_style}]{text}[/]"

    def dim(self, text: str) -> str:
        return f"[{self.dim_style}]{text}[/]"


DEFAULT_MARKUP = Markup(
    title_style="bold cyan",
    dim_style="dim",
    ok_style="bold green",
    warn_style="bold yellow",
    err_style="bold red",
    emph_style="bold",
)

_MARKUP = {
    "c64": Markup(
        title_style="bold #FFFFFF",
        dim_style="#6A5FC0",
        ok_style="bold #9AD284",   # C64 light green
        warn_style="bold #BFCE72", # C64 yellow
        err_style="bold #C46C71",  # C64 light red
        emph_style="bold #FFFFFF",
        ascii_labels=True,
    ),
    "wildcat": Markup(
        title_style="bold #FFFF55",
        dim_style="#555555",
        ok_style="bold #55FF55",
        warn_style="bold #FFFF55",
        err_style="bold #FF5555",
        emph_style="bold #FFFFFF",
        ascii_labels=True,
    ),
}


def get_markup(theme_name: str | None) -> Markup:
    """Markup styles for a theme — built-ins get the stock styles."""
    return _MARKUP.get(theme_name or "", DEFAULT_MARKUP)


# -- pq-* CSS variables ------------------------------------------------------
# Defaults used under any theme that doesn't override them (all built-ins).
# The border values reproduce the original hardcoded look; the rest are only
# reachable under .pq-themed, which retro themes always override, but they
# must be valid colors so the stylesheet parses.
PQ_VARIABLE_DEFAULTS: dict[str, str] = {
    "pq-border-type": "round",
    "pq-border-printer": "cyan",
    "pq-border-camera": "blue",
    "pq-border-queue": "green",
    "pq-border-filament": "yellow",
    "pq-border-history": "magenta",
    "pq-bg": "black",
    "pq-fg": "white",
    "pq-accent": "white",
    "pq-header-bg": "black",
    "pq-header-fg": "white",
    "pq-footer-bg": "black",
    "pq-footer-fg": "white",
    "pq-table-header-bg": "black",
    "pq-table-header-fg": "white",
    "pq-cursor-bg": "white",
    "pq-cursor-fg": "black",
    "pq-row-odd-bg": "black",
    "pq-row-even-bg": "black",
}

# Commodore 64 boot screen: light blue text inside a fat light-blue border
# on that unmistakable blue. Colors from the Pepto C64 palette.
_C64_VARS = {
    "pq-border-type": "block",
    "pq-border-printer": "#7869C4",
    "pq-border-camera": "#7869C4",
    "pq-border-queue": "#7869C4",
    "pq-border-filament": "#7869C4",
    "pq-border-history": "#7869C4",
    "pq-bg": "#40318D",
    "pq-fg": "#7869C4",
    "pq-accent": "#FFFFFF",
    "pq-header-bg": "#7869C4",      # inverse video, like pressing CTRL+9
    "pq-header-fg": "#40318D",
    "pq-footer-bg": "#40318D",
    "pq-footer-fg": "#7869C4",
    "pq-table-header-bg": "#7869C4",
    "pq-table-header-fg": "#40318D",
    "pq-cursor-bg": "#7869C4",
    "pq-cursor-fg": "#40318D",
    "pq-row-odd-bg": "#40318D",
    "pq-row-even-bg": "#453697",
}

# Wildcat! BBS: DOS-era ANSI art — double-line boxes in hot colors on black,
# bright-white-on-blue bars, yellow highlights. Baud not included.
_WILDCAT_VARS = {
    "pq-border-type": "double",
    "pq-border-printer": "#FF5555",   # bright red
    "pq-border-camera": "#FF55FF",    # bright magenta
    "pq-border-queue": "#55FFFF",     # bright cyan
    "pq-border-filament": "#FFFF55",  # bright yellow
    "pq-border-history": "#55FF55",   # bright green
    "pq-bg": "#000000",
    "pq-fg": "#AAAAAA",
    "pq-accent": "#FFFF55",
    "pq-header-bg": "#0000AA",        # classic DOS blue bar
    "pq-header-fg": "#FFFFFF",
    "pq-footer-bg": "#0000AA",
    "pq-footer-fg": "#AAAAAA",
    "pq-table-header-bg": "#0000AA",
    "pq-table-header-fg": "#FFFFFF",
    "pq-cursor-bg": "#AA0000",
    "pq-cursor-fg": "#FFFF55",
    "pq-row-odd-bg": "#000000",
    "pq-row-even-bg": "#101010",
}


def custom_themes() -> list:
    """The PrintQueue custom themes, as textual.theme.Theme objects."""
    from textual.theme import Theme

    return [
        Theme(
            name="c64",
            primary="#7869C4",
            secondary="#6A5FC0",
            background="#40318D",
            surface="#40318D",
            panel="#453697",
            foreground="#7869C4",
            success="#9AD284",
            warning="#BFCE72",
            error="#C46C71",
            accent="#FFFFFF",
            dark=True,
            variables=_C64_VARS,
        ),
        Theme(
            name="wildcat",
            primary="#FFFF55",
            secondary="#55FFFF",
            background="#000000",
            surface="#0A0A0A",
            panel="#101010",
            foreground="#AAAAAA",
            success="#55FF55",
            warning="#FFFF55",
            error="#FF5555",
            accent="#FF55FF",
            dark=True,
            variables=_WILDCAT_VARS,
        ),
    ]


def known_theme_names() -> list[str]:
    """All valid theme names: Textual built-ins plus the PrintQueue ones."""
    from textual.theme import BUILTIN_THEMES

    return sorted(set(BUILTIN_THEMES) | set(RETRO_THEMES))
