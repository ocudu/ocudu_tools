# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Extract some members of a remote zip with HTTP range requests, without downloading the whole archive."""

import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

# Bytes to fetch from the end first: the central directory of an archive with thousands of files fits.
TAIL = 1 << 20
# A local header's extra field may differ from the central directory's; fetch this much more per member.
_SLACK = 1 << 16
# Ranges closer than this are fetched as one.
_MERGE_GAP = 1 << 20


class _Missing(Exception):
    def __init__(self, start: int):
        super().__init__(f"byte {start} not fetched")
        self.start = start


class _SparseFile(io.RawIOBase):
    """A read-only file of known size of which only some ranges are present."""

    def __init__(self, size: int):
        self.size = size
        self.pos = 0
        self.chunks: list[tuple[int, bytes]] = []

    def add(self, start: int, data: bytes) -> None:
        self.chunks.append((start, data))

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=io.SEEK_SET):
        self.pos = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence] + offset
        return self.pos

    def readinto(self, buffer):
        want = min(len(buffer), self.size - self.pos)
        done = 0
        while done < want:
            for start, data in self.chunks:
                if start <= self.pos < start + len(data):
                    n = min(want - done, start + len(data) - self.pos)
                    buffer[done:done + n] = data[self.pos - start:self.pos - start + n]
                    self.pos += n
                    done += n
                    break
            else:
                raise _Missing(self.pos)
        return done


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start - merged[-1][1] <= _MERGE_GAP:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def extract(fetch_range: Callable[[int, int], bytes], size: int, tail: tuple[int, bytes],
            select: Callable[[list[str]], list[str]], workers: int = 8) -> dict[str, bytes]:
    """Members chosen by `select` from the archive's file names, read through `fetch_range(start, end_exclusive)`.
    `tail` is (start, data) of the archive's last bytes, already fetched."""
    sparse = _SparseFile(size)
    sparse.add(*tail)
    while True:
        try:
            zf = zipfile.ZipFile(sparse)
            break
        except _Missing as missing:
            # Central directory larger than the tail: fetch the rest of it.
            first = min(start for start, _ in sparse.chunks)
            sparse.add(missing.start, fetch_range(missing.start, first))

    names = select([i.filename for i in zf.infolist() if not i.is_dir()])
    infos = [zf.getinfo(n) for n in names]
    ranges = _merge([
        (i.header_offset, min(size, i.header_offset + 30 + len(i.filename.encode()) + len(i.extra)
                          + i.compress_size + _SLACK))
        for i in infos
    ])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (start, _), data in zip(ranges, pool.map(lambda r: fetch_range(*r), ranges)):
            sparse.add(start, data)
    return {n: zf.read(n) for n in names}
