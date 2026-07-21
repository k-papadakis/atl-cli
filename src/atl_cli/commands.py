"""Commands: orchestrate I/O (HTTP, keyring, stdout, the browser) around the
pure core. Each maps one CLI verb to its side effects.
"""

import json
import sys
import webbrowser
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from typing import cast

import httpx
from pydantic import HttpUrl, JsonValue, TypeAdapter, ValidationError

from atl_cli.account import (
    available_products,
    load_credentials,
    read_metadata,
    save_credentials,
    stored_backend,
)
from atl_cli.client import (
    AtlassianClient,
    ConfluenceApi,
    Headers,
    JiraApi,
    Params,
    error_message,
    probe,
    resolve_cloud_id,
    search_issues_url,
    search_pages_url,
)
from atl_cli.config import CRED_FILE
from atl_cli.console import (
    emit_code,
    emit_json,
    emit_markdown,
    emit_table,
    emit_text,
    note,
    prompt,
    prompt_secret,
    spinner,
    status_line,
    success,
)
from atl_cli.errors import AtlError
from atl_cli.models import (
    Auth,
    Credentials,
    GatewayAuth,
    Page,
    Product,
    SiteAuth,
    StoredCredential,
)
from atl_cli.rendering import (
    confluence_search_table,
    jira_search_table,
    render_confluence_page,
    render_jira_issue,
    search_summary,
)
from atl_cli.schemas import SearchIssue, User


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


def open_url(url: str) -> None:
    # webbrowser.open returns False when it can't find a browser to launch; treat
    # that as a real failure rather than exiting 0 and leaving the user unsure
    # whether anything happened. The URL is in the message so it can be copied.
    if not webbrowser.open(url):
        raise AtlError(f"No web browser is available. Open this URL manually:\n{url}")


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
    method: HTTPMethod
    params: Params | None
    content: bytes | None
    headers: Headers


def plan_request(
    method: HTTPMethod | None, fields: Fields, input_body: bytes | None
) -> Request:
    """Decide method, query vs body, and headers by gh's rules (pure).

    Default method is GET, or POST once there is anything to send. A raw
    ``--input`` body wins and pushes fields to the query string; otherwise a
    GET/HEAD carries fields as query params and any other method serializes
    them as a JSON body. Atlassian bodies are JSON, so a body defaults the
    Content-Type (a user ``-H`` still overrides it).
    """
    resolved = method or (
        HTTPMethod.POST if fields or input_body is not None else HTTPMethod.GET
    )
    match (input_body, resolved):
        case (bytes() as body, _):
            return Request(resolved, _query(fields), body, JSON_HEADER)
        case (None, HTTPMethod.GET | HTTPMethod.HEAD):
            return Request(resolved, _query(fields), None, {})
        case (None, _) if fields:
            return Request(resolved, None, json.dumps(fields).encode(), JSON_HEADER)
        case _:
            return Request(resolved, None, None, {})


@dataclass(frozen=True)
class RenderedBody:
    """The text to print for a raw response, and how to render it.

    ``language`` is the syntax to highlight as (``"json"`` for a body that
    actually parses as JSON), or ``None`` to print ``text`` verbatim.
    """

    text: str
    language: str | None


def render_response(resp: httpx.Response) -> RenderedBody | None:
    """The body to print for a raw response, or ``None`` for an empty body.

    A body mislabeled as JSON that fails to parse falls back to verbatim text.
    ``None`` means an empty body (e.g. 204), so nothing should be printed.
    """
    if not resp.content:
        return None
    if "json" in resp.headers.get("content-type", ""):
        try:
            return RenderedBody(
                json.dumps(cast(JsonValue, resp.json()), indent=2), "json"
            )
        except json.JSONDecodeError:
            pass  # mislabeled body: fall through to verbatim
    return RenderedBody(resp.text, None)


def cmd_api(
    client: AtlassianClient,
    endpoint: str,
    *,
    method: HTTPMethod | None,
    raw_fields: list[str],
    typed_fields: list[str],
    input_source: str | None,
    headers: list[str],
    product: Product | None,
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
        product=product,
        params=req.params,
        content=req.content,
        headers={**req.headers, **user_headers} or None,
    )
    # Colorize a JSON body like `-o json`; anything else prints verbatim.
    body = render_response(resp)
    if body is None:
        return
    if body.language is None:
        emit_text(body.text)
    else:
        emit_code(body.text, body.language)


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


def normalize_base_url(raw: str) -> str:
    """Validate a site URL and normalize it to its bare host root.

    Validation is delegated to pydantic's HttpUrl (requires an http(s):// URL
    with a host). We keep pydantic for validation only and normalize the raw
    string ourselves -- HttpUrl would rewrite a host-only URL with a trailing
    slash. Strips a trailing slash and a '/wiki' suffix so both the Jira
    ('/rest/...') and Confluence ('/wiki/rest/...') base URLs derive from it.
    """
    url = raw.strip()
    try:
        _ = TypeAdapter(HttpUrl).validate_python(url)
    except ValidationError as exc:
        raise AtlError(
            f"Invalid site URL {url!r}; expected a full URL like "
            + "'https://mycompany.atlassian.net'."
        ) from exc
    return url.rstrip("/").removesuffix("/wiki")


def build_credential(
    product: Product,
    username: str,
    token: str,
    auth: Auth,
) -> Credentials:
    """Assemble a candidate credential from an auth variant (pure). The REST root
    is derived from the variant by ``Credentials.base_url``."""
    return Credentials(
        username=username,
        token=token,
        product=product,
        auth=auth,
    )


def _detect_credential(
    product: Product, site: str, username: str, token: str
) -> tuple[Credentials, User]:
    """Verify the token and settle its auth mode, without persisting anything.

    Probes the site URL first (a classic token). A 401/403 there means the token
    may be scoped, so we resolve the cloudId and re-probe the gateway. Any other
    failure aborts with nothing saved. Returns the working credential and the
    verified user.
    """
    site_creds = build_credential(product, username, token, SiteAuth(site_url=site))
    try:
        return site_creds, probe(site_creds)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in (
            HTTPStatus.UNAUTHORIZED,
            HTTPStatus.FORBIDDEN,
        ):
            raise AtlError(
                f"Could not verify credentials; nothing was saved. {error_message(exc.response)}"
            ) from exc
    except AtlError as exc:
        raise AtlError(
            f"Could not verify credentials; nothing was saved. {exc}"
        ) from exc

    # The site rejected the token as unauthorized: it is likely a scoped token,
    # which only works through the api.atlassian.com gateway.
    cloud_id = resolve_cloud_id(site, username, token)
    gw_creds = build_credential(
        product, username, token, GatewayAuth(site_url=site, cloud_id=cloud_id)
    )
    try:
        return gw_creds, probe(gw_creds)
    except httpx.HTTPStatusError as exc:
        raise AtlError(
            f"Could not verify the scoped token; nothing was saved. {error_message(exc.response)}"
        ) from exc
    except AtlError as exc:
        raise AtlError(
            f"Could not verify the scoped token; nothing was saved. {exc}"
        ) from exc


def cmd_login(product: Product) -> None:
    site = normalize_base_url(
        prompt("Atlassian site URL", hint="e.g. 'https://mycompany.atlassian.net'")
    )
    username = prompt("Atlassian email/username").strip()
    if not username:
        raise AtlError("Username is required.")

    token = prompt_secret("Atlassian API token", hint=product.value)
    if not token:
        raise AtlError("API token is required.")

    # Verify the credentials before persisting, so we never store a token that
    # doesn't work -- and let the probe settle whether it is a classic or scoped
    # token (site URL vs gateway).
    creds, me = _detect_credential(product, site, username, token)

    backend = save_credentials(
        product,
        auth=creds.auth,
        username=username,
        token=token,
    )
    success(f"Verified as {me.display_name or username}.")
    # The where/how detail is secondary to the green confirmation above, so grey
    # it out rather than compete with it for attention.
    note(
        f"{product.value.capitalize()} credentials saved to {CRED_FILE} "
        + f"(mode: {creds.auth.kind}, token backend: {backend.value})"
    )


def _verify_for_status(product: Product, cred: StoredCredential) -> str:
    """Load the product's credential and confirm the token; return the display name."""
    creds = load_credentials(product)
    api = JiraApi(creds) if product is Product.JIRA else ConfluenceApi(creds)
    me = api.get_myself()
    return me.display_name or cred.username


def cmd_status(product: Product) -> bool:
    """Report a product's account and whether its token still verifies.

    Returns True only if the token was retrieved and accepted. Verification is a
    reported outcome, not a fatal error -- the account line and the verdict are
    printed in order, so a keyring/network/token failure can't contradict an
    already-emitted success line.
    """
    meta = read_metadata()
    cred = meta.credentials.get(product)
    if cred is None:
        status_line(
            f"Not logged in to {product.value}. "
            + f"Run 'atl auth login {product.value}'.",
            style="yellow",
        )
        return True
    status_line(
        f"Logged in to {cred.auth.site_url} as {cred.username} "
        + f"(product: {product.value}, mode: {cred.auth.kind}, "
        + f"token backend: {stored_backend(cred).value})"
    )
    try:
        display = _verify_for_status(product, cred)
    except AtlError as exc:
        status_line(f"Token could not be verified: {exc}", style="red")
        return False
    status_line(f"Token verified — authenticated as {display}.", style="green")
    return True


def cmd_status_all() -> bool:
    """Show every configured product (for the top-level ``atl auth status``).

    Every configured product is always shown; the return is False if any of them
    failed to verify.
    """
    products = available_products()
    if not products:
        status_line(
            "Not logged in. Run 'atl auth login jira' or "
            + "'atl auth login confluence'.",
            style="yellow",
        )
        return True
    # Materialize before aggregating: every product must be shown, so all of the
    # (printing) cmd_status calls have to run. A generator would let all() short-
    # circuit on the first failure and skip the rest of the output.
    results = [cmd_status(product) for product in products]
    return all(results)


def cmd_jira_view(
    client: AtlassianClient, key: str, *, web: bool, output: OutputFormat
) -> None:
    if web:
        open_url(f"{client.jira.creds.web_base}/browse/{key}")
        return

    match output:
        case OutputFormat.JSON:
            # The raw single-issue payload (all fields). Unlike the text view, it
            # deliberately does not fold in the separate remote-link/worklog/
            # comment endpoints: `-o json` is the wire issue, for scripting.
            with spinner(f"Loading {key}…"):
                data = client.jira.get_issue_json(key)
            emit_json(data)
        case OutputFormat.TEXT:
            with spinner(f"Loading {key}…"):
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
            emit_markdown(
                render_jira_issue(issue, remote_links, worklog, comments, children)
            )


def cmd_jira_search(
    client: AtlassianClient,
    jql: str,
    *,
    web: bool,
    output: OutputFormat,
    limit: int | None,
) -> None:
    base_url = client.jira.creds.web_base
    if web:
        open_url(search_issues_url(base_url, jql))
        return

    match output:
        case OutputFormat.JSON:
            with spinner("Searching…"):
                data = client.jira.search_json(jql, limit)
            emit_json(data)
        case OutputFormat.TEXT:
            with spinner("Searching…"):
                st = jira_search_table(client.jira.search(jql, limit))
            note(search_summary(len(st.rows), st.more, f"{base_url}/browse/<KEY>"))
            emit_table(st)


def cmd_confluence_view(
    client: AtlassianClient, page_id: str, *, web: bool, output: OutputFormat
) -> None:
    if web:
        open_url(
            f"{client.confluence.creds.web_base}/wiki/pages/viewpage.action?pageId={page_id}"
        )
        return

    match output:
        case OutputFormat.JSON:
            with spinner(f"Loading page {page_id}…"):
                data = client.confluence.get_page_json(page_id)
            emit_json(data)
        case OutputFormat.TEXT:
            with spinner(f"Loading page {page_id}…"):
                page = client.confluence.get_page(page_id)
                comments = client.confluence.get_page_comments(page_id)
            emit_markdown(render_confluence_page(page, comments))


def cmd_confluence_search(
    client: AtlassianClient,
    cql: str,
    *,
    web: bool,
    output: OutputFormat,
    limit: int | None,
) -> None:
    base_url = client.confluence.creds.web_base
    if web:
        open_url(search_pages_url(base_url, cql))
        return

    match output:
        case OutputFormat.JSON:
            with spinner("Searching…"):
                data = client.confluence.search_json(cql, limit)
            emit_json(data)
        case OutputFormat.TEXT:
            with spinner("Searching…"):
                st = confluence_search_table(client.confluence.search(cql, limit))
            note(
                search_summary(
                    len(st.rows),
                    st.more,
                    f"{base_url}/wiki/pages/viewpage.action?pageId=<ID>",
                )
            )
            emit_table(st)


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
    success(f"Saved {path} ({len(data)} bytes).")


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
