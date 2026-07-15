"""Tests for the pure credential-storage policy.

The decisions -- the keyring key scheme, which backend to use, how a new
credential merges into the document, whether a failed write must roll the
keyring back, and how a token is resolved on load -- are pure functions, so
these run on plain data with no keyring, filesystem, or mocks involved.
"""

import pytest

from atl_cli.account import (
    keyring_service,
    plan_storage,
    resolve_token,
    stored_backend,
)
from atl_cli.errors import AtlError
from atl_cli.models import (
    AuthMode,
    Product,
    StoredCredential,
    StoredMetadata,
    TokenBackend,
)


def _plan(existing: StoredMetadata | None = None, *, keyring_ok: bool):
    return plan_storage(
        existing or StoredMetadata(),
        Product.JIRA,
        base_url="https://site",
        site_url="https://site",
        username="ada",
        token="secret",
        mode=AuthMode.SITE,
        cloud_id=None,
        keyring_ok=keyring_ok,
    )


def test_keyring_service_is_scoped_per_product() -> None:
    assert keyring_service(Product.JIRA) == "atl-cli-jira"
    assert keyring_service(Product.CONFLUENCE) == "atl-cli-confluence"


def test_plan_storage_keeps_token_out_of_the_file_when_keyring_works() -> None:
    plan = _plan(keyring_ok=True)
    assert plan.backend is TokenBackend.KEYRING
    assert plan.rollback_keyring is True  # a failed write must undo the keyring set
    jira = StoredMetadata.model_validate(plan.document).credentials[Product.JIRA]
    assert jira.token is None  # the secret stays in the keyring, not the file
    assert jira.url == "https://site"
    assert jira.username == "ada"


def test_plan_storage_falls_back_to_file_when_keyring_unavailable() -> None:
    plan = _plan(keyring_ok=False)
    assert plan.backend is TokenBackend.FILE
    assert plan.rollback_keyring is False  # nothing was put in the keyring to undo
    jira = StoredMetadata.model_validate(plan.document).credentials[Product.JIRA]
    assert jira.token == "secret"


def test_plan_storage_preserves_the_other_products_credential() -> None:
    existing = StoredMetadata(
        credentials={
            Product.CONFLUENCE: StoredCredential(
                url="https://site", username="ada", mode=AuthMode.GATEWAY
            )
        },
    )
    plan = _plan(existing, keyring_ok=True)
    doc = StoredMetadata.model_validate(plan.document)
    assert set(doc.credentials) == {Product.JIRA, Product.CONFLUENCE}


def test_resolve_token_prefers_the_file_token_over_the_keyring() -> None:
    cred = StoredCredential(url="u", username="ada", token="file-tok")
    assert resolve_token(Product.JIRA, cred, "keyring-tok") == "file-tok"


def test_resolve_token_falls_back_to_the_keyring() -> None:
    cred = StoredCredential(url="u", username="ada")
    assert resolve_token(Product.JIRA, cred, "keyring-tok") == "keyring-tok"


def test_resolve_token_raises_when_absent_everywhere() -> None:
    cred = StoredCredential(url="u", username="ada")
    with pytest.raises(AtlError, match="Token not found"):
        _ = resolve_token(Product.CONFLUENCE, cred, None)


def test_stored_backend_reflects_where_the_token_lives() -> None:
    in_file = StoredCredential(url="u", username="ada", token="t")
    in_keyring = StoredCredential(url="u", username="ada")
    assert stored_backend(in_file) is TokenBackend.FILE
    assert stored_backend(in_keyring) is TokenBackend.KEYRING
