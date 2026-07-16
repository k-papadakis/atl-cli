"""Domain data types: products, auth mode, the token backend, credentials, and
paged results."""

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class TokenBackend(StrEnum):
    """Where the API token lives."""

    KEYRING = "keyring"
    FILE = "file"


class Product(StrEnum):
    """An Atlassian product a credential is scoped to."""

    JIRA = "jira"
    CONFLUENCE = "confluence"


class AuthMode(StrEnum):
    """How a credential reaches the REST API.

    ``SITE`` -- a classic token against the site root (``https://site``).
    ``GATEWAY`` -- a scoped token against
    ``https://api.atlassian.com/ex/{product}/{cloudId}``.
    """

    SITE = "site"
    GATEWAY = "gateway"


class StoredCredential(BaseModel):
    """One product's persisted credential. The token is present only in file mode."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    url: str = ""  # resolved REST-root base (the site root, or the gateway root)
    site_url: str = ""  # the human site the user typed (for --web links / status)
    username: str = ""
    token: str | None = None
    mode: AuthMode = AuthMode.SITE
    cloud_id: str | None = None


class StoredMetadata(BaseModel):
    """The on-disk credentials file: a per-product credential map."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    credentials: dict[Product, StoredCredential] = Field(default_factory=dict)


@dataclass(frozen=True)
class Credentials:
    base_url: str
    username: str
    token: str
    product: Product = Product.JIRA
    mode: AuthMode = AuthMode.SITE
    site_url: str = ""
    cloud_id: str | None = None

    @property
    def web_base(self) -> str:
        """The human site root for browser links.

        In gateway mode ``base_url`` is the API gateway (not browsable), so
        ``--web`` links use the original site; falls back to ``base_url`` (which is
        identical in site mode).
        """
        return self.site_url or self.base_url


@dataclass(frozen=True, slots=True)
class Page[T]:
    """A slice of search results, plus whether the server had more beyond it."""

    items: list[T]
    more: bool
