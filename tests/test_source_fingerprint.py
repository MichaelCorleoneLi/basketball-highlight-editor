from pathlib import Path

import pytest

from basketball_engine import fingerprint
from basketball_engine.fingerprint import SAMPLE_BYTES, is_sampled, sampled_fingerprint
from basketball_engine.protocol import ProtocolError
from basketball_engine.service import EngineService


def _video_record(path: Path, fingerprint_value: str) -> dict:
    stat = path.stat()
    return {
        "source_path": str(path),
        "source_size_bytes": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "source_fingerprint": fingerprint_value,
    }


def test_sampled_fingerprint_reads_only_head_middle_tail(tmp_path: Path, monkeypatch):
    source = tmp_path / "big.mp4"
    source.write_bytes(b"\0" * (SAMPLE_BYTES * 10))
    reads: list[int] = []
    real_open = Path.open

    def tracking_open(self, *args, **kwargs):
        stream = real_open(self, *args, **kwargs)
        real_read = stream.read

        class Wrapper:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                stream.close()

            def seek(self_inner, offset):
                return stream.seek(offset)

            def read(self_inner, size=-1):
                data = real_read(size)
                reads.append(len(data))
                return data

        return Wrapper()

    monkeypatch.setattr(Path, "open", tracking_open)
    value = sampled_fingerprint(source)

    assert is_sampled(value)
    assert sum(reads) == SAMPLE_BYTES * 3


def test_sampled_fingerprint_detects_rewritten_content(tmp_path: Path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"a" * 1024)
    stored = sampled_fingerprint(source)
    record = _video_record(source, stored)
    assert EngineService._validated_original_source(record) == source

    source.write_bytes(b"b" * 1024)
    record["source_mtime_ns"] = source.stat().st_mtime_ns
    with pytest.raises(ProtocolError) as exc:
        EngineService._validated_original_source(record)
    assert exc.value.code == "VIDEO_SOURCE_CHANGED"


def test_legacy_full_hash_is_not_recomputed(tmp_path: Path, monkeypatch):
    source = tmp_path / "legacy.mp4"
    source.write_bytes(b"legacy")
    record = _video_record(source, "6:" + "0" * 64)

    def fail(*_args, **_kwargs):
        raise AssertionError("validation must not read the whole source video")

    monkeypatch.setattr(fingerprint, "sampled_fingerprint", fail)
    monkeypatch.setattr(EngineService, "_source_fingerprint", staticmethod(fail))

    assert EngineService._validated_original_source(record) == source
