"""Tests for the pure credential-storage policy.

The decisions -- the keyring key scheme, which backend to use, how a new
credential merges into the document, whether a failed write must roll the
keyring back, and how a token is resolved on load -- are pure functions, so
these run on plain data with no keyring, filesystem, or mocks involved.
"""

import pytest
from pydantic import ValidationError

from atl_cli.account import (
    keyring_service,
    plan_storage,
    resolve_token,
    stored_backend,
)
from atl_cli.errors import AtlError
from atl_cli.models import (
    GatewayAuth,
    Product,
    SiteAuth,
    StoredCredential,
    StoredMetadata,
    TokenBackend,
)


def _plan(existing: StoredMetadata | None = None, *, keyring_ok: bool):
    return plan_storage(
        existing or StoredMetadata(),
        Product.JIRA,
        auth=SiteAuth(site_url="https://site"),
        username="ada",
        token="secret",
        keyring_ok=keyring_ok,
    )


def test_keyring_service_is_scoped_per_product() -> None:
    assert keyring_service(Product.JIRA) == "atl-cli-jira"
    assert keyring_service(Product.CONFLUENCE) == "atl-cli-confluence"


def test_plan_storage_keeps_token_out_of_the_file_when_keyring_works() -> None:
    plan = _plan(keyring_ok=True)
    assert plan.backend is TokenBackend.KEYRING
    jira = StoredMetadata.model_validate(plan.document).credentials[Product.JIRA]
    assert jira.token is None  # the secret stays in the keyring, not the file
    assert jira.auth == SiteAuth(site_url="https://site")
    assert jira.username == "ada"


def test_plan_storage_serializes_the_tagged_auth_shape() -> None:
    plan = _plan(keyring_ok=True)
    # The whole document, exactly: the token stays in the keyring (dropped by
    # exclude_none) and the REST root is derived, so no "url" is persisted.
    assert plan.document == {
        "credentials": {
            "jira": {
                "username": "ada",
                "auth": {"kind": "site", "site_url": "https://site"},
            }
        }
    }


def test_plan_storage_falls_back_to_file_when_keyring_unavailable() -> None:
    plan = _plan(keyring_ok=False)
    assert plan.backend is TokenBackend.FILE
    jira = StoredMetadata.model_validate(plan.document).credentials[Product.JIRA]
    assert jira.token == "secret"


def test_plan_storage_preserves_the_other_products_credential() -> None:
    existing = StoredMetadata(
        credentials={
            Product.CONFLUENCE: StoredCredential(
                username="ada",
                auth=GatewayAuth(site_url="https://site", cloud_id="cid"),
            )
        },
    )
    plan = _plan(existing, keyring_ok=True)
    doc = StoredMetadata.model_validate(plan.document)
    assert set(doc.credentials) == {Product.JIRA, Product.CONFLUENCE}


def test_gateway_auth_requires_a_cloud_id() -> None:
    with pytest.raises(ValidationError):
        _ = GatewayAuth(site_url="https://site", cloud_id="")


def test_gateway_auth_without_a_cloud_id_fails_the_discriminated_union() -> None:
    with pytest.raises(ValidationError):
        _ = StoredCredential.model_validate(
            {"username": "ada", "auth": {"kind": "gateway", "site_url": "https://site"}}
        )


def test_site_auth_rejects_a_stray_cloud_id() -> None:
    with pytest.raises(ValidationError):
        _ = StoredCredential.model_validate(
            {
                "username": "ada",
                "auth": {"kind": "site", "site_url": "https://site", "cloud_id": "x"},
            }
        )


def test_stored_credential_rejects_an_empty_username() -> None:
    with pytest.raises(ValidationError):
        _ = StoredCredential(username="", auth=SiteAuth(site_url="https://site"))


def test_stored_credential_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _ = StoredCredential.model_validate(
            {
                "username": "ada",
                "url": "https://site",  # the old, now-removed field
                "auth": {"kind": "site", "site_url": "https://site"},
            }
        )


def test_stored_metadata_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _ = StoredMetadata.model_validate({"credentialz": {}})


def test_resolve_token_prefers_the_file_token_over_the_keyring() -> None:
    cred = StoredCredential(
        username="ada", token="file-tok", auth=SiteAuth(site_url="https://site")
    )
    assert resolve_token(Product.JIRA, cred, "keyring-tok") == "file-tok"


def test_resolve_token_falls_back_to_the_keyring() -> None:
    cred = StoredCredential(username="ada", auth=SiteAuth(site_url="https://site"))
    assert resolve_token(Product.JIRA, cred, "keyring-tok") == "keyring-tok"


def test_resolve_token_raises_when_absent_everywhere() -> None:
    cred = StoredCredential(username="ada", auth=SiteAuth(site_url="https://site"))
    with pytest.raises(AtlError, match="Token not found"):
        _ = resolve_token(Product.CONFLUENCE, cred, None)


def test_stored_backend_reflects_where_the_token_lives() -> None:
    in_file = StoredCredential(
        username="ada", token="t", auth=SiteAuth(site_url="https://site")
    )
    in_keyring = StoredCredential(
        username="ada", auth=SiteAuth(site_url="https://site")
    )
    assert stored_backend(in_file) is TokenBackend.FILE
    assert stored_backend(in_keyring) is TokenBackend.KEYRING
