"""Tests for the pure client helpers: error message extraction, cursor/filename
parsing, endpoint validation, and web-search URL builders.
"""

import httpx
import pytest

from atl_cli.client import (
    build_gateway_base,
    choose_api_product,
    cursor_from,
    error_message,
    filename_from_disposition,
    infer_product,
    resolve_endpoint,
    search_issues_url,
    search_pages_url,
)
from atl_cli.errors import AtlError
from atl_cli.models import AuthMode, Credentials, Product


def _creds(
    base: str = "https://site",
    *,
    site_url: str = "https://site",
    mode: AuthMode = AuthMode.SITE,
    product: Product = Product.JIRA,
) -> Credentials:
    return Credentials(
        base_url=base,
        username="ada",
        token="t",
        product=product,
        mode=mode,
        site_url=site_url,
    )


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
    creds = _creds()
    assert (
        resolve_endpoint(creds, "/rest/api/3/myself")
        == "https://site/rest/api/3/myself"
    )
    assert (
        resolve_endpoint(creds, "rest/api/3/myself") == "https://site/rest/api/3/myself"
    )


def test_resolve_endpoint_joins_relative_paths_to_the_gateway_in_scoped_mode() -> None:
    """A scoped credential's relative path hangs off the gateway root, not the site."""
    creds = _creds("https://api.atlassian.com/ex/jira/cid", mode=AuthMode.GATEWAY)
    assert (
        resolve_endpoint(creds, "/rest/api/3/myself")
        == "https://api.atlassian.com/ex/jira/cid/rest/api/3/myself"
    )


def test_resolve_endpoint_passes_same_origin_full_urls_through() -> None:
    """A same-origin full URL is accepted for callers that need an absolute path."""
    url = "https://site/rest/api/3/field"
    assert resolve_endpoint(_creds(), url) == url


def test_resolve_endpoint_accepts_the_gateway_origin_in_scoped_mode() -> None:
    """Both the gateway origin and the human site origin are allowed for a scoped token."""
    creds = _creds("https://api.atlassian.com/ex/jira/cid", mode=AuthMode.GATEWAY)
    gateway_url = "https://api.atlassian.com/ex/jira/cid/rest/api/3/field"
    assert resolve_endpoint(creds, gateway_url) == gateway_url
    assert (
        resolve_endpoint(creds, "https://site/rest/api/3/field")
        == "https://site/rest/api/3/field"
    )


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
        _ = resolve_endpoint(_creds(), endpoint)


def test_infer_product_routes_by_path() -> None:
    """Confluence lives under /wiki or /ex/confluence/; everything else is Jira."""
    assert infer_product("/wiki/rest/api/content/1") is Product.CONFLUENCE
    assert infer_product("/rest/api/3/myself") is Product.JIRA
    assert (
        infer_product("https://api.atlassian.com/ex/confluence/cid/wiki/rest/api/x")
        is Product.CONFLUENCE
    )
    assert (
        infer_product("https://api.atlassian.com/ex/jira/cid/rest/api/3/x")
        is Product.JIRA
    )


def test_infer_product_override_wins() -> None:
    assert infer_product("/rest/api/3/myself", Product.CONFLUENCE) is Product.CONFLUENCE


def test_choose_api_product_uses_the_sole_credential_for_any_path() -> None:
    """With one credential configured, `atl api` uses it regardless of the path."""
    assert (
        choose_api_product("/wiki/rest/api/content/1", None, [Product.JIRA])
        is Product.JIRA
    )


def test_choose_api_product_infers_when_both_are_configured() -> None:
    both = [Product.JIRA, Product.CONFLUENCE]
    assert choose_api_product("/wiki/rest/api/x", None, both) is Product.CONFLUENCE
    assert choose_api_product("/rest/api/3/myself", None, both) is Product.JIRA


def test_choose_api_product_override_always_wins() -> None:
    assert (
        choose_api_product("/rest/api/3/x", Product.CONFLUENCE, [Product.JIRA])
        is Product.CONFLUENCE
    )


def test_build_gateway_base_names_the_product_and_cloud_id() -> None:
    assert (
        build_gateway_base(Product.CONFLUENCE, "cid")
        == "https://api.atlassian.com/ex/confluence/cid"
    )
