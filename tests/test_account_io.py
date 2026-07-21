"""Filesystem-bound credential persistence tests."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import override

import keyring.errors
import pytest

from atl_cli.account import CredentialStore, KeyringStore
from atl_cli.errors import AtlError
from atl_cli.models import Product, SiteAuth


@dataclass
class MemoryKeyring:
    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def set(self, service: str, username: str, token: str) -> None:
        self.values[service, username] = token

    def get(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def delete(self, service: str, username: str) -> None:
        _ = self.values.pop((service, username), None)


class UnavailableKeyring(MemoryKeyring):
    @override
    def set(self, service: str, username: str, token: str) -> None:
        raise keyring.errors.NoKeyringError()


def test_save_credentials_does_not_overwrite_a_malformed_file(tmp_path: Path) -> None:
    cred_file = tmp_path / "credentials.json"
    original = '{"legacy": "credentials"}'
    _ = cred_file.write_text(original)

    with pytest.raises(AtlError, match="Credentials file is malformed"):
        _ = CredentialStore(cred_file=cred_file, config_dir=tmp_path).save(
            Product.JIRA,
            auth=SiteAuth(site_url="https://site"),
            username="ada",
            token="secret",
        )

    assert cred_file.read_text() == original


def test_store_round_trips_a_keyring_token(tmp_path: Path) -> None:
    keyring: KeyringStore = MemoryKeyring()
    store = CredentialStore(
        cred_file=tmp_path / "credentials.json",
        config_dir=tmp_path,
        keyring=keyring,
    )

    assert (
        store.save(
            Product.JIRA,
            auth=SiteAuth(site_url="https://site"),
            username="ada",
            token="secret",
        ).value
        == "keyring"
    )
    assert store.load(Product.JIRA).token == "secret"
    assert '"token"' not in store.cred_file.read_text()


def test_store_falls_back_to_file_when_keyring_is_unavailable(tmp_path: Path) -> None:
    store = CredentialStore(
        cred_file=tmp_path / "credentials.json",
        config_dir=tmp_path,
        keyring=UnavailableKeyring(),
    )

    _ = store.save(
        Product.JIRA,
        auth=SiteAuth(site_url="https://site"),
        username="ada",
        token="secret",
    )

    assert store.load(Product.JIRA).token == "secret"
    assert '"token": "secret"' in store.cred_file.read_text()


def test_store_rolls_back_keyring_when_metadata_write_fails(tmp_path: Path) -> None:
    keyring = MemoryKeyring()
    parent_file = tmp_path / "not-a-directory"
    _ = parent_file.write_text("not a directory")
    store = CredentialStore(
        cred_file=parent_file / "credentials.json",
        config_dir=tmp_path,
        keyring=keyring,
    )

    with pytest.raises(AtlError, match="Could not write"):
        _ = store.save(
            Product.JIRA,
            auth=SiteAuth(site_url="https://site"),
            username="ada",
            token="secret",
        )

    assert keyring.values == {}


def test_store_restores_previous_keyring_token_when_replacement_fails(
    tmp_path: Path,
) -> None:
    keyring = MemoryKeyring()
    service_key = ("atl-cli-jira", "ada")
    keyring.values[service_key] = "old-secret"
    parent_file = tmp_path / "not-a-directory"
    _ = parent_file.write_text("not a directory")
    store = CredentialStore(
        cred_file=parent_file / "credentials.json",
        config_dir=tmp_path,
        keyring=keyring,
    )

    with pytest.raises(AtlError, match="Could not write"):
        _ = store.save(
            Product.JIRA,
            auth=SiteAuth(site_url="https://site"),
            username="ada",
            token="new-secret",
        )

    assert keyring.values[service_key] == "old-secret"


def test_store_keeps_keyring_token_when_remove_write_fails(tmp_path: Path) -> None:
    keyring = MemoryKeyring()
    cred_file = tmp_path / "credentials.json"
    store = CredentialStore(cred_file=cred_file, config_dir=tmp_path, keyring=keyring)
    _ = store.save(
        Product.JIRA,
        auth=SiteAuth(site_url="https://site"),
        username="ada",
        token="secret",
    )
    original_mode = tmp_path.stat().st_mode
    _ = os.chmod(tmp_path, 0o500)
    try:
        with pytest.raises(AtlError, match=r"Could not (write|remove)"):
            store.remove(Product.JIRA)
    finally:
        _ = os.chmod(tmp_path, original_mode)

    assert keyring.values[("atl-cli-jira", "ada")] == "secret"


def test_store_keeps_keyring_tokens_when_remove_all_fails(tmp_path: Path) -> None:
    keyring = MemoryKeyring()
    cred_file = tmp_path / "credentials.json"
    store = CredentialStore(cred_file=cred_file, config_dir=tmp_path, keyring=keyring)
    _ = store.save(
        Product.JIRA,
        auth=SiteAuth(site_url="https://site"),
        username="ada",
        token="secret",
    )
    original_mode = tmp_path.stat().st_mode
    _ = os.chmod(tmp_path, 0o500)
    try:
        with pytest.raises(AtlError, match="Could not remove"):
            store.remove_all()
    finally:
        _ = os.chmod(tmp_path, original_mode)

    assert cred_file.exists()
    assert keyring.values[("atl-cli-jira", "ada")] == "secret"


def test_store_remove_deletes_keyring_token_and_preserves_other_product(
    tmp_path: Path,
) -> None:
    keyring = MemoryKeyring()
    store = CredentialStore(
        cred_file=tmp_path / "credentials.json",
        config_dir=tmp_path,
        keyring=keyring,
    )
    _ = store.save(
        Product.JIRA,
        auth=SiteAuth(site_url="https://jira"),
        username="ada",
        token="jira-token",
    )
    _ = store.save(
        Product.CONFLUENCE,
        auth=SiteAuth(site_url="https://confluence"),
        username="ada",
        token="confluence-token",
    )

    store.remove(Product.JIRA)

    assert store.load(Product.CONFLUENCE).token == "confluence-token"
    assert keyring.get("atl-cli-jira", "ada") is None
