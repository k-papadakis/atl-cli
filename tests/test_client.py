"""Tests for the pure client helpers: error message extraction, cursor/filename
parsing, endpoint validation, and web-search URL builders.
"""

import httpx
import pytest

from atl_cli.client import (
    cursor_from,
    error_message,
    filename_from_disposition,
    resolve_endpoint,
    search_issues_url,
    search_pages_url,
)
from atl_cli.errors import AtlError


def test_error_message_joins_general_and_field_errors() -> None:
    """All errorMessages and field-specific errors are surfaced, not just the first."""
    resp = httpx.Response(
        400,
        json={"errorMessages": ["Bad request"], "errors": {"jql": "Field 'x' unknown"}},
    )
    assert error_message(resp) == "Bad request; Field 'x' unknown"


def test_error_message_falls_back_to_status_for_non_json() -> None:
    """A non-JSON body (proxy/gateway page) is reduced to the status line."""
    resp = httpx.Response(502, text="<html>gateway error</html>")
    assert error_message(resp) == "HTTP 502 Bad Gateway"


def test_cursor_from_extracts_the_confluence_cursor() -> None:
    """The next-page cursor is parsed out of a relative _links.next URL."""
    assert (
        cursor_from("/rest/api/content/search?cql=x&limit=25&cursor=ABC123") == "ABC123"
    )
    assert cursor_from("/rest/api/content/search?cql=x&limit=25") is None


def test_filename_from_disposition() -> None:
    """The RFC 5987 encoded form wins over a plain filename; missing → None."""
    assert filename_from_disposition('attachment; filename="log.txt"') == "log.txt"
    assert (
        filename_from_disposition("attachment; filename*=UTF-8''sp%C3%A9c%20file.png")
        == "spéc file.png"
    )
    assert filename_from_disposition(None) is None
    assert filename_from_disposition("inline") is None


def test_search_issues_url_encodes_the_jql() -> None:
    url = search_issues_url("https://site", "project = SYS AND status = Open")
    assert url.startswith("https://site/issues/?jql=")
    assert " " not in url  # spaces and '=' are percent/plus-encoded, not raw


def test_search_pages_url_encodes_the_cql() -> None:
    url = search_pages_url("https://site", 'text ~ "release notes"')
    assert url.startswith("https://site/wiki/search?cql=")
    assert " " not in url
    assert '"' not in url  # spaces and quotes are encoded, not left to break the URL


def test_resolve_endpoint_joins_paths_to_the_site_root() -> None:
    """A leading slash is optional; both forms hang off the site root."""
    base = "https://site"
    assert (
        resolve_endpoint(base, "/rest/api/3/myself") == "https://site/rest/api/3/myself"
    )
    assert (
        resolve_endpoint(base, "rest/api/3/myself") == "https://site/rest/api/3/myself"
    )


def test_resolve_endpoint_passes_same_origin_full_urls_through() -> None:
    """A same-origin full URL is accepted for callers that need an absolute path."""
    url = "https://site/rest/api/3/field"
    assert resolve_endpoint("https://site", url) == url


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://other.atlassian.net/rest/api/3/field",
        "http://site/rest/api/3/field",
        "https://site:8443/rest/api/3/field",
    ],
)
def test_resolve_endpoint_rejects_cross_origin_urls(endpoint: str) -> None:
    """Configured credentials must never be sent to a different origin."""
    with pytest.raises(AtlError, match="configured site's origin"):
        _ = resolve_endpoint("https://site", endpoint)
