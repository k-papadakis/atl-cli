"""Commands: orchestrate I/O (HTTP, keyring, stdout, the browser) around the
pure core. Each maps one CLI verb to its side effects.
"""

import getpass
import json
import sys
import webbrowser
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

import httpx
from pydantic import JsonValue

from atl.account import (
    load_credentials,
    read_metadata,
    save_credentials,
    stored_backend,
)
from atl.client import (
    AtlassianClient,
    Headers,
    Params,
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


# --------------------------------------------------------------------------- #
# `atl api` raw passthrough: gh-style field parsing and body/query routing.
#
# The core is pure: strings are parsed into fields, and `plan_request` turns
# them into a `Request` by gh's rules. Only `cmd_api` touches the outside world
# (files, stdin, HTTP, stdout).
# --------------------------------------------------------------------------- #
type Fields = dict[str, JsonValue]

JSON_HEADER: Headers = {"Content-Type": "application/json"}


# A `-F` value before any I/O: either an inline (coerced) literal, or a file to
# read. Resolving the file is deferred to the edge so parsing stays pure.
@dataclass(frozen=True)
class Inline:
    value: JsonValue


@dataclass(frozen=True)
class FromFile:
    path: str


type FieldSource = Inline | FromFile


def coerce_field(value: str) -> JsonValue:
    """gh's `-F` type magic: JSON literals and integers convert, else a string."""
    match value:
        case "true":
            return True
        case "false":
            return False
        case "null":
            return None
        case _:
            try:
                return int(value)
            except ValueError:
                return value


def typed_source(value: str) -> FieldSource:
    """Classify a `-F` value: `@path` reads a file (`@-` = stdin), else coerce."""
    if value.startswith("@"):
        return FromFile(value.removeprefix("@"))
    return Inline(coerce_field(value))


def parse_pairs(items: list[str], sep: str, kind: str) -> list[tuple[str, str]]:
    """Split each ``key<sep>value`` item; a missing separator or key is fatal."""
    pairs: list[tuple[str, str]] = []
    for item in items:
        key, found, value = item.partition(sep)
        if not found or not key:
            raise AtlError(f"Invalid {kind} {item!r}; expected key{sep}value.")
        pairs.append((key, value))
    return pairs


def _query_value(value: JsonValue) -> str:
    """Render a coerced field as a query-string value (bool → true/false)."""
    match value:
        case bool():
            return "true" if value else "false"
        case None:
            return "null"
        case _:
            return str(value)


def _query(fields: Fields) -> Params | None:
    return {key: _query_value(value) for key, value in fields.items()} or None


# What a request actually sends, once routing is decided. Kept as data so the
# decision is a pure, testable function separate from the HTTP call.
@dataclass(frozen=True)
class Request:
    method: str
    params: Params | None
    content: bytes | None
    headers: Headers


def plan_request(
    method: str | None, fields: Fields, input_body: bytes | None
) -> Request:
    """Decide method, query vs body, and headers by gh's rules (pure).

    Default method is GET, or POST once there is anything to send. A raw
    ``--input`` body wins and pushes fields to the query string; otherwise a
    GET/HEAD carries fields as query params and any other method serializes
    them as a JSON body. Atlassian bodies are JSON, so a body defaults the
    Content-Type (a user ``-H`` still overrides it).
    """
    resolved = (
        method or ("POST" if fields or input_body is not None else "GET")
    ).upper()
    match (input_body, resolved):
        case (bytes() as body, _):
            return Request(resolved, _query(fields), body, JSON_HEADER)
        case (None, "GET" | "HEAD"):
            return Request(resolved, _query(fields), None, {})
        case (None, _) if fields:
            return Request(resolved, None, json.dumps(fields).encode(), JSON_HEADER)
        case _:
            return Request(resolved, None, None, {})


def render_response(resp: httpx.Response) -> str | None:
    """The text to print for a raw response: JSON pretty-printed, else verbatim.

    ``None`` means an empty body (e.g. 204), so nothing should be printed.
    """
    if not resp.content:
        return None
    if "json" not in resp.headers.get("content-type", ""):
        return resp.text
    try:
        return json.dumps(cast(JsonValue, resp.json()), indent=2)
    except json.JSONDecodeError:
        return resp.text  # mislabeled body: print it verbatim


def cmd_api(
    client: AtlassianClient,
    endpoint: str,
    *,
    method: str | None,
    raw_fields: list[str],
    typed_fields: list[str],
    input_source: str | None,
    headers: list[str],
) -> None:
    # Pure parse first, then the edge: resolve @file/@- sources and --input.
    fields: Fields = dict(parse_pairs(raw_fields, "=", "field"))
    fields.update(
        {
            key: resolve_field(typed_source(value))
            for key, value in parse_pairs(typed_fields, "=", "field")
        }
    )
    input_body = None if input_source is None else read_source(input_source).encode()
    user_headers: Headers = {
        key.strip(): value.strip() for key, value in parse_pairs(headers, ":", "header")
    }

    # Later flags win: user headers override the JSON default.
    req = plan_request(method, fields, input_body)
    resp = client.api(
        req.method,
        endpoint,
        params=req.params,
        content=req.content,
        headers={**req.headers, **user_headers} or None,
    )
    if (text := render_response(resp)) is not None:
        print(text)


def read_source(path: str) -> str:
    """Read a value from a file, or from stdin when ``path`` is ``-``."""
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text()
    except OSError as exc:
        raise AtlError(f"Could not read {path}: {exc}") from exc


def resolve_field(source: FieldSource) -> JsonValue:
    """Resolve a typed-field source to a value (reads a file for ``FromFile``)."""
    match source:
        case Inline(value):
            return value
        case FromFile(path):
            return read_source(path)


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
