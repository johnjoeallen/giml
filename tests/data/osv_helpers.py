"""Builders for OSV bulk-dump fixtures."""

import json
import zipfile
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "osv"


def logback_advisories() -> list[dict]:
    data = json.loads((FIXTURES / "logback-core-advisories.json").read_text(encoding="utf-8"))
    return data["advisories"]


def write_osv_zip(path: Path, advisories: list[dict], extra: dict[str, bytes] | None = None) -> Path:
    """Write advisories as an OSV bulk zip (one ``<id>.json`` per advisory), byte-for-byte stable."""
    entries = {f"{a['id']}.json": json.dumps(a, sort_keys=True).encode() for a in advisories}
    entries.update(extra or {})
    with zipfile.ZipFile(path, "w") as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            archive.writestr(info, entries[name])
    return path
