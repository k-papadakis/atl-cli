"""Commands: orchestrate I/O (HTTP, keyring, stdout, the browser) around the
pure core. Each maps one CLI verb to its side effects.
"""

import getpass
import json
import sys
import webbrowser
from enum import StrEnum
from pathlib import Path

from pydantic import JsonValue

from atl.account import (
    load_credentials,
    read_metadata,
    save_credentials,
    stored_backend,
)
from atl.client import (
    AtlassianClient,
    search_issues_url,
    search_pages_url,
)
from atl.config import CRED_FILE
from atl.errors import AtlError
from atl.models import Credentials, Page
from atl.rendering import (
    confluence_search_table,
    jira_search_table,
    render_confluence_page,
    render_jira_issue,
)
from atl.schemas import SearchIssue


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


def open_url(url: str) -> None:
    # webbrowser.open returns False when it can't find a browser to launch; treat
    # that as a real failure rather than exiting 0 and leaving the user unsure
    # whether anything happened. The URL is in the message so it can be copied.
    if not webbrowser.open(url):
        raise AtlError(f"No web browser is available. Open this URL manually:\n{url}")


def print_json(data: JsonValue) -> None:
    print(json.dumps(data, indent=2))


def report_count(count: int, more: bool) -> None:
    suffix = " (more available; raise --limit)" if more else ""
    print(f"{count} result(s){suffix}", file=sys.stderr)


def cmd_login() -> None:
    base_url = input("Atlassian site URL (e.g. 'https://mycompany.atlassian.net'): ")
    username = input("Atlassian email/username: ")
    if not base_url or not username:
        raise AtlError("Site URL and username are required.")

    # Normalize to the bare site host so both the Jira ('/rest/...') and
    # Confluence ('/wiki/rest/...') base URLs can be derived from it.
    base_url = base_url.strip().rstrip("/").removesuffix("/wiki")

    token = getpass.getpass("Atlassian API token: ")
    if not token:
        raise AtlError("API token is required.")

    username = username.strip()
    # Verify the credentials against the authenticated "who am I" endpoint before
    # persisting, so we never store a token that doesn't work.
    client = AtlassianClient(Credentials(base_url, username, token))
    try:
        me = client.jira.get_myself()
    except AtlError as exc:
        raise AtlError(
            f"Could not verify credentials; nothing was saved. {exc}"
        ) from exc

    backend = save_credentials(base_url, username, token)
    print(f"Verified as {me.display_name or username}.", file=sys.stderr)
    print(
        f"Configuration saved to {CRED_FILE} (token backend: {backend.value})",
        file=sys.stderr,
    )


def cmd_status() -> None:
    meta = read_metadata()
    print(
        f"Logged in to {meta.url} as {meta.username} "
        + f"(token backend: {stored_backend(meta).value})"
    )
    me = AtlassianClient(load_credentials()).jira.get_myself()
    print(f"Token verified — authenticated as {me.display_name or meta.username}.")


def cmd_jira_view(
    client: AtlassianClient, key: str, *, web: bool, output: OutputFormat
) -> None:
    if web:
        open_url(f"{client.creds.base_url}/browse/{key}")
        return

    match output:
        case OutputFormat.JSON:
            # The raw single-issue payload (all fields). Unlike the text view, it
            # deliberately does not fold in the separate remote-link/worklog/
            # comment endpoints: `-o json` is the wire issue, for scripting.
            print_json(client.jira.get_issue_json(key))
        case OutputFormat.TEXT:
            issue = client.jira.get_issue(key)
            remote_links = client.jira.get_remote_links(key)
            worklog = client.jira.get_worklog(key)
            comments = client.jira.get_comments(key)
            # Epics keep their children out-of-band; fetch them only for epics
            # so a normal issue stays a single round of requests.
            issuetype = issue.fields.issuetype
            children: Page[SearchIssue] = (
                client.jira.get_epic_children(key)
                if issuetype and issuetype.name == "Epic"
                else Page([], more=False)
            )
            print(render_jira_issue(issue, remote_links, worklog, comments, children))


def cmd_jira_search(
    client: AtlassianClient,
    jql: str,
    *,
    web: bool,
    output: OutputFormat,
    limit: int | None,
) -> None:
    base_url = client.creds.base_url
    if web:
        open_url(search_issues_url(base_url, jql))
        return

    match output:
        case OutputFormat.JSON:
            print_json(client.jira.search_json(jql, limit))
        case OutputFormat.TEXT:
            st = jira_search_table(base_url, client.jira.search(jql, limit))
            report_count(st.count, st.more)
            print(st.table)


def cmd_confluence_view(
    client: AtlassianClient, page_id: str, *, web: bool, output: OutputFormat
) -> None:
    if web:
        open_url(f"{client.creds.base_url}/wiki/pages/viewpage.action?pageId={page_id}")
        return

    match output:
        case OutputFormat.JSON:
            print_json(client.confluence.get_page_json(page_id))
        case OutputFormat.TEXT:
            page = client.confluence.get_page(page_id)
            comments = client.confluence.get_page_comments(page_id)
            print(render_confluence_page(page, comments))


def cmd_confluence_search(
    client: AtlassianClient,
    cql: str,
    *,
    web: bool,
    output: OutputFormat,
    limit: int | None,
) -> None:
    base_url = client.creds.base_url
    if web:
        open_url(search_pages_url(base_url, cql))
        return

    match output:
        case OutputFormat.JSON:
            print_json(client.confluence.search_json(cql, limit))
        case OutputFormat.TEXT:
            st = confluence_search_table(base_url, client.confluence.search(cql, limit))
            report_count(st.count, st.more)
            print(st.table)


def _save_download(data: bytes, filename: str | None, output: Path | None) -> None:
    """Write downloaded bytes, refusing to clobber an existing file."""
    name = Path(filename or "attachment").name or "attachment"
    path = output or Path(name)
    if path.is_dir():
        path = path / name
    if path.exists():
        raise AtlError(
            f"Refusing to overwrite existing file: {path}. Pass --output to choose a path."
        )
    _ = path.write_bytes(data)
    print(f"Saved {path} ({len(data)} bytes).", file=sys.stderr)


def cmd_jira_attachment(
    client: AtlassianClient, attachment_id: str, *, output: Path | None
) -> None:
    data, filename = client.jira.download_attachment(attachment_id)
    _save_download(data, filename, output)


def cmd_confluence_attachment(
    client: AtlassianClient, attachment_id: str, *, output: Path | None
) -> None:
    data, filename = client.confluence.download_attachment(attachment_id)
    _save_download(data, filename, output)
