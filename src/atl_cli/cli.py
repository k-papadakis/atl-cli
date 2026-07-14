"""The Typer command-line interface: thin adapters over the command layer."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from atl_cli import __version__
from atl_cli.account import load_credentials, remove_credentials
from atl_cli.client import AtlassianClient
from atl_cli.commands import (
    OutputFormat,
    cmd_api,
    cmd_confluence_attachment,
    cmd_confluence_search,
    cmd_confluence_view,
    cmd_jira_attachment,
    cmd_jira_search,
    cmd_jira_view,
    cmd_login,
    cmd_status,
)
from atl_cli.errors import AtlError

# Rich stderr console, as Typer recommends for output/errors; it honors
# NO_COLOR, FORCE_COLOR and tty detection automatically.
err_console = Console(stderr=True)

# `-h` alongside `--help` everywhere: Typer has no dedicated knob, but Click's
# `help_option_names` set on the root context is inherited by every subcommand,
# so declaring it once here covers `atl`, `atl jira view`, ... alike.
app = typer.Typer(
    help="Render Atlassian (Jira + Confluence) content as clean Markdown in the terminal.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
jira_app = typer.Typer(help="Jira work items", no_args_is_help=True)
conf_app = typer.Typer(help="Confluence pages", no_args_is_help=True)
auth_app = typer.Typer(help="Manage credentials", no_args_is_help=True)
app.add_typer(jira_app, name="jira")
app.add_typer(conf_app, name="confluence")
app.add_typer(auth_app, name="auth")


def _version_callback(value: bool) -> None:
    """Print the version and exit; eager so `--version` wins over any argument."""
    if value:
        print(f"atl {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    _version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            "-V",
            help="Show the version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = None,
) -> None:
    # Root callback: its only job is to host the global --version option. The
    # app's help text stays on `typer.Typer(help=...)`, which takes precedence.
    pass


def _connect() -> AtlassianClient:
    """Load stored credentials and return a ready client."""
    return AtlassianClient(load_credentials())


# Shared option annotations.
Web = Annotated[
    bool, typer.Option("--web", help="open in a browser instead of rendering")
]
Output = Annotated[OutputFormat, typer.Option("-o", "--output", help="output format")]
Limit = Annotated[
    int | None, typer.Option(min=1, help="max results to return (default: all)")
]
OutputPath = Annotated[
    Path | None,
    typer.Option(
        "-o", "--output", help="path to write to (default: the attachment's filename)"
    ),
]

# `atl api` options, named after their `gh api` counterparts.
Method = Annotated[
    str | None,
    typer.Option(
        "-X",
        "--method",
        help="HTTP method (default: GET, or POST once a field or --input is set)",
    ),
]
RawField = Annotated[
    list[str] | None,
    typer.Option("-f", "--raw-field", help="add a string parameter (key=value)"),
]
TypedField = Annotated[
    list[str] | None,
    typer.Option(
        "-F",
        "--field",
        help="add a typed parameter (key=value; true/false/null/int convert, @file reads a value)",
    ),
]
InputSource = Annotated[
    str | None,
    typer.Option("--input", help="raw request body from a file, or '-' for stdin"),
]
Header = Annotated[
    list[str] | None,
    typer.Option("-H", "--header", help="add a request header (key:value)"),
]


@app.command("api")
def api(
    endpoint: Annotated[
        str, typer.Argument(help="REST path (e.g. /rest/api/3/myself) or full URL")
    ],
    method: Method = None,
    raw_field: RawField = None,
    field: TypedField = None,
    input: InputSource = None,
    header: Header = None,
) -> None:
    """Make an authenticated request to the Atlassian REST API (Jira or Confluence)."""
    cmd_api(
        _connect(),
        endpoint,
        method=method,
        raw_fields=raw_field or [],
        typed_fields=field or [],
        input_source=input,
        headers=header or [],
    )


@jira_app.command("view")
def jira_view(
    key: Annotated[str, typer.Argument(help="work item key, e.g. SYS-123")],
    web: Web = False,
    output: Output = OutputFormat.TEXT,
) -> None:
    """View a work item."""
    cmd_jira_view(_connect(), key, web=web, output=output)


@jira_app.command("search")
def jira_search(
    jql: Annotated[str, typer.Argument(help="JQL query")],
    web: Web = False,
    output: Output = OutputFormat.TEXT,
    limit: Limit = None,
) -> None:
    """Search work items with JQL."""
    cmd_jira_search(_connect(), jql, web=web, output=output, limit=limit)


@jira_app.command("download-attachment")
def jira_download_attachment(
    attachment_id: Annotated[
        str, typer.Argument(help="attachment id (shown in `atl jira view`)")
    ],
    output: OutputPath = None,
) -> None:
    """Download a work-item attachment by id."""
    cmd_jira_attachment(_connect(), attachment_id, output=output)


@conf_app.command("view")
def confluence_view(
    page_id: Annotated[int, typer.Argument(min=1, help="numeric page id")],
    web: Web = False,
    output: Output = OutputFormat.TEXT,
) -> None:
    """View a page."""
    cmd_confluence_view(_connect(), str(page_id), web=web, output=output)


@conf_app.command("search")
def confluence_search(
    cql: Annotated[str, typer.Argument(help="CQL query")],
    web: Web = False,
    output: Output = OutputFormat.TEXT,
    limit: Limit = None,
) -> None:
    """Search pages with CQL."""
    cmd_confluence_search(_connect(), cql, web=web, output=output, limit=limit)


@conf_app.command("download-attachment")
def confluence_download_attachment(
    attachment_id: Annotated[
        str,
        typer.Argument(
            help="attachment id, e.g. 'att123456' (shown in `atl confluence view`)"
        ),
    ],
    output: OutputPath = None,
) -> None:
    """Download a page attachment by id."""
    cmd_confluence_attachment(_connect(), attachment_id, output=output)


@auth_app.command("login")
def auth_login() -> None:
    """Save or update Atlassian credentials."""
    cmd_login()


@auth_app.command("logout")
def auth_logout() -> None:
    """Remove stored credentials."""
    remove_credentials()


@auth_app.command("status")
def auth_status() -> None:
    """Show the configured site and account."""
    cmd_status()


def main() -> None:
    """Console entry point: the single boundary that renders AtlError cleanly.

    Typer's pretty-exception handler runs as ``sys.excepthook``, so catching
    AtlError here — before it escapes to the top — keeps it out of that handler.
    Everything else stays Typer's: usage errors (exit 2), --help, Ctrl-C, and
    unexpected bugs (a pretty traceback).
    """
    try:
        app()
    except AtlError as exc:
        # markup=False keeps brackets literal; without it '[...]' in a message is
        # parsed as Rich tags (mangled, or a MarkupError on unbalanced tags).
        err_console.print(f"Error: {exc}", style="red", markup=False)
        raise SystemExit(1) from exc
