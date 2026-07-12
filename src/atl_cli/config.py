"""Static configuration: paths and service names."""

import os
from pathlib import Path

PROG = "atl"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "atl"
CRED_FILE = CONFIG_DIR / "credentials.json"
KEYRING_SERVICE = "atl"

HTTP_TIMEOUT = 30.0

# Search fetches every result by default; `--limit` caps the total. This is the
# per-request page size the pagination loop uses under the hood.
SEARCH_PAGE_SIZE = 50
