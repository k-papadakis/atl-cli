"""Tests for the pure `atl api` core: field coercion/classification and the
request planner. No I/O -- file/stdin reads and the HTTP call live in `cmd_api`.
"""

import pytest

from atl_cli.commands import (
    FromFile,
    Inline,
    coerce_field,
    normalize_base_url,
    plan_request,
    typed_source,
)
from atl_cli.errors import AtlError


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
    assert (
        plan_request("delete", {}, None).method == "DELETE"
    )  # explicit wins, upper-cased


def test_plan_request_routes_get_fields_to_the_query_string() -> None:
    """A GET carries fields as query params, never a body; bools render as true/false."""
    req = plan_request("GET", {"expand": "groups", "active": True}, None)
    assert req.params == {"expand": "groups", "active": "true"}
    assert req.content is None
    assert "Content-Type" not in req.headers


def test_plan_request_serializes_non_get_fields_as_a_json_body() -> None:
    """A non-GET with fields sends a JSON body and defaults the Content-Type."""
    req = plan_request("POST", {"name": "x"}, None)
    assert req.params is None
    assert req.content == b'{"name": "x"}'
    assert req.headers["Content-Type"] == "application/json"


def test_plan_request_raw_body_wins_and_pushes_fields_to_the_query() -> None:
    """`--input` owns the body; fields fall back to the query string regardless of method."""
    req = plan_request("PUT", {"notify": False}, b'{"body": 1}')
    assert req.content == b'{"body": 1}'
    assert req.params == {"notify": "false"}
    assert req.headers["Content-Type"] == "application/json"


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
