"""Credential persistence: metadata in a config file, the token in the keyring.

Credentials are stored *per product* (Jira, Confluence): a scoped API token is
locked to a single product, so each gets its own entry with its own auth variant
(site or gateway). Non-secret metadata lives in a mode-600 JSON file; the token
is kept in the OS keyring (keyed per product), falling back to that file when no
keyring backend is usable.

The storage *policy* (key scheme, backend choice, merge, rollback, token
resolution) lives in the pure functions below; the rest is the thin I/O shell.
"""

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import keyring
import keyring.errors
from pydantic import JsonValue, ValidationError

from atl_cli.config import CONFIG_DIR, CRED_FILE, KEYRING_SERVICE, PROG
from atl_cli.console import success, warn
from atl_cli.errors import AtlError
from atl_cli.models import (
    Auth,
    Credentials,
    Product,
    StoredCredential,
    StoredMetadata,
    TokenBackend,
)


# --------------------------------------------------------------------------- #
# Pure storage policy
# --------------------------------------------------------------------------- #
def keyring_service(product: Product) -> str:
    """The keyring service (Keychain item name) for a product's token.

    The product is namespaced into the *service*, keyed by the plain username, so
    each product is its own entry and the stored account stays equal to the real
    login identity -- rather than overloading the username with a compound key.
    """
    return f"{KEYRING_SERVICE}-{product.value}"


def serialize_metadata(
    credentials: dict[Product, StoredCredential],
) -> dict[str, JsonValue]:
    """Render a per-product credential map as the on-disk document."""
    return {
        "credentials": {
            product.value: cred.model_dump(mode="json", exclude_none=True)
            for product, cred in credentials.items()
        },
    }


@dataclass(frozen=True, slots=True)
class StoragePlan:
    """The document to persist and which backend holds its token.

    A ``KEYRING`` backend means the token was placed in the keyring, so a failed
    metadata write must delete it again -- otherwise the secret is orphaned with
    no file to locate it by.
    """

    document: dict[str, JsonValue]
    backend: TokenBackend


def plan_storage(
    existing: StoredMetadata,
    product: Product,
    *,
    auth: Auth,
    username: str,
    token: str,
    keyring_ok: bool,
) -> StoragePlan:
    """Merge a new/updated per-product credential into the existing document.

    With a working keyring the token stays out of the file (keyring backend);
    otherwise it falls into the mode-600 file. Any credential for the *other*
    product is preserved.
    """
    cred = StoredCredential(
        username=username,
        token=None if keyring_ok else token,
        auth=auth,
    )
    credentials = dict(existing.credentials)
    credentials[product] = cred
    document = serialize_metadata(credentials)
    backend = TokenBackend.KEYRING if keyring_ok else TokenBackend.FILE
    return StoragePlan(document, backend)


def resolve_token(
    product: Product, cred: StoredCredential, keyring_token: str | None
) -> str:
    """Pick the effective token: a file token wins, else the keyring's."""
    token = cred.token or keyring_token or ""
    if not token:
        raise AtlError(f"Token not found. Run '{PROG} auth login {product.value}'.")
    return token


def stored_backend(cred: StoredCredential) -> TokenBackend:
    return TokenBackend.FILE if cred.token else TokenBackend.KEYRING


# --------------------------------------------------------------------------- #
# I/O shell
# --------------------------------------------------------------------------- #
class KeyringStore(Protocol):
    """The keyring operations needed by credential persistence."""

    def set(self, service: str, username: str, token: str) -> None: ...

    def get(self, service: str, username: str) -> str | None: ...

    def delete(self, service: str, username: str) -> None: ...


class SystemKeyring:
    """Adapter for the process' OS keyring backend."""

    def set(self, service: str, username: str, token: str) -> None:
        keyring.set_password(service, username, token)

    def get(self, service: str, username: str) -> str | None:
        return keyring.get_password(service, username)

    def delete(self, service: str, username: str) -> None:
        keyring.delete_password(service, username)


def _write_secure(path: Path, data: str) -> None:
    """Atomically write text to a mode-600 file.

    The data is written to a temp file in the same directory -- created 0o600 by
    ``mkstemp``, so it is never briefly group/world-readable the way a plain
    ``write_text`` would be -- then renamed into place. The rename is atomic, so
    a crashed write can't leave a truncated credentials file behind.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            _ = f.write(data)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@dataclass(frozen=True, slots=True)
class CredentialStore:
    """Filesystem and keyring boundary for persisted credentials."""

    cred_file: Path = CRED_FILE
    config_dir: Path = CONFIG_DIR
    keyring: KeyringStore = field(default_factory=SystemKeyring)

    def _read_metadata_or_empty(self) -> StoredMetadata:
        if not self.cred_file.exists():
            return StoredMetadata()
        return self.read_metadata()

    def _delete_keyring(self, product: Product, username: str) -> None:
        try:
            self.keyring.delete(keyring_service(product), username)
        except keyring.errors.PasswordDeleteError:
            pass
        except keyring.errors.KeyringError as exc:
            warn(
                "Warning: could not remove the token from the keyring; "
                + f"it may still be present: {exc}"
            )

    def save(
        self, product: Product, *, auth: Auth, username: str, token: str
    ) -> TokenBackend:
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AtlError(
                f"Could not create {self.config_dir}: {exc.strerror}"
            ) from exc

        existing = self._read_metadata_or_empty()
        keyring_ok = True
        try:
            self.keyring.set(keyring_service(product), username, token)
        except keyring.errors.KeyringError as exc:
            warn(
                f"Warning: keyring unavailable ({exc}); "
                + f"storing the token in {self.cred_file} (mode 600)."
            )
            keyring_ok = False

        plan = plan_storage(
            existing,
            product,
            auth=auth,
            username=username,
            token=token,
            keyring_ok=keyring_ok,
        )
        try:
            _write_secure(self.cred_file, json.dumps(plan.document))
        except OSError as exc:
            if plan.backend is TokenBackend.KEYRING:
                self._delete_keyring(product, username)
            raise AtlError(
                f"Could not write to {self.cred_file}: {exc.strerror}"
            ) from exc
        return plan.backend

    def read_metadata(self) -> StoredMetadata:
        if not self.cred_file.exists():
            raise AtlError(
                f"No credentials found. Run '{PROG} auth login jira' to set up."
            )
        try:
            raw = self.cred_file.read_text()
        except OSError as exc:
            raise AtlError(f"Could not read {self.cred_file}: {exc.strerror}") from exc
        try:
            return StoredMetadata.model_validate_json(raw)
        except ValidationError as exc:
            raise AtlError(
                f"Credentials file is malformed. Run '{PROG} auth login jira' to reset."
            ) from exc

    def _keyring_token(self, product: Product, username: str) -> str | None:
        try:
            return self.keyring.get(keyring_service(product), username)
        except keyring.errors.KeyringError as exc:
            raise AtlError(f"Could not read the token from the keyring: {exc}") from exc

    def available_products(self) -> list[Product]:
        try:
            meta = self.read_metadata()
        except AtlError:
            return []
        return [product for product in Product if product in meta.credentials]

    def load(self, product: Product) -> Credentials:
        meta = self.read_metadata()
        cred = meta.credentials.get(product)
        if cred is None:
            raise AtlError(
                f"No {product.value} credentials. Run '{PROG} auth login {product.value}'."
            )
        keyring_token = (
            None if cred.token else self._keyring_token(product, cred.username)
        )
        return Credentials(
            username=cred.username,
            token=resolve_token(product, cred, keyring_token),
            product=product,
            auth=cred.auth,
        )

    def _write_or_unlink(self, credentials: dict[Product, StoredCredential]) -> None:
        if not credentials:
            try:
                self.cred_file.unlink()
            except OSError as exc:
                raise AtlError(
                    f"Could not remove {self.cred_file}: {exc.strerror}"
                ) from exc
            return
        try:
            _write_secure(self.cred_file, json.dumps(serialize_metadata(credentials)))
        except OSError as exc:
            raise AtlError(
                f"Could not write to {self.cred_file}: {exc.strerror}"
            ) from exc

    def remove(self, product: Product) -> None:
        if not self.cred_file.exists():
            warn("No credentials found.")
            return
        meta = self.read_metadata()
        cred = meta.credentials.get(product)
        if cred is None:
            warn(f"No {product.value} credentials found.")
            return
        if stored_backend(cred) is TokenBackend.KEYRING:
            self._delete_keyring(product, cred.username)
        remaining = {p: c for p, c in meta.credentials.items() if p != product}
        self._write_or_unlink(remaining)
        success(f"{product.value.capitalize()} credentials removed.")

    def remove_all(self) -> None:
        if not self.cred_file.exists():
            warn("No credentials found.")
            return
        meta = self.read_metadata()
        for product, cred in meta.credentials.items():
            if stored_backend(cred) is TokenBackend.KEYRING:
                self._delete_keyring(product, cred.username)
        try:
            self.cred_file.unlink()
        except OSError as exc:
            raise AtlError(
                f"Could not remove {self.cred_file}: {exc.strerror}"
            ) from exc
        success("Credentials removed.")
