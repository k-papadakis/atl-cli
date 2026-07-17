"""The output boundary: the one module that knows about Rich.

The tool keeps a strict split: the *payload* (Markdown, tables, JSON) goes to
stdout so it stays pipeable, and *chrome* (summaries, status, errors) goes to
stderr. Each stream gets its own console, and every color/styling decision --
theme, styles, tty-gating, the markup/wrap flags -- lives here behind a small
semantic API, so the rest of the codebase imports no Rich.

**Invariant:** text payloads (code, JSON, Markdown, plain text) fall back to a
bare ``print`` when the stream is not a terminal, so piped output is
byte-identical to ``print``; a tty only layers color on top. The search table is
the one payload Rich lays out itself -- borderless plain text with every cell
complete when piped, and always rendered in full (never truncated to the
terminal; a tty soft-wraps overlong rows) plus color on a tty. Rich honors
``NO_COLOR``, ``FORCE_COLOR`` and tty detection automatically.
"""

import json

from pydantic import JsonValue
from rich.console import Console
from rich.prompt import Prompt
from rich.status import Status
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from atl_cli.rendering import SearchTable

_out = Console()  # payload -> stdout
_err = Console(stderr=True)  # chrome + errors -> stderr

# An ANSI (16-color) theme adapts to the user's own terminal palette instead of
# hard-coding a scheme. It must be a *transparent-background* theme: emit_code
# leans on that to keep long lines intact. With a transparent theme, Rich renders
# the Syntax through the code path that respects soft_wrap's "don't crop" flag; an
# opaque theme takes a different path that always crops each line to the terminal
# width, so long code/Markdown lines would be silently truncated.
_SYNTAX_THEME = "ansi_dark"
_ID_STYLE = "green"  # color for the search table's id column

# A stand-in for "unlimited width": Rich has no true infinite width, so a large
# value is the idiom. emit_table renders at this width so the table is never
# fitted (and truncated) to the terminal -- every column survives in full.
_UNBOUNDED_WIDTH = 10_000


# --------------------------------------------------------------------------- #
# Payload (stdout): coloring only -- no borders, no reflow. Piped output stays
# byte-identical to a plain `print`; a tty adds color.
# --------------------------------------------------------------------------- #
def emit_code(text: str, language: str) -> None:
    """Emit a payload string: syntax-highlighted on a tty, plain `print` when piped."""
    if not _out.is_terminal:
        print(text)
        return
    # soft_wrap keeps long lines intact (no crop to the terminal width); the
    # terminal soft-wraps them visually. Relies on _SYNTAX_THEME being transparent.
    _out.print(
        Syntax(text, language, word_wrap=False, theme=_SYNTAX_THEME), soft_wrap=True
    )


def emit_text(text: str) -> None:
    """Emit a verbatim payload line to stdout -- a bare ``print``, identical piped or not.

    Unlike the ``emit_*`` above, there is no tty branch: this is output that must
    pass through untouched on either stream (a non-JSON ``atl api`` body, the
    ``--version`` line). It exists only so no module outside console.py calls
    ``print`` for a payload -- the stdout boundary stays greppable in one place.
    """
    print(text)


def emit_json(data: JsonValue) -> None:
    """Pretty-print a JSON payload to stdout (colorized on a tty)."""
    emit_code(json.dumps(data, indent=2), "json")


def emit_markdown(md: str) -> None:
    """Print a rendered Markdown document to stdout.

    The document is emitted verbatim -- headings, ``**bold**``, list markers and
    line breaks all stay literal -- and a tty only layers syntax color on top.
    """
    emit_code(md, "markdown")


def emit_table(st: SearchTable) -> None:
    """Print a search-results table to stdout, laid out by Rich.

    A borderless table with color on the header row and the id column on a tty.
    It is always rendered in full -- no column is ever truncated or dropped -- so
    the id (the value you copy into the next command) stays intact and piped
    output carries every cell whole. A tty soft-wraps any row wider than the
    window and adds color; nothing else changes between the two.
    """
    # box=None + pad_edge=False make it borderless.
    table = Table(box=None, pad_edge=False)
    for i, header in enumerate(st.headers):
        # no_wrap so no cell is folded to fit; paired with the unbounded-width
        # console below, every column renders at its full content width.
        table.add_column(
            header, style=_ID_STYLE if i == st.id_col else "", no_wrap=True
        )
    for row in st.rows:
        # Text() cells keep '[...]' literal; a bare str would be parsed as markup.
        table.add_row(*(Text(cell) for cell in row))
    # A fresh console at the sentinel width renders the table in full instead of
    # fitting (and truncating) it to the terminal; it still detects the tty for
    # color. The terminal itself soft-wraps rows too wide for the window.
    Console(width=_UNBOUNDED_WIDTH).print(table)


# --------------------------------------------------------------------------- #
# Chrome (stderr, except status lines): styled human-facing messages. The
# markup/highlight/wrap flags are baked in once here.
#   - markup=False: these messages interpolate dynamic/external data (keys, URLs,
#     ids, error text, filenames) that can contain '[...]'; with markup on, Rich
#     would parse those as tags (dropped, or a MarkupError). Nothing in this module
#     relies on markup parsing -- the one styled static string (the prompt hint)
#     is built as a `Text`, so there is no injection surface to guard.
#   - highlight=False leaves numbers/paths/urls uncolored.
#   - soft_wrap=True keeps long lines on one line instead of wrapping at width 80.
# --------------------------------------------------------------------------- #
def _chrome(console: Console, msg: str, style: str) -> None:
    console.print(msg, style=style, markup=False, highlight=False, soft_wrap=True)


def success(msg: str) -> None:
    _chrome(_err, msg, "green")


def warn(msg: str) -> None:
    _chrome(_err, msg, "yellow")


def note(msg: str) -> None:
    _chrome(_err, msg, "dim")


def error(msg: str) -> None:
    _chrome(_err, msg, "red")


def status_line(msg: str, *, style: str = "") -> None:
    """A single ``auth status`` line.

    Unlike the other chrome, these go to **stdout** to preserve the documented
    ``auth status`` exit-code contract (the command reports its verdict on stdout
    and exits non-zero); the color is the only tty addition.
    """
    _chrome(_out, msg, style)


# --------------------------------------------------------------------------- #
# Interactive input: the question is styled on stderr, the answer read from stdin,
# so the payload on stdout stays clean.
# --------------------------------------------------------------------------- #
def _question(label: str, hint: str | None) -> Text:
    """Build the prompt question as a ``Text``: the label verbatim, plus an
    optional dimmed ``(hint)``.

    Built explicitly rather than via markup so a literal '[...]' in either part
    stays literal -- there is no markup-injection surface to escape.
    """
    question = Text(label)
    if hint is not None:
        _ = question.append(f" ({hint})", style="dim")
    return question


def prompt(label: str, *, hint: str | None = None) -> str:
    """Ask for a line of input, with the question styled on stderr.

    ``label`` is shown verbatim; an optional ``hint`` is appended in parentheses
    and dimmed (e.g. an example value).
    """
    return Prompt.ask(_question(label, hint), console=_err)


def prompt_secret(label: str, *, hint: str | None = None) -> str:
    """Ask for a secret without echoing it, with the question styled on stderr
    (label/hint as in ``prompt``)."""
    return Prompt.ask(_question(label, hint), console=_err, password=True)


def spinner(label: str) -> Status:
    """Show a spinner on a tty while a block runs; renders nothing when piped.

    Returns Rich's own ``Status`` context manager -- use it as
    ``with spinner(...):``. The label is dimmed so the transient status recedes;
    the spinner glyph keeps its default accent.
    """
    return _err.status(Text(label, style="dim"))
