"""atl - fetch, render and search Atlassian (Jira + Confluence) content.

Grammar (noun-first):
    atl jira       view <KEY>       work item + comments
    atl jira       search <jql>     search work items with JQL
    atl confluence view <id>        wiki page
    atl confluence search <cql>     search pages with CQL
    atl auth       login | logout | status

Common options for view/search:
    --web            open in a browser instead of rendering
    -o, --output     output format: text (default) or json
    --limit <n>      max search results (default: all)

Global options:
    -V, --version    show the version and exit
    -h, --help       show help and exit

Design: the package is layered so side effects sit at the edges.

    config / errors / models   foundation (constants, domain types)
    schemas                    pydantic models for the API responses
    rendering                  pure: typed models in, Markdown out
    account / client           I/O: keyring, filesystem, HTTP + validation
    commands                   orchestrate I/O around the pure core
    cli                        argument parsing and dispatch
"""

from importlib.metadata import version

# Distribution name ("atl-cli"), not the import name ("atl_cli"); see pyproject.
__version__ = version("atl-cli")
