import hashlib

import pytest

from clipfactory.storage import LocalStorage, ObjectNotFound, ObjectStorage, StorageError


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "data")


def test_protocol(storage):
    assert isinstance(storage, ObjectStorage)


def test_put_get_roundtrip(storage, tmp_path):
    src = tmp_path / "файл с пробелом.bin"
    src.write_bytes(b"hello")
    storage.put_file("jobs/j1/source.mp4", src)
    assert storage.exists("jobs/j1/source.mp4")
    dest = storage.get_file("jobs/j1/source.mp4", tmp_path / "out.bin")
    assert dest.read_bytes() == b"hello"
    with storage.open_read("jobs/j1/source.mp4") as f:
        assert f.read() == b"hello"
    assert storage.stat("jobs/j1/source.mp4").size == 5


def test_checksum_deterministic(storage):
    storage.put_bytes("a/x.json", b'{"a":1}')
    expected = hashlib.sha256(b'{"a":1}').hexdigest()
    assert storage.checksum("a/x.json") == expected
    assert storage.checksum("a/x.json") == expected  # из кеша
    storage.put_bytes("a/x.json", b'{"a":2}')
    assert storage.checksum("a/x.json") == hashlib.sha256(b'{"a":2}').hexdigest()


def test_delete_and_missing(storage):
    storage.put_bytes("a/b", b"1")
    storage.delete("a/b")
    assert not storage.exists("a/b")
    with pytest.raises(ObjectNotFound):
        storage.checksum("a/b")
    storage.delete("a/b")  # идемпотентно


@pytest.mark.parametrize("key", ["", "/abs", "../x", "a/../b", "a//b", "a\\b", "a/./b"])
def test_invalid_keys(storage, key):
    with pytest.raises(StorageError):
        storage.exists(key)


def test_no_tmp_leftovers(storage):
    storage.put_bytes("k/v", b"x" * 1000)
    leftovers = [p for p in storage.local_path("k/v").parent.iterdir() if p.name.startswith(".tmp")]
    assert leftovers == []
