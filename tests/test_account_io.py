"""Filesystem-bound credential persistence tests."""

from dataclasses import dataclass, field
from pathlib import Path

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
