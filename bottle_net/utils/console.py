"""Terminal output helpers: banner, headers, status lines and summaries.

All of this is human-readable, so it is written to **stderr**; stdout is
reserved for machine-readable data (see :mod:`bottle_net.utils.output`).

Status indicators follow a consistent scheme:

* ``[+]`` progress/information   * ``[✓]`` success
* ``[!]`` warning or failure     * ``[i]`` note
"""

from __future__ import annotations

from collections.abc import Sequence

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from bottle_net import APP_NAME, __author__
from bottle_net.errors import BottleNetError

BANNER_WIDTH = 50
INDENT = "    "


class UI:
    """Human-readable CLI output.

    ``console`` carries status and progress (silenced by ``quiet``);
    ``err`` carries errors, which are always shown. Both write to stderr
    unless other consoles are supplied.
    """

    def __init__(
        self,
        console: Console | None = None,
        *,
        err_console: Console | None = None,
        quiet: bool = False,
        no_color: bool = False,
    ) -> None:
        self.quiet = quiet
        self.console = console or Console(
            stderr=True, highlight=False, soft_wrap=True, quiet=quiet, no_color=no_color
        )
        self.err = err_console or Console(stderr=True, highlight=False, soft_wrap=True, no_color=no_color)

    # ------------------------------------------------------------------ framing

    def _box(self, lines: Sequence[Text]) -> None:
        body = Group(*(Align.center(line) for line in lines))
        self.console.print(Panel(body, box=box.DOUBLE, width=BANNER_WIDTH, border_style="cyan", safe_box=False))

    def banner(self) -> None:
        """Print the startup banner."""
        self._box([
            Text(APP_NAME.upper(), style="bold cyan"),
            Text("Public Video Crawler & Downloader"),
            Text(""),
            Text(f"Author: {__author__}", style="dim"),
        ])

    def header(self, subtitle: str) -> None:
        """Print a command header box, e.g. ``TikTok Crawler``."""
        self._box([Text(APP_NAME.upper(), style="bold cyan"), Text(subtitle)])
        self.console.print()

    def rule(self, width: int = 32) -> None:
        """Print a horizontal separator."""
        self.console.print("─" * width, style="dim")

    def blank(self) -> None:
        """Print an empty line."""
        self.console.print()

    # ------------------------------------------------------------------- status

    def info(self, message: str) -> None:
        """Print a ``[+]`` progress line."""
        self.console.print(f"[cyan]\\[+][/cyan] {escape(message)}")

    def success(self, message: str) -> None:
        """Print a ``[✓]`` success line."""
        self.console.print(f"[bold green]\\[✓][/bold green] {escape(message)}")

    def note(self, message: str) -> None:
        """Print an ``[i]`` note."""
        self.console.print(f"[dim]\\[i] {escape(message)}[/dim]")

    def warning(self, message: str) -> None:
        """Print a ``[!]`` warning."""
        self.console.print(f"[yellow]\\[!][/yellow] {escape(message)}")

    def notice(self, title: str, lines: Sequence[str]) -> None:
        """Print a ``[!] Note:`` block with unindented explanatory lines."""
        self.console.print(f"[yellow]\\[!][/yellow] [bold]{escape(title)}[/bold]")
        for line in lines:
            self.console.print(escape(line))

    def error(self, message: str, *, reason: str | None = None, hint: str | None = None) -> None:
        """Print a ``[!]`` error to stderr with an optional reason and hint."""
        self.err.print(f"[bold red]\\[!][/bold red] {escape(message)}")
        if reason:
            self.err.print(f"{INDENT}{escape(reason)}")
        if hint:
            for line in hint.splitlines():
                self.err.print(f"{INDENT}{escape(line)}" if line.strip() else "")

    def failure(self, error: BottleNetError) -> None:
        """Print a tool error as headline, explanation and next step.

        ``[!] Video unavailable.`` / explanation / hint (e.g. a command to run).
        """
        headline = error.label.rstrip(".") + "."
        message = error.message
        if message.rstrip(".").lower() == error.label.rstrip(".").lower():
            message = ""
        self.error(headline, reason=message or None, hint=error.hint)

    def field(self, label: str, value: str, *, width: int = 0) -> None:
        """Print a ``Label: value`` line (labels padded to *width*)."""
        name = f"{label}:"
        self.console.print(f"[bold]{escape(name)}[/bold]{' ' * max(1, width - len(name) + 1)}{escape(value)}")

    def saved(self, label: str, path: str) -> None:
        """Print a label followed by an indented path."""
        self.console.print()
        self.console.print(f"[bold]{escape(label)}[/bold]")
        self.console.print(f"  {escape(path)}")

    def counts(self, rows: Sequence[tuple[str, int, str]]) -> None:
        """Print aligned counters, e.g. ``Successful: 94`` / ``Failed:      6``.

        Each row is ``(label, count, style)``.
        """
        width = max(len(label) for label, _, _ in rows) + 1
        for label, count, style in rows:
            self.console.print(f"{label + ':':<{width}} [{style}]{count:>4}[/{style}]")
