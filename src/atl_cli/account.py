"""Credential persistence: metadata in a config file, the token in the keyring.

Credentials are stored *per product* (Jira, Confluence): a scoped API token is
locked to a single product, so each gets its own entry with its own base URL and
mode. Non-secret metadata lives in a mode-600 JSON file; the token is kept in the
OS keyring (keyed per product), falling back to that file when no keyring backend
is usable.

The storage *policy* (key scheme, backend choice, merge, rollback, token
resolution) lives in the pure functions below; the rest is the thin I/O shell.
"""

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import keyring
import keyring.errors
from pydantic import JsonValue, ValidationError

from atl_cli.config import CONFIG_DIR, CRED_FILE, KEYRING_SERVICE, PROG
from atl_cli.console import success, warn
from atl_cli.errors import AtlError
from atl_cli.models import (
    AuthMode,
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
    """What to persist for a credential, and how to recover from a failed write."""

    document: dict[str, JsonValue]  # the credentials document to serialize to the file
    backend: TokenBackend
    # The token was placed in the keyring, so a failed metadata write must delete
    # it again -- otherwise the secret is orphaned with no file to locate it by.
    rollback_keyring: bool


def plan_storage(
    existing: StoredMetadata,
    product: Product,
    *,
    base_url: str,
    site_url: str,
    username: str,
    token: str,
    mode: AuthMode,
    cloud_id: str | None,
    keyring_ok: bool,
) -> StoragePlan:
    """Merge a new/updated per-product credential into the existing document.

    With a working keyring the token stays out of the file (keyring backend);
    otherwise it falls into the mode-600 file. Any credential for the *other*
    product is preserved.
    """
    cred = StoredCredential(
        url=base_url,
        site_url=site_url,
        username=username,
        token=None if keyring_ok else token,
        mode=mode,
        cloud_id=cloud_id,
    )
    credentials = dict(existing.credentials)
    credentials[product] = cred
    document = serialize_metadata(credentials)
    if keyring_ok:
        return StoragePlan(document, TokenBackend.KEYRING, rollback_keyring=True)
    return StoragePlan(document, TokenBackend.FILE, rollback_keyring=False)


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


def _read_metadata_or_empty() -> StoredMetadata:
    """Read the existing document for merging, tolerating an absent/malformed
    file (a login is a deliberate reset)."""
    try:
        return read_metadata()
    except AtlError:
        return StoredMetadata()


def _delete_keyring(product: Product, username: str) -> None:
    """Best-effort keyring deletion: a missing entry is fine; a real backend
    error is surfaced but not fatal.

    Note: some backends (macOS among them) wrap *every* delete failure as a
    ``PasswordDeleteError``, so the branch below can't tell a missing entry from
    a genuine failure and swallows both -- which also means the ``KeyringError``
    warning branch never fires for a failed delete. That's acceptable here:
    logout is best-effort and the file-side removal proceeds regardless.
    """
    try:
        keyring.delete_password(keyring_service(product), username)
    except keyring.errors.PasswordDeleteError:
        pass  # no such entry, or a best-effort delete that failed -- either way, move on
    except keyring.errors.KeyringError as exc:
        warn(
            "Warning: could not remove the token from the keyring; "
            + f"it may still be present: {exc}"
        )


def save_credentials(
    product: Product,
    *,
    base_url: str,
    site_url: str,
    username: str,
    token: str,
    mode: AuthMode,
    cloud_id: str | None,
) -> TokenBackend:
    """Persist a product's metadata to the config file and its token to the
    keyring, preserving any credential already stored for the other product.

    Falls back to a mode-600 file if no keyring backend is usable.
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AtlError(f"Could not create {CONFIG_DIR}: {exc.strerror}") from exc

    existing = _read_metadata_or_empty()

    keyring_ok = True
    try:
        keyring.set_password(keyring_service(product), username, token)
    except keyring.errors.KeyringError as exc:
        warn(
            f"Warning: keyring unavailable ({exc}); "
            + f"storing the token in {CRED_FILE} (mode 600)."
        )
        keyring_ok = False

    plan = plan_storage(
        existing,
        product,
        base_url=base_url,
        site_url=site_url,
        username=username,
        token=token,
        mode=mode,
        cloud_id=cloud_id,
        keyring_ok=keyring_ok,
    )
    try:
        _write_secure(CRED_FILE, json.dumps(plan.document))
    except OSError as exc:
        if plan.rollback_keyring:
            _delete_keyring(product, username)
        raise AtlError(f"Could not write to {CRED_FILE}: {exc.strerror}") from exc
    return plan.backend


def read_metadata() -> StoredMetadata:
    if not CRED_FILE.exists():
        raise AtlError(f"No credentials found. Run '{PROG} auth login jira' to set up.")
    try:
        raw = CRED_FILE.read_text()
    except OSError as exc:
        raise AtlError(f"Could not read {CRED_FILE}: {exc.strerror}") from exc
    try:
        return StoredMetadata.model_validate_json(raw)
    except ValidationError as exc:
        raise AtlError(
            f"Credentials file is malformed. Run '{PROG} auth login jira' to reset."
        ) from exc


def _keyring_token(product: Product, username: str) -> str | None:
    """Fetch a product's token from the keyring."""
    try:
        return keyring.get_password(keyring_service(product), username)
    except keyring.errors.KeyringError as exc:
        raise AtlError(f"Could not read the token from the keyring: {exc}") from exc


def available_products() -> list[Product]:
    """Which products currently have a stored credential (empty if none)."""
    try:
        meta = read_metadata()
    except AtlError:
        return []
    return [product for product in Product if product in meta.credentials]


def load_credentials(product: Product) -> Credentials:
    meta = read_metadata()
    cred = meta.credentials.get(product)
    if cred is None:
        raise AtlError(
            f"No {product.value} credentials. Run '{PROG} auth login {product.value}'."
        )
    keyring_token = None if cred.token else _keyring_token(product, cred.username)
    return Credentials(
        base_url=cred.url,
        username=cred.username,
        token=resolve_token(product, cred, keyring_token),
        product=product,
        mode=cred.mode,
        site_url=cred.site_url or cred.url,
        cloud_id=cred.cloud_id,
    )


def _write_or_unlink(credentials: dict[Product, StoredCredential]) -> None:
    """Rewrite the file with the given credentials, or unlink it when empty."""
    if not credentials:
        try:
            CRED_FILE.unlink()
        except OSError as exc:
            raise AtlError(f"Could not remove {CRED_FILE}: {exc.strerror}") from exc
        return
    try:
        _write_secure(CRED_FILE, json.dumps(serialize_metadata(credentials)))
    except OSError as exc:
        raise AtlError(f"Could not write to {CRED_FILE}: {exc.strerror}") from exc


def remove_credentials(product: Product) -> None:
    """Remove one product's credential, leaving the other intact."""
    if not CRED_FILE.exists():
        warn("No credentials found.")
        return
    meta = read_metadata()
    cred = meta.credentials.get(product)
    if cred is None:
        warn(f"No {product.value} credentials found.")
        return
    if stored_backend(cred) is TokenBackend.KEYRING:
        _delete_keyring(product, cred.username)
    remaining = {p: c for p, c in meta.credentials.items() if p != product}
    _write_or_unlink(remaining)
    success(f"{product.value.capitalize()} credentials removed.")


def remove_all_credentials() -> None:
    """Remove every stored credential and delete the file."""
    if not CRED_FILE.exists():
        warn("No credentials found.")
        return
    meta = read_metadata()
    for product, cred in meta.credentials.items():
        if stored_backend(cred) is TokenBackend.KEYRING:
            _delete_keyring(product, cred.username)
    try:
        CRED_FILE.unlink()
    except OSError as exc:
        raise AtlError(f"Could not remove {CRED_FILE}: {exc.strerror}") from exc
    success("Credentials removed.")
