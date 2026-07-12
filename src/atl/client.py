"""The Atlassian HTTP client: fetch JSON and validate it into typed models.

`AtlassianClient` is a thin facade exposing two namespaced surfaces, `jira` and
`confluence`, whose methods return typed models (or raw JSON, for the `*_json`
siblings that back `--json`). The endpoint URLs, query params and error labels
live here; `commands.py` just orchestrates and prints.
"""

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import ClassVar, cast

import httpx
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from atl.config import HTTP_TIMEOUT, SEARCH_PAGE_SIZE
from atl.errors import AtlError
from atl.models import Credentials, Page
from atl.schemas import (
    ApiError,
    CommentPage,
    ConfluenceAttachment,
    ConfluenceComments,
    ConfluencePage,
    ConfluenceSearchPage,
    Issue,
    JiraSearchPage,
    RemoteLink,
    SearchIssue,
    SearchPage,
    User,
    WorklogPage,
)

type Params = dict[str, str | int]
type Headers = dict[str, str]


def _http_status(resp: httpx.Response) -> str:
    return f"HTTP {resp.status_code} {resp.reason_phrase}".strip()


def error_message(resp: httpx.Response) -> str:
    """Extract a human-readable message from an error response body.

    Only Atlassian's own structured error text is surfaced; a non-JSON body
    (e.g. an untrusted proxy/gateway page) is reduced to the status line rather
    than echoed verbatim.
    """
    try:
        data = cast(JsonValue, resp.json())
    except json.JSONDecodeError:
        return _http_status(resp)
    try:
        error = ApiError.model_validate(data)
    except ValidationError:
        return _http_status(resp)
    # Jira puts general problems in errorMessages and field-specific ones in an
    # errors dict; Confluence uses a single message. Surface whatever is present.
    texts = list(error.error_messages)
    texts += [msg for msg in error.errors.values() if msg]
    if error.message:
        texts.append(error.message)
    return "; ".join(texts) if texts else _http_status(resp)


def validation_error_message(label: str, exc: ValidationError) -> str:
    return f"{label} API returned data in an unexpected shape:\n{exc}"


def cursor_from(next_url: str) -> str | None:
    """Pull the opaque ``cursor`` out of a Confluence ``_links.next`` URL.

    Content search paginates by cursor (not offset), and the next cursor is only
    exposed inside that relative URL's query string.
    """
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(next_url).query).get("cursor")
    return values[0] if values else None


# --------------------------------------------------------------------------- #
# Human-facing web search URLs (for `--web`; distinct from the REST endpoints
# below). These encode a user query into a query string; the trivial per-item
# URLs (browse/viewpage) are open-coded at their call sites instead.
# --------------------------------------------------------------------------- #
def search_issues_url(base_url: str, jql: str) -> str:
    return f"{base_url}/issues/?{httpx.QueryParams(jql=jql)}"


def search_pages_url(base_url: str, cql: str) -> str:
    return f"{base_url}/wiki/search?{httpx.QueryParams(cql=cql)}"


def resolve_endpoint(base_url: str, endpoint: str) -> str:
    """Resolve a raw-passthrough endpoint against the site root.

    A full URL is used verbatim; anything else is joined to ``base_url`` (a
    leading slash is optional), so the caller types the real REST path -- Jira
    (``/rest/...``) and Confluence (``/wiki/...``) alike -- version included.
    """
    if endpoint.startswith(("http://", "https://")):
        return endpoint
    return f"{base_url}/{endpoint.removeprefix('/')}"


# --------------------------------------------------------------------------- #
# Transport primitives: fetch JSON and (optionally) validate it into a model.
# --------------------------------------------------------------------------- #
def _request(
    creds: Credentials,
    method: str,
    url: str,
    *,
    params: Params | None = None,
    content: bytes | None = None,
    headers: Headers | None = None,
    label: str,
) -> httpx.Response:
    """Perform an authenticated request, mapping transport errors to AtlError.

    ``follow_redirects`` carries the auth through the signed-media hop that
    attachment endpoints redirect to.
    """
    try:
        resp = httpx.request(
            method,
            url,
            params=params,
            content=content,
            headers=headers,
            auth=(creds.username, creds.token),
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
        )
        _ = resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise AtlError(f"{label} API failed: {error_message(exc.response)}") from exc
    except httpx.HTTPError as exc:
        raise AtlError(f"{label} API request failed: {exc}") from exc
    return resp


def _get_response(
    creds: Credentials, url: str, params: Params | None = None, *, label: str
) -> httpx.Response:
    """GET a URL with auth, mapping transport errors to AtlError."""
    return _request(creds, "GET", url, params=params, label=label)


def _fetch_json(
    creds: Credentials, url: str, params: Params | None = None, *, label: str
) -> JsonValue:
    """Return the raw decoded JSON, mapping transport errors to AtlError."""
    resp = _get_response(creds, url, params, label=label)
    try:
        return cast(JsonValue, resp.json())
    except json.JSONDecodeError as exc:
        raise AtlError(
            f"{label} API returned a non-JSON response ({_http_status(resp)})."
        ) from exc


def _page_size(limit: int | None, fetched: int) -> int:
    """Next request's size: the standard page, capped so we never overshoot
    ``limit`` (``None`` = no cap → fetch everything)."""
    if limit is None:
        return SEARCH_PAGE_SIZE
    return min(SEARCH_PAGE_SIZE, limit - fetched)


def _validate_list[M: BaseModel](
    data: JsonValue, schema: type[M], *, label: str
) -> list[M]:
    """Validate already-fetched data as a list of ``schema``."""
    try:
        return TypeAdapter(list[schema]).validate_python(data)
    except ValidationError as exc:
        raise AtlError(validation_error_message(label, exc)) from exc


def _fetch_model[M: BaseModel](
    creds: Credentials,
    url: str,
    schema: type[M],
    params: Params | None = None,
    *,
    label: str,
) -> M:
    data = _fetch_json(creds, url, params, label=label)
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise AtlError(validation_error_message(label, exc)) from exc


def _fetch_list[M: BaseModel](
    creds: Credentials,
    url: str,
    schema: type[M],
    params: Params | None = None,
    *,
    label: str,
) -> list[M]:
    return _validate_list(
        _fetch_json(creds, url, params, label=label), schema, label=label
    )


def filename_from_disposition(value: str | None) -> str | None:
    """Pull a filename out of a Content-Disposition header, if present.

    Prefers RFC 5987's ``filename*=UTF-8''<percent-encoded>`` (which carries
    non-ASCII names) over the plain ``filename=`` form.
    """
    if not value:
        return None
    if m := re.search(r"filename\*=(?:[\w-]+'[^']*')?([^;]+)", value, re.IGNORECASE):
        return urllib.parse.unquote(m.group(1).strip().strip('"'))
    if m := re.search(r'filename="?([^";]+)"?', value, re.IGNORECASE):
        return m.group(1).strip()
    return None


def _fetch_bytes(
    creds: Credentials, url: str, *, label: str
) -> tuple[bytes, str | None]:
    """Download a binary body, returning the bytes and any server-suggested name."""
    resp = _get_response(creds, url, label=label)
    disposition = cast("str | None", resp.headers.get("content-disposition"))
    return resp.content, filename_from_disposition(disposition)


# --------------------------------------------------------------------------- #
# Namespaced API surfaces
# --------------------------------------------------------------------------- #
@dataclass
class JiraApi:
    creds: Credentials

    def _api(self) -> str:
        """The versioned API root -- the single place the Jira version lives."""
        return f"{self.creds.base_url}/rest/api/3"

    def _issue_url(self, key: str) -> str:
        return f"{self._api()}/issue/{key}"

    def _search_url(self) -> str:
        return f"{self._api()}/search/jql"

    def get_myself(self) -> User:
        """Fetch the authenticated user; used to prove a token actually works."""
        return _fetch_model(self.creds, f"{self._api()}/myself", User, label="Jira")

    def get_issue(self, key: str) -> Issue:
        return _fetch_model(
            self.creds,
            self._issue_url(key),
            Issue,
            # renderedFields -> the HTML description; changelog -> status history.
            {"fields": "*all", "expand": "renderedFields,changelog"},
            label="Jira",
        )

    def get_issue_json(self, key: str) -> JsonValue:
        return _fetch_json(
            self.creds,
            self._issue_url(key),
            {"fields": "*all", "expand": "renderedFields"},
            label="Jira",
        )

    def get_remote_links(self, key: str) -> list[RemoteLink]:
        return _fetch_list(
            self.creds, f"{self._issue_url(key)}/remotelink", RemoteLink, label="Jira"
        )

    def get_worklog(self, key: str) -> WorklogPage:
        return _fetch_model(
            self.creds, f"{self._issue_url(key)}/worklog", WorklogPage, label="Jira"
        )

    def get_comments(self, key: str) -> CommentPage:
        return _fetch_model(
            self.creds,
            f"{self._issue_url(key)}/comment",
            CommentPage,
            {"expand": "renderedBody", "maxResults": 100},
            label="Jira",
        )

    def _search_pages(
        self, jql: str, limit: int | None
    ) -> tuple[list[JsonValue], bool]:
        """Page through ``/search/jql`` until exhausted or ``limit`` is reached.

        Returns the accumulated raw issue objects and whether the server still
        had results beyond the ones we kept. ``limit=None`` fetches everything.
        """
        issues: list[JsonValue] = []
        token: str | None = None
        while True:
            params: Params = {
                "jql": jql,
                "fields": "summary,status,assignee",
                "maxResults": _page_size(limit, len(issues)),
            }
            if token:
                params["nextPageToken"] = token
            page = _fetch_model(
                self.creds, self._search_url(), JiraSearchPage, params, label="Jira"
            )
            issues.extend(page.issues)
            token = page.next_page_token
            server_more = page.is_last is False or token is not None
            if not server_more or (limit is not None and len(issues) >= limit):
                return issues, server_more

    def search(self, jql: str, limit: int | None) -> Page[SearchIssue]:
        issues, more = self._search_pages(jql, limit)
        # Project the merged raw issues into the typed items the table renders.
        items = _validate_list(issues, SearchIssue, label="Jira")
        return Page(items, more)

    def search_json(self, jql: str, limit: int | None) -> list[JsonValue]:
        # The merged raw issues, each exactly as the wire returned it. Search
        # spans multiple responses, so there is no single envelope to preserve;
        # the honest projection is the concatenated list of result objects.
        issues, _more = self._search_pages(jql, limit)
        return issues

    def get_epic_children(self, key: str) -> Page[SearchIssue]:
        # An epic's children point back at it through the standard `parent`
        # field (company-managed projects). Fetch every child.
        return self.search(f"parent = {key}", None)

    def download_attachment(self, attachment_id: str) -> tuple[bytes, str | None]:
        return _fetch_bytes(
            self.creds,
            f"{self._api()}/attachment/content/{attachment_id}",
            label="Jira",
        )


@dataclass
class ConfluenceApi:
    """Confluence read API, on the v1 REST API throughout: it is the only
    version offering content search, so staying on it keeps every response
    (page, comments, search) a single uniform ``content`` shape.
    """

    creds: Credentials

    def _api(self) -> str:
        """The versioned API root -- the single place the Confluence version lives."""
        return f"{self.creds.base_url}/wiki/rest/api"

    def _page_url(self, page_id: str) -> str:
        return f"{self._api()}/content/{page_id}"

    # One GET folds the rendered body plus child pages and attachments (each
    # capped at the API page size) into a single payload.
    _PAGE_EXPAND: ClassVar[str] = "body.view,children.page,children.attachment"

    def _search_url(self) -> str:
        return f"{self._api()}/content/search"

    def get_page(self, page_id: str) -> ConfluencePage:
        return _fetch_model(
            self.creds,
            self._page_url(page_id),
            ConfluencePage,
            {"expand": self._PAGE_EXPAND},
            label="Confluence",
        )

    def get_page_json(self, page_id: str) -> JsonValue:
        return _fetch_json(
            self.creds,
            self._page_url(page_id),
            {"expand": self._PAGE_EXPAND},
            label="Confluence",
        )

    def get_page_comments(self, page_id: str) -> ConfluenceComments:
        return _fetch_model(
            self.creds,
            f"{self._page_url(page_id)}/child/comment",
            ConfluenceComments,
            {"expand": "body.view,history", "limit": 100},
            label="Confluence",
        )

    def _search_pages(
        self, cql: str, limit: int | None
    ) -> tuple[list[JsonValue], bool]:
        """Page through content search by cursor until exhausted or ``limit``.

        Returns the accumulated raw results and whether more remained beyond the
        ones we kept. ``limit=None`` fetches everything.
        """
        results: list[JsonValue] = []
        cursor: str | None = None
        while True:
            params: Params = {"cql": cql, "limit": _page_size(limit, len(results))}
            if cursor:
                params["cursor"] = cursor
            page = _fetch_model(
                self.creds,
                self._search_url(),
                ConfluenceSearchPage,
                params,
                label="Confluence",
            )
            results.extend(page.results)
            next_url = page.links.next
            if not next_url or (limit is not None and len(results) >= limit):
                return results, next_url is not None
            cursor = cursor_from(next_url)
            if cursor is None:  # defensive: a next link we can't follow
                return results, True

    def search(self, cql: str, limit: int | None) -> Page[SearchPage]:
        results, more = self._search_pages(cql, limit)
        # Project the merged raw results into the typed rows the table renders.
        items = _validate_list(results, SearchPage, label="Confluence")
        return Page(items, more)

    def search_json(self, cql: str, limit: int | None) -> list[JsonValue]:
        # See JiraApi.search_json: the merged raw results, each verbatim wire.
        results, _more = self._search_pages(cql, limit)
        return results

    def download_attachment(self, attachment_id: str) -> tuple[bytes, str | None]:
        # Resolve the attachment to its download path (which embeds the parent
        # page id we don't otherwise have), then fetch the bytes.
        meta = _fetch_model(
            self.creds,
            f"{self._api()}/content/{attachment_id}",
            ConfluenceAttachment,
            label="Confluence",
        )
        if not meta.links.download:
            raise AtlError(f"Attachment {attachment_id} has no download link.")
        data, disp = _fetch_bytes(
            self.creds,
            f"{self.creds.base_url}/wiki{meta.links.download}",
            label="Confluence",
        )
        return data, meta.title or disp


@dataclass
class AtlassianClient:
    creds: Credentials
    jira: JiraApi = field(init=False)
    confluence: ConfluenceApi = field(init=False)

    def __post_init__(self) -> None:
        self.jira = JiraApi(self.creds)
        self.confluence = ConfluenceApi(self.creds)

    def api(
        self,
        method: str,
        endpoint: str,
        *,
        params: Params | None = None,
        content: bytes | None = None,
        headers: Headers | None = None,
    ) -> httpx.Response:
        """Raw passthrough: an arbitrary authenticated request to the site.

        Backs `atl api`; the endpoint is resolved against the site root so any
        REST surface (Jira, Confluence, Agile, v2, ...) is reachable.
        """
        return _request(
            self.creds,
            method,
            resolve_endpoint(self.creds.base_url, endpoint),
            params=params,
            content=content,
            headers=headers,
            label="Atlassian",
        )
