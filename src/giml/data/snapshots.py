"""Immutable, content-hashed data snapshots (spec section 7.3).

A snapshot is built in a temporary directory and renamed into place only when complete, so a
crashed sync never leaves a half-written snapshot that planning could read.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from giml import __version__
from giml.core.model import SnapshotInfo
from giml.store.result_cache import canonical_json

MANIFEST = "manifest.json"


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class SnapshotWriter:
    """Builds one snapshot. Use as a context manager; call ``commit`` to publish it."""

    def __init__(self, state_dir: Path, source: str) -> None:
        self.source = source
        self.parent = state_dir / "snapshots" / source
        self.parent.mkdir(parents=True, exist_ok=True)
        # Same directory as the final snapshot so the publishing rename is atomic; the dot prefix
        # keeps in-progress snapshots out of listings.
        self.path = Path(tempfile.mkdtemp(dir=self.parent, prefix=".tmp-"))
        self._raw: set[str] = set()
        self._committed = False

    def __enter__(self) -> SnapshotWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        if not self._committed:
            shutil.rmtree(self.path, ignore_errors=True)

    def add_raw(self, name: str) -> Path:
        """Register a raw (fetched) file; only raw files contribute to the content hash."""
        if "/" in name or name in ("", ".", "..", MANIFEST):
            raise ValueError(f"invalid snapshot file name {name!r}")
        self._raw.add(name)
        return self.path / name

    def commit(self, fetched_at: datetime.datetime, sources: list[str], stats: dict) -> SnapshotInfo:
        files = {name: sha256_file(self.path / name) for name in sorted(self._raw)}
        content_hash = hashlib.sha256(canonical_json(files).encode("utf-8")).hexdigest()
        stamp = fetched_at.astimezone(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        name = f"{stamp}-{content_hash[:12]}"
        manifest = {
            "id": f"{self.source}-{name}",
            "source": self.source,
            "fetched_at": fetched_at.astimezone(datetime.UTC).isoformat(),
            "sources": sources,
            "files": files,
            "content_hash": content_hash,
            "stats": stats,
            "giml_version": __version__,
        }
        (self.path / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", "utf-8")
        final = self.parent / name
        if final.exists():
            raise FileExistsError(f"snapshot directory already exists: {final}")
        self.path.rename(final)
        self.path = final
        self._committed = True
        return SnapshotInfo(manifest["id"], self.source, fetched_at, content_hash, final)


def read_manifest(snapshot_dir: Path) -> dict:
    return json.loads((snapshot_dir / MANIFEST).read_text(encoding="utf-8"))
