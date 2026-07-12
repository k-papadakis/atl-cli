"""Tests for the pure credential-storage policy.

The decisions -- which backend to use, what the metadata file carries, whether a
failed write must roll the keyring back, and how a token is resolved on load --
are pure functions, so these run on plain data with no keyring, filesystem, or
mocks involved.
"""

import pytest

from atl_cli.account import plan_storage, resolve_token, stored_backend
from atl_cli.errors import AtlError
from atl_cli.models import StoredMetadata, TokenBackend


def test_plan_storage_keeps_token_out_of_the_file_when_keyring_works() -> None:
    plan = plan_storage("https://site", "ada", "secret", keyring_ok=True)
    assert plan.backend is TokenBackend.KEYRING
    assert plan.metadata == {"url": "https://site", "username": "ada"}
    assert plan.rollback_keyring is True  # a failed write must undo the keyring set


def test_plan_storage_falls_back_to_file_when_keyring_unavailable() -> None:
    plan = plan_storage("https://site", "ada", "secret", keyring_ok=False)
    assert plan.backend is TokenBackend.FILE
    assert plan.metadata["token"] == "secret"
    assert plan.rollback_keyring is False  # nothing was put in the keyring to undo


def test_resolve_token_prefers_the_file_token_over_the_keyring() -> None:
    meta = StoredMetadata(url="u", username="ada", token="file-tok")
    assert resolve_token(meta, "keyring-tok") == "file-tok"


def test_resolve_token_falls_back_to_the_keyring() -> None:
    meta = StoredMetadata(url="u", username="ada")
    assert resolve_token(meta, "keyring-tok") == "keyring-tok"


def test_resolve_token_raises_when_absent_everywhere() -> None:
    meta = StoredMetadata(url="u", username="ada")
    with pytest.raises(AtlError, match="Token not found"):
        _ = resolve_token(meta, None)


def test_stored_backend_reflects_where_the_token_lives() -> None:
    in_file = StoredMetadata(url="u", username="ada", token="t")
    in_keyring = StoredMetadata(url="u", username="ada")
    assert stored_backend(in_file) is TokenBackend.FILE
    assert stored_backend(in_keyring) is TokenBackend.KEYRING
