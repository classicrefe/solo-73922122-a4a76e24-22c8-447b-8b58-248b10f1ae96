"""Runtime configuration (environment driven)."""
from __future__ import annotations

import os
from pathlib import Path


class Settings:
    def __init__(self) -> None:
        self.data_dir = Path(os.environ.get("DATA_DIR", "data")).resolve()
        # Local source packages live here; the service only ever reads them.
        self.sources = self.data_dir / "sources"
        # SQLite holds the manifest (orders) and the index (versions) only.
        self.db_path = self.data_dir / "zipfetch.db"
        self.max_members = 32          # at most 32 observation ids per order
        self.max_total = 4 * 1024**3   # assembled ZIP must stay below 4 GiB


settings = Settings()
