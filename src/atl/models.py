"""Domain data types: the token backend, credentials, and paged results."""

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class TokenBackend(StrEnum):
    """Where the API token lives."""

    KEYRING = "keyring"
    FILE = "file"


class StoredMetadata(BaseModel):
    """The on-disk credentials file. The token is only present in file mode."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    url: str = ""
    username: str = ""
    token: str | None = None


@dataclass(frozen=True)
class Credentials:
    base_url: str
    username: str
    token: str


@dataclass(frozen=True, slots=True)
class Page[T]:
    """A slice of search results plus whether the server had more beyond it.

    The aggregated, domain-facing outcome of a paginated search: `more` is a
    plain fact carried as data, not re-derived from a wire cursor downstream.
    """

    items: list[T]
    more: bool
