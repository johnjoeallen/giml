import datetime
import hashlib
import json
import re

import pytest

from giml import __version__
from giml.data.snapshots import SnapshotWriter, read_manifest, sha256_file

FETCHED = datetime.datetime(2026, 9, 24, 10, 30, 5, tzinfo=datetime.UTC)


def write_snapshot(state_dir, payload=b"data"):
    with SnapshotWriter(state_dir, "osv") as writer:
        raw = writer.add_raw("all.zip")
        raw.write_bytes(payload)
        (writer.path / "index.sqlite").write_bytes(b"derived")
        return writer.commit(FETCHED, ["https://example.invalid/all.zip"], {"advisories": 3})


def test_commit_publishes_hashed_snapshot_with_manifest(tmp_path):
    info = write_snapshot(tmp_path)
    file_hash = hashlib.sha256(b"data").hexdigest()
    content_hash = hashlib.sha256(json.dumps({"all.zip": file_hash}, separators=(",", ":")).encode()).hexdigest()
    assert info.content_hash == content_hash
    assert info.id == f"osv-20260924T103005Z-{content_hash[:12]}"
    assert info.path == tmp_path / "snapshots" / "osv" / f"20260924T103005Z-{content_hash[:12]}"
    manifest = read_manifest(info.path)
    assert manifest["files"] == {"all.zip": file_hash}
    assert manifest["fetched_at"] == "2026-09-24T10:30:05+00:00"
    assert manifest["stats"] == {"advisories": 3}
    assert manifest["sources"] == ["https://example.invalid/all.zip"]
    assert (info.path / "index.sqlite").read_bytes() == b"derived"
    assert [p.name for p in info.path.parent.iterdir()] == [info.path.name]


def test_derived_files_do_not_change_the_content_hash(tmp_path):
    first = write_snapshot(tmp_path / "a")
    with SnapshotWriter(tmp_path / "b", "osv") as writer:
        writer.add_raw("all.zip").write_bytes(b"data")
        (writer.path / "index.sqlite").write_bytes(b"other derived bytes")
        second = writer.commit(FETCHED, [], {})
    assert first.content_hash == second.content_hash


def test_failure_before_commit_removes_temporary_directory(tmp_path):
    with pytest.raises(RuntimeError), SnapshotWriter(tmp_path, "osv") as writer:
        writer.add_raw("all.zip").write_bytes(b"partial")
        raise RuntimeError("download failed")
    assert list((tmp_path / "snapshots" / "osv").iterdir()) == []


def test_identical_snapshot_at_same_second_is_refused(tmp_path):
    write_snapshot(tmp_path)
    with pytest.raises(FileExistsError):
        write_snapshot(tmp_path)
    assert len(list((tmp_path / "snapshots" / "osv").iterdir())) == 1


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "manifest.json"])
def test_invalid_raw_names_are_rejected(tmp_path, name):
    with SnapshotWriter(tmp_path, "osv") as writer, pytest.raises(ValueError):
        writer.add_raw(name)


def test_sha256_file(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"abc")
    assert sha256_file(path) == hashlib.sha256(b"abc").hexdigest()


def test_in_progress_snapshot_is_hidden_next_to_its_final_location(tmp_path):
    with SnapshotWriter(tmp_path, "osv") as writer:
        assert writer.path.parent == tmp_path / "snapshots" / "osv"
        assert writer.path.name.startswith(".tmp-")
        writer.add_raw("all.zip").write_bytes(b"data")
        info = writer.commit(FETCHED, ["u"], {"n": 1})
        assert writer.path == info.path


def test_manifest_is_complete_and_stably_formatted(tmp_path):
    info = write_snapshot(tmp_path)
    manifest = read_manifest(info.path)
    assert manifest == {
        "id": info.id,
        "source": "osv",
        "fetched_at": "2026-09-24T10:30:05+00:00",
        "sources": ["https://example.invalid/all.zip"],
        "files": {"all.zip": hashlib.sha256(b"data").hexdigest()},
        "content_hash": info.content_hash,
        "stats": {"advisories": 3},
        "giml_version": __version__,
    }
    text = (info.path / "manifest.json").read_text(encoding="utf-8")
    assert text == json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def test_existing_snapshot_error_names_directory(tmp_path):
    info = write_snapshot(tmp_path)
    with pytest.raises(FileExistsError, match=f"snapshot directory already exists: {re.escape(str(info.path))}"):
        write_snapshot(tmp_path)


def test_sha256_of_multi_chunk_file(tmp_path):
    payload = bytes(range(256)) * 1000
    path = tmp_path / "big"
    path.write_bytes(payload)
    assert sha256_file(path) == hashlib.sha256(payload).hexdigest()
