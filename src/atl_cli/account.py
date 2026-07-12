"""Credential persistence: metadata in a config file, the token in the keyring.

Non-secret metadata (site URL, username) lives in a mode-600 JSON file. The API
token is kept in the OS keyring; if no keyring backend is usable it falls back
to the same file.

The storage *policy* -- which backend to use, what the file should contain,
whether a failed write must roll the keyring back, and how to resolve a token on
load -- lives in the pure functions below (`plan_storage`, `resolve_token`,
`stored_backend`), tested with plain data. The remaining functions are the thin
I/O shell that runs the plan.
"""

import json
import sys
from dataclasses import dataclass

import keyring
import keyring.errors
from pydantic import ValidationError

from atl_cli.config import CONFIG_DIR, CRED_FILE, KEYRING_SERVICE, PROG
from atl_cli.errors import AtlError
from atl_cli.models import Credentials, StoredMetadata, TokenBackend


# --------------------------------------------------------------------------- #
# Pure storage policy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class StoragePlan:
    """What to persist for a credential, and how to recover from a failed write."""

    metadata: dict[str, str]
    backend: TokenBackend
    # The token was placed in the keyring, so a failed metadata write must delete
    # it again -- otherwise the secret is orphaned with no file to locate it by.
    rollback_keyring: bool


def plan_storage(
    base_url: str, username: str, token: str, *, keyring_ok: bool
) -> StoragePlan:
    """Decide where the token lives and what the metadata file should contain.

    With a working keyring the token stays out of the file (keyring backend);
    otherwise it falls back into the mode-600 file.
    """
    metadata = {"url": base_url, "username": username}
    if keyring_ok:
        return StoragePlan(metadata, TokenBackend.KEYRING, rollback_keyring=True)
    return StoragePlan(
        metadata | {"token": token}, TokenBackend.FILE, rollback_keyring=False
    )


def resolve_token(meta: StoredMetadata, keyring_token: str | None) -> str:
    """Pick the effective token: a file token wins, else the keyring's."""
    token = meta.token or keyring_token or ""
    if not token:
        raise AtlError(f"Token not found. Run '{PROG} auth login'.")
    return token


def stored_backend(meta: StoredMetadata) -> TokenBackend:
    return TokenBackend.FILE if meta.token else TokenBackend.KEYRING


# --------------------------------------------------------------------------- #
# I/O shell
# --------------------------------------------------------------------------- #
def save_credentials(base_url: str, username: str, token: str) -> TokenBackend:
    """Persist metadata to the config file and the token to the keyring.

    Falls back to a mode-600 file if no keyring backend is usable.
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AtlError(f"Could not create {CONFIG_DIR}: {exc.strerror}") from exc

    keyring_ok = True
    try:
        keyring.set_password(KEYRING_SERVICE, username, token)
    except keyring.errors.KeyringError as exc:
        print(
            f"Warning: keyring unavailable ({exc}); "
            + f"storing the token in {CRED_FILE} (mode 600).",
            file=sys.stderr,
        )
        keyring_ok = False

    plan = plan_storage(base_url, username, token, keyring_ok=keyring_ok)
    try:
        _ = CRED_FILE.write_text(json.dumps(plan.metadata))
        CRED_FILE.chmod(0o600)
    except OSError as exc:
        if plan.rollback_keyring:
            try:
                keyring.delete_password(KEYRING_SERVICE, username)
            except keyring.errors.KeyringError as rollback_exc:
                # The metadata write failed *and* we can't undo the keyring set,
                # so the token may be orphaned -- say so rather than hide it.
                print(
                    "Warning: could not roll back the keyring token; "
                    + f"it may remain stored: {rollback_exc}",
                    file=sys.stderr,
                )
        raise AtlError(f"Could not write to {CRED_FILE}: {exc.strerror}") from exc
    return plan.backend


def read_metadata() -> StoredMetadata:
    if not CRED_FILE.exists():
        raise AtlError(f"No credentials found. Run '{PROG} auth login' to set up.")
    try:
        raw = CRED_FILE.read_text()
    except OSError as exc:
        raise AtlError(f"Could not read {CRED_FILE}: {exc.strerror}") from exc
    try:
        return StoredMetadata.model_validate_json(raw)
    except ValidationError as exc:
        raise AtlError(
            f"Credentials file is malformed. Run '{PROG} auth login' to reset."
        ) from exc


def load_credentials() -> Credentials:
    meta = read_metadata()
    keyring_token: str | None = None
    if not meta.token:
        try:
            keyring_token = keyring.get_password(KEYRING_SERVICE, meta.username)
        except keyring.errors.KeyringError as exc:
            raise AtlError(f"Could not read the token from the keyring: {exc}") from exc
    return Credentials(meta.url, meta.username, resolve_token(meta, keyring_token))


def remove_credentials() -> None:
    if not CRED_FILE.exists():
        print("No credentials found.", file=sys.stderr)
        return
    meta = read_metadata()
    if stored_backend(meta) is TokenBackend.KEYRING:
        try:
            keyring.delete_password(KEYRING_SERVICE, meta.username)
        except keyring.errors.PasswordDeleteError:
            pass  # nothing was stored; nothing to remove
        except keyring.errors.KeyringError as exc:
            print(
                "Warning: could not remove the token from the keyring; "
                + f"it may still be present: {exc}",
                file=sys.stderr,
            )
    try:
        CRED_FILE.unlink()
    except OSError as exc:
        raise AtlError(f"Could not remove {CRED_FILE}: {exc.strerror}") from exc
    print("Credentials removed.", file=sys.stderr)
