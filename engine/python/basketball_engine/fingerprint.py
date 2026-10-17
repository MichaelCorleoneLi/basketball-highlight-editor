"""Cheap content fingerprint for linked source videos.

原视频经常放在 NAS 上，体积十几 GB。早期实现每次校验都完整读一遍算 SHA-256，
导致导出、封面抽帧等请求要等几分钟甚至超时。这里改为只抽样读取文件头、中、尾
各一块，再加上文件大小，足以发现“换了文件”或“文件被改写”。

旧库里存的是完整哈希（``<size>:<sha256>``），无法在不全量读取的情况下比较，
这类指纹只按大小和修改时间判断。
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

SAMPLED_PREFIX = "sample-v1:"
SAMPLE_BYTES = 4 * 1024 * 1024

_cache: dict[tuple[str, int, int], str] = {}
_cache_lock = threading.Lock()


def sampled_fingerprint(path: str | Path) -> str:
    """Return ``sample-v1:<size>:<sha256 of head/middle/tail samples>``.

    Results are memoised per (path, size, mtime) so repeated validations of the
    same unchanged file do not touch the disk again.
    """
    source = Path(path)
    stat = source.stat()
    key = (str(source), stat.st_size, stat.st_mtime_ns)
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached

    size = stat.st_size
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        if size <= SAMPLE_BYTES * 3:
            digest.update(stream.read())
        else:
            for offset in (0, (size - SAMPLE_BYTES) // 2, size - SAMPLE_BYTES):
                stream.seek(offset)
                digest.update(stream.read(SAMPLE_BYTES))
    value = f"{SAMPLED_PREFIX}{size}:{digest.hexdigest()}"
    with _cache_lock:
        _cache[key] = value
    return value


def is_sampled(fingerprint: object) -> bool:
    return isinstance(fingerprint, str) and fingerprint.startswith(SAMPLED_PREFIX)
