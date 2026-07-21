"""Tests for the pure `atl api` core: field coercion/classification and the
request planner. No I/O -- file/stdin reads and the HTTP call live in `cmd_api`.
"""

from http import HTTPMethod

import httpx
import pytest

from atl_cli.commands import (
    FromFile,
    Inline,
    RenderedBody,
    build_credential,
    coerce_field,
    normalize_base_url,
    plan_request,
    render_response,
    typed_source,
)
from atl_cli.errors import AtlError
from atl_cli.models import GatewayAuth, Product, SiteAuth


def test_coerce_field_applies_gh_type_magic() -> None:
    """true/false/null and integers convert to JSON types; everything else stays a string."""
    assert coerce_field("true") is True
    assert coerce_field("false") is False
    assert coerce_field("null") is None
    assert coerce_field("42") == 42
    assert coerce_field("BST-1") == "BST-1"
    assert coerce_field("3.14") == "3.14"  # only integers convert, matching gh


def test_typed_source_classifies_at_prefixed_values_as_files() -> None:
    """`@path` becomes a FromFile (`@-` = stdin); anything else is a coerced Inline."""
    assert typed_source("@body.json") == FromFile("body.json")
    assert typed_source("@-") == FromFile("-")
    assert typed_source("42") == Inline(42)
    assert typed_source("hello") == Inline("hello")


def test_plan_request_defaults_to_get_then_post_when_there_is_a_payload() -> None:
    """No payload → GET; any field or body flips the default to POST (gh's rule)."""
    assert plan_request(None, {}, None).method == "GET"
    assert plan_request(None, {"a": "b"}, None).method == "POST"
    assert plan_request(None, {}, b"{}").method == "POST"
    assert plan_request(HTTPMethod.DELETE, {}, None).method == "DELETE"  # explicit wins


def test_plan_request_routes_get_fields_to_the_query_string() -> None:
    """A GET carries fields as query params, never a body; bools render as true/false."""
    req = plan_request(HTTPMethod.GET, {"expand": "groups", "active": True}, None)
    assert req.params == {"expand": "groups", "active": "true"}
    assert req.content is None
    assert "Content-Type" not in req.headers


def test_plan_request_serializes_non_get_fields_as_a_json_body() -> None:
    """A non-GET with fields sends a JSON body and defaults the Content-Type."""
    req = plan_request(HTTPMethod.POST, {"name": "x"}, None)
    assert req.params is None
    assert req.content == b'{"name": "x"}'
    assert req.headers["Content-Type"] == "application/json"


def test_plan_request_raw_body_wins_and_pushes_fields_to_the_query() -> None:
    """`--input` owns the body; fields fall back to the query string regardless of method."""
    req = plan_request(HTTPMethod.PUT, {"notify": False}, b'{"body": 1}')
    assert req.content == b'{"body": 1}'
    assert req.params == {"notify": "false"}
    assert req.headers["Content-Type"] == "application/json"


def test_render_response_empty_body_prints_nothing() -> None:
    """An empty body (e.g. 204) yields None so the caller prints nothing."""
    assert render_response(httpx.Response(204)) is None


def test_render_response_pretty_prints_and_flags_real_json() -> None:
    """A JSON body is re-indented and tagged with the 'json' lexer for highlighting."""
    resp = httpx.Response(
        200, headers={"content-type": "application/json"}, content=b'{"a":1}'
    )
    assert render_response(resp) == RenderedBody('{\n  "a": 1\n}', "json")


def test_render_response_non_json_prints_verbatim() -> None:
    """A non-JSON content-type prints verbatim with no lexer."""
    resp = httpx.Response(200, headers={"content-type": "text/plain"}, content=b"hello")
    assert render_response(resp) == RenderedBody("hello", None)


def test_render_response_mislabeled_json_falls_back_to_verbatim() -> None:
    """A body that claims JSON but fails to parse prints verbatim, not JSON-lexed."""
    resp = httpx.Response(
        200, headers={"content-type": "application/json"}, content=b"not json"
    )
    assert render_response(resp) == RenderedBody("not json", None)


def test_normalize_base_url_strips_trailing_slash_and_wiki_suffix() -> None:
    """A valid site URL is reduced to its bare host root, dropping /wiki and slashes."""
    assert (
        normalize_base_url("https://acme.atlassian.net") == "https://acme.atlassian.net"
    )
    assert (
        normalize_base_url("  https://acme.atlassian.net/  ")
        == "https://acme.atlassian.net"
    )
    assert (
        normalize_base_url("https://acme.atlassian.net/wiki")
        == "https://acme.atlassian.net"
    )
    assert (
        normalize_base_url("https://acme.atlassian.net/wiki/")
        == "https://acme.atlassian.net"
    )
    assert normalize_base_url("http://localhost:8080") == "http://localhost:8080"


def test_normalize_base_url_rejects_non_http_urls() -> None:
    """A bare host, a non-http scheme, a scheme with no host, or a blank all fail fast."""
    for bad in ("acme.atlassian.net", "ftp://acme.atlassian.net", "https://", "   "):
        with pytest.raises(AtlError):
            _ = normalize_base_url(bad)


def test_build_credential_uses_the_site_root_in_site_mode() -> None:
    """A classic token talks to the site root; --web links use the same site."""
    creds = build_credential(
        Product.JIRA, "ada", "t", SiteAuth(site_url="https://site")
    )
    assert creds.base_url == "https://site"
    assert creds.auth.kind == "site"
    assert creds.web_base == "https://site"


def test_build_credential_uses_the_gateway_root_in_gateway_mode() -> None:
    """A scoped token talks to the gateway, but --web links still use the human site."""
    creds = build_credential(
        Product.CONFLUENCE,
        "ada",
        "t",
        GatewayAuth(site_url="https://site", cloud_id="cid"),
    )
    assert creds.base_url == "https://api.atlassian.com/ex/confluence/cid"
    assert creds.web_base == "https://site"
