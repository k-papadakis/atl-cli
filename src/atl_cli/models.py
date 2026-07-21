"""Domain data types: products, auth variants, the token backend, credentials,
and paged results."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

# Scoped API tokens reach the REST API through this gateway rather than the site
# URL (which returns 401 for them); classic tokens keep using the site URL.
GATEWAY_HOST = "api.atlassian.com"


class TokenBackend(StrEnum):
    """Where the API token lives."""

    KEYRING = "keyring"
    FILE = "file"


class Product(StrEnum):
    """An Atlassian product a credential is scoped to."""

    JIRA = "jira"
    CONFLUENCE = "confluence"


def build_gateway_base(product: Product, cloud_id: str) -> str:
    """The scoped-token REST root for a product on a site's cloud instance."""
    return f"https://{GATEWAY_HOST}/ex/{product.value}/{cloud_id}"


class SiteAuth(BaseModel):
    """A classic token against the site root (``https://site``)."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    kind: Literal["site"] = "site"
    site_url: str = Field(min_length=1)


class GatewayAuth(BaseModel):
    """A scoped token against ``https://api.atlassian.com/ex/{product}/{cloudId}``."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    kind: Literal["gateway"] = "gateway"
    site_url: str = Field(min_length=1)  # the human site, for --web links / status
    cloud_id: str = Field(min_length=1)


# The tagged union: ``kind`` selects the variant, so an invalid combination
# (gateway without a cloud id, site with a stray cloud id) can't be represented.
Auth = Annotated[SiteAuth | GatewayAuth, Field(discriminator="kind")]


class StoredCredential(BaseModel):
    """One product's persisted credential. The token is present only in file mode."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    username: str = Field(min_length=1)
    token: str | None = Field(default=None, min_length=1)
    auth: Auth


class StoredMetadata(BaseModel):
    """The on-disk credentials file: a per-product credential map."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    credentials: dict[Product, StoredCredential] = Field(default_factory=dict)


@dataclass(frozen=True)
class Credentials:
    username: str
    token: str
    product: Product
    auth: Auth

    @property
    def base_url(self) -> str:
        """The REST-root origin: the site root in site mode, the gateway in gateway mode."""
        if isinstance(self.auth, SiteAuth):
            return self.auth.site_url
        return build_gateway_base(self.product, self.auth.cloud_id)

    @property
    def web_base(self) -> str:
        """The human site root for browser links (both variants always carry it)."""
        return self.auth.site_url


@dataclass(frozen=True, slots=True)
class Page[T]:
    """A slice of search results, plus whether the server had more beyond it."""

    items: list[T]
    more: bool
