import gzip
import hashlib
import io
import tarfile

import pytest

from dradar.taskpacks import MARKER, TaskPackError, ensure_benchmark_task_pack


def _archive(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as tar:
            for name, content in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(content))
    return output.getvalue()


class FakeClient:
    def __init__(self, payload: bytes, digest: str | None = None):
        self.payload = payload
        self.digest = digest or hashlib.sha256(payload).hexdigest()
        self.downloads = 0

    def benchmarks(self):
        return {"benchmarks": [{
            "id": "pompeii-adjacency",
            "title": "Pompeii",
            "task_bundle": {
                "url": "/api/v1/benchmark-bundles/pompeii-adjacency",
                "sha256": self.digest,
                "bytes": len(self.payload),
            },
        }]}

    def download(self, _url, destination):
        self.downloads += 1
        destination.write_bytes(self.payload)
        return self.digest


def test_task_pack_install_is_atomic_verified_and_idempotent(tmp_path):
    payload = _archive({
        "pompeii-adjacency-rp-002/task.toml": b"[task]\n",
        "pompeii-adjacency-rp-002/instruction.md": b"recover adjacency\n",
    })
    client = FakeClient(payload)
    root = tmp_path / "benchmarks" / "pompeii-adjacency" / "tasks"

    assert ensure_benchmark_task_pack(client, "pompeii-adjacency", root) is True
    assert (root / "pompeii-adjacency-rp-002" / "task.toml").is_file()
    assert (root / MARKER).is_file()
    assert ensure_benchmark_task_pack(client, "pompeii-adjacency", root) is False
    assert client.downloads == 1


def test_task_pack_checksum_upgrade_atomically_replaces_managed_pack(tmp_path):
    old = FakeClient(_archive({"task/old.txt": b"old"}))
    root = tmp_path / "tasks"
    assert ensure_benchmark_task_pack(old, "pompeii-adjacency", root) is True

    new = FakeClient(_archive({"task/new.txt": b"new"}))
    assert ensure_benchmark_task_pack(new, "pompeii-adjacency", root) is True

    assert not (root / "task" / "old.txt").exists()
    assert (root / "task" / "new.txt").read_bytes() == b"new"
    assert new.downloads == 1


def test_task_pack_upgrade_checksum_failure_preserves_installed_pack(tmp_path):
    old = FakeClient(_archive({"task/old.txt": b"old"}))
    root = tmp_path / "tasks"
    assert ensure_benchmark_task_pack(old, "pompeii-adjacency", root) is True

    bad = FakeClient(_archive({"task/new.txt": b"new"}), digest="0" * 64)
    with pytest.raises(TaskPackError, match="checksum mismatch"):
        ensure_benchmark_task_pack(bad, "pompeii-adjacency", root)

    assert (root / "task" / "old.txt").read_bytes() == b"old"
    assert not (root / "task" / "new.txt").exists()


def test_task_pack_never_replaces_unmanaged_existing_directory(tmp_path):
    root = tmp_path / "tasks"
    root.mkdir()
    (root / "user-file").write_text("keep")
    client = FakeClient(_archive({"task/new.txt": b"new"}))

    with pytest.raises(TaskPackError, match="already exists"):
        ensure_benchmark_task_pack(client, "pompeii-adjacency", root)

    assert (root / "user-file").read_text() == "keep"
    assert client.downloads == 0


def test_task_pack_checksum_mismatch_leaves_no_partial_install(tmp_path):
    payload = _archive({"task/task.toml": b"x"})
    client = FakeClient(payload, digest="0" * 64)
    root = tmp_path / "tasks"
    with pytest.raises(TaskPackError, match="checksum mismatch"):
        ensure_benchmark_task_pack(client, "pompeii-adjacency", root)
    assert not root.exists()


def test_task_pack_rejects_path_traversal(tmp_path):
    payload = _archive({"../escape": b"no"})
    client = FakeClient(payload)
    root = tmp_path / "tasks"
    with pytest.raises(TaskPackError, match="unsafe path"):
        ensure_benchmark_task_pack(client, "pompeii-adjacency", root)
    assert not (tmp_path / "escape").exists()
    assert not root.exists()
