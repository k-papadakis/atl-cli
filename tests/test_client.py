"""Tests for the client layer: the pure helpers (error extraction, cursor/filename
parsing, endpoint/product resolution, web-search URL builders) and the
AtlassianClient facade's lazy per-product loading and `atl api` routing.
"""

from collections.abc import Callable
from http import HTTPMethod

import httpx
import pytest

from atl_cli.client import (
    AtlassianClient,
    choose_api_product,
    choose_cloud_id,
    cursor_from,
    error_message,
    filename_from_disposition,
    infer_product,
    resolve_endpoint,
    search_issues_url,
    search_pages_url,
)
from atl_cli.errors import AtlError
from atl_cli.models import (
    Auth,
    Credentials,
    GatewayAuth,
    Product,
    SiteAuth,
    build_gateway_base,
)
from atl_cli.schemas import AccessibleResource


def _creds(
    *,
    auth: Auth | None = None,
    product: Product = Product.JIRA,
) -> Credentials:
    return Credentials(
        username="ada",
        token="t",
        product=product,
        auth=auth or SiteAuth(site_url="https://site"),
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
    creds = _creds(auth=GatewayAuth(site_url="https://site", cloud_id="cid"))
    assert (
        resolve_endpoint(creds, "/rest/api/3/myself")
        == "https://api.atlassian.com/ex/jira/cid/rest/api/3/myself"
    )


def test_resolve_endpoint_passes_same_origin_full_urls_through() -> None:
    """A same-origin full URL is accepted for callers that need an absolute path."""
    url = "https://site/rest/api/3/field"
    assert resolve_endpoint(_creds(), url) == url


def test_resolve_endpoint_accepts_only_the_gateway_origin_in_scoped_mode() -> None:
    """A scoped token reaches its gateway origin only -- never the site it can't use."""
    creds = _creds(auth=GatewayAuth(site_url="https://site", cloud_id="cid"))
    gateway_url = "https://api.atlassian.com/ex/jira/cid/rest/api/3/field"
    assert resolve_endpoint(creds, gateway_url) == gateway_url
    with pytest.raises(AtlError, match="credential's own origin"):
        _ = resolve_endpoint(creds, "https://site/rest/api/3/field")


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
    with pytest.raises(AtlError, match="credential's own origin"):
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


def test_choose_cloud_id_matches_the_configured_site_origin() -> None:
    resources = [
        AccessibleResource(id="other", url="https://other.atlassian.net"),
        AccessibleResource(id="site", url="https://site"),
    ]
    assert choose_cloud_id("https://site", resources) == "site"


def test_choose_cloud_id_returns_none_without_a_matching_resource() -> None:
    resources = [AccessibleResource(id="other", url="https://other.atlassian.net")]
    assert choose_cloud_id("https://site", resources) is None


def test_choose_cloud_id_ignores_malformed_resource_urls() -> None:
    resources = [
        AccessibleResource(id="bad", url="not a URL"),
        AccessibleResource(id="site", url="https://site"),
    ]
    assert choose_cloud_id("https://site", resources) == "site"


def test_build_gateway_base_names_the_product_and_cloud_id() -> None:
    assert (
        build_gateway_base(Product.CONFLUENCE, "cid")
        == "https://api.atlassian.com/ex/confluence/cid"
    )


# --------------------------------------------------------------------------- #
# AtlassianClient facade: lazy per-product loading and `atl api` routing.
# These exercise the injection seam directly -- the client is built from fake
# loader/available callables, so no keyring, filesystem, or network is touched.
# --------------------------------------------------------------------------- #
def _fake_source(
    configured: list[Product], loaded: list[Product]
) -> tuple[Callable[[Product], Credentials], Callable[[], list[Product]]]:
    """A (loader, available) pair over a fixed product set, recording each load."""

    def loader(product: Product) -> Credentials:
        loaded.append(product)
        if product not in configured:
            raise AtlError(f"No {product.value} credentials.")
        return _creds(product=product)

    return loader, lambda: list(configured)


def test_client_loads_only_the_touched_product_and_caches_it() -> None:
    """Touching one surface never loads the other, and a repeat access is cached."""
    loaded: list[Product] = []
    loader, available = _fake_source([Product.JIRA, Product.CONFLUENCE], loaded)
    client = AtlassianClient(loader, available)

    _ = client.jira
    _ = client.jira  # cached_property: the second access must not reload

    assert loaded == [Product.JIRA]  # Confluence is configured but never loaded


def test_api_routes_to_the_product_inferred_from_the_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With both configured, `api` sends each path to that product's credential."""
    seen: list[Product] = []

    def fake_request(
        creds: Credentials, *_args: object, **_kwargs: object
    ) -> httpx.Response:
        seen.append(creds.product)
        return httpx.Response(200, json={})

    monkeypatch.setattr("atl_cli.client._request", fake_request)
    loader, available = _fake_source([Product.JIRA, Product.CONFLUENCE], [])
    client = AtlassianClient(loader, available)

    _ = client.api(HTTPMethod.GET, "/wiki/rest/api/content/1")
    _ = client.api(HTTPMethod.GET, "/rest/api/3/myself")

    assert seen == [Product.CONFLUENCE, Product.JIRA]


def test_api_uses_the_sole_credential_regardless_of_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With one product configured, a Confluence-looking path still uses it and
    never loads the unconfigured product."""
    seen: list[Product] = []

    def fake_request(
        creds: Credentials, *_args: object, **_kwargs: object
    ) -> httpx.Response:
        seen.append(creds.product)
        return httpx.Response(200, json={})

    monkeypatch.setattr("atl_cli.client._request", fake_request)
    loaded: list[Product] = []
    loader, available = _fake_source([Product.JIRA], loaded)
    client = AtlassianClient(loader, available)

    _ = client.api(HTTPMethod.GET, "/wiki/rest/api/content/1")

    assert seen == [Product.JIRA]
    assert loaded == [Product.JIRA]  # the Confluence loader is never invoked
