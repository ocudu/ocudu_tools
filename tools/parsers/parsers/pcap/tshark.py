# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Running tshark on OCUDU pcaps, with staging of pcaps where tshark can read them and caching of field extractions."""

from __future__ import annotations

import glob
import hashlib
import os
import shutil
import subprocess
import tempfile
import uuid
import warnings
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path

# Benign tshark startup warnings about the user's wireshark configuration.
_NOISE_PATTERNS = ("User DLTs Table", "/.config/wireshark/")
# Warnings whose path may follow on the next line.
_NOISE_INTROS = (
    "Can't open your preferences",
    "Could not open your disabled protocols",
    "Could not open your enabled protocols",
    "Could not open your heuristic dissectors",
)
# Error of a pcap whose last record is incomplete, e.g. of a process killed while writing it.
_CUT_SHORT = "cut short in the middle of a packet"
# OCUDU mac.pcap and rlc.pcap wrap NR PDUs in UDP-framed Upper-PDUs, dissected only with these heuristics.
_READ_ARGS = ("--enable-heuristic", "mac_nr_udp", "--enable-heuristic", "rlc_nr_udp")
# Directories the AppArmor profile of tshark on Ubuntu lets it read, besides a few system paths.
_READABLE_PREFIXES = ("/tmp/",)
# First line of "tshark -v" of each tshark executable run by this process.
_versions: dict[str, str] = {}


class TsharkError(RuntimeError):
    """tshark is missing or failed."""


def default_work_dir() -> Path:
    """Directory of staged pcaps and cached extractions when none is given, private to the user."""
    return Path(tempfile.gettempdir()) / f"ocudu-parsers-{os.getuid()}"


def _private_dir(path: Path, tighten: bool) -> Path:
    """path, created private to the user, or a new private temporary directory when another user owns it.

    tighten removes the access of others to an existing path.
    """
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = path.lstat()
    except OSError:
        st = None
    # Another user could plant or read staged pcaps and cached extractions in a directory of theirs.
    if st is None or st.st_uid != os.getuid() or not path.is_dir() or path.is_symlink():
        fallback = Path(tempfile.mkdtemp(prefix=f"{path.name}-"))
        warnings.warn(f"{path} is not a directory of this user; using {fallback}")
        return fallback
    if tighten and st.st_mode & 0o077:
        path.chmod(0o700)
    return path


def _executable() -> str:
    """Path of tshark; raises TsharkError when it is not on PATH."""
    tshark = shutil.which("tshark")
    if not tshark:
        raise TsharkError("tshark not found on PATH")
    return tshark


def split_fields(line: str, expected: int, sep: str = "\t") -> list[str]:
    """Columns of a tshark "-T fields" line, padded with "" or truncated to the expected count."""
    parts = line.split(sep)
    if len(parts) < expected:
        parts.extend([""] * (expected - len(parts)))
    return parts[:expected]


class Tshark:
    """Runs tshark, staging pcaps and caching field extractions in a work directory.

    on_cache, if given, is called with the cache file and whether it was a hit, for each cached extraction. Without
    cache, extractions always run tshark.
    """

    def __init__(
        self,
        work_dir: str | os.PathLike[str] | None = None,
        on_cache: Callable[[Path, bool], None] | None = None,
        cache: bool = True,
    ):
        self._given_work_dir = Path(work_dir) if work_dir is not None else None
        self._work_dir: Path | None = None
        self.on_cache = on_cache
        self.cache = cache
        # Whether tshark reads each pcap in place, by resolved path.
        self._readable: dict[Path, bool] = {}
        # Known fields of each list of fields checked, which depend on the tshark build only.
        self._valid: dict[tuple[str, ...], list[str]] = {}

    @property
    def work_dir(self) -> Path:
        """Directory of staged pcaps and cached extractions, the one given or default_work_dir(), see _private_dir()."""
        if self._work_dir is None:
            given = self._given_work_dir is not None
            self._work_dir = _private_dir(self._given_work_dir if given else default_work_dir(), tighten=not given)
        return self._work_dir

    def run(self, args: Sequence[str], *, check: bool = True) -> list[str]:
        """Runs tshark with args, returning its non-empty stdout lines. Raises TsharkError if it fails and check, except
        for a pcap whose last record is incomplete, whose complete records are returned with a warning.
        """
        return self._run(args, check)[0]

    def _run(self, args: Sequence[str], check: bool = True) -> tuple[list[str], bool]:
        """The non-empty stdout lines of tshark run with args, and whether the pcap was read whole, see run()."""
        tshark = _executable()
        extra = list(_READ_ARGS) if "-r" in args else []
        proc = subprocess.run([tshark, *extra, *args], check=False, capture_output=True, text=True)
        lines = [line for line in proc.stdout.splitlines() if line]
        if check and proc.returncode != 0:
            err = _filter_stderr(proc.stderr) or proc.stderr.strip()
            # The exit code of a cut-short pcap varies across tshark builds, e.g. 2 or 14.
            if _CUT_SHORT in err:
                warnings.warn(f"{err}\nOnly its complete records are read.")
                return lines, False
            raise TsharkError(f"tshark exited {proc.returncode}: {err}\nargs: {' '.join(args)}")
        return lines, True

    def version(self) -> str:
        """First line of "tshark -v", run once per process and tshark executable."""
        tshark = _executable()
        if tshark not in _versions:
            lines = self.run(["-v"])
            _versions[tshark] = lines[0] if lines else "unknown"
        return _versions[tshark]

    def stage(self, pcap: str | os.PathLike[str]) -> Path:
        """Returns a path to the pcap that tshark can read: the pcap itself when tshark reads it, else a link or copy in
        the work directory, of the pcap's current size and mtime.
        """
        src = Path(pcap).resolve()
        # Without tshark, staging would copy the pcap, maybe of gigabytes, for nothing.
        _executable()
        if str(src).startswith(_READABLE_PREFIXES) or self._reads_in_place(src):
            return src
        st = src.stat()
        source = hashlib.sha256(str(src).encode()).hexdigest()[:16]
        version = hashlib.sha256(f"{st.st_size}\0{st.st_mtime_ns}".encode()).hexdigest()[:8]
        stage_dir = self.work_dir / "pcap-stage"
        stage_dir.mkdir(parents=True, exist_ok=True)
        staged = stage_dir / f"{source}-{version}-{src.name}"
        if staged.exists():
            return staged
        # A staged pcap counts as complete once it exists, so it appears only once linked or copied whole.
        tmp = _temp_path(staged)
        try:
            try:
                os.link(src, tmp)
            except OSError:
                shutil.copy2(src, tmp)
            tmp.replace(staged)
        finally:
            tmp.unlink(missing_ok=True)
        # Earlier versions of the pcap, which a copy would otherwise keep in the work directory, e.g. in memory.
        for old in stage_dir.glob(f"{source}-*-{glob.escape(src.name)}"):
            if old != staged:
                old.unlink(missing_ok=True)
        return staged

    def _reads_in_place(self, src: Path) -> bool:
        if src not in self._readable:
            try:
                self.run(["-r", str(src), "-c", "1", "-T", "fields", "-e", "frame.number"])
                self._readable[src] = True
            except TsharkError:
                self._readable[src] = False
        return self._readable[src]

    def valid_fields(self, pcap: str | os.PathLike[str], fields: Iterable[str]) -> list[str]:
        """The fields that this tshark build knows, in order, checked by reading one frame of pcap once per list."""
        key = tuple(fields)
        if key not in self._valid:
            self._valid[key] = self._check_fields(pcap, list(key))
        return list(self._valid[key])

    def _check_fields(self, pcap: str | os.PathLike[str], fields: list[str]) -> list[str]:
        staged = self.stage(pcap)
        remaining = list(fields)
        # tshark rejects a query with any unknown field, naming the unknown ones, and names vary across versions.
        for _ in range(len(remaining) + 1):
            if not remaining:
                return remaining
            args = ["-r", str(staged), "-T", "fields", "-c", "1"]
            for f in remaining:
                args += ["-e", f]
            try:
                self.run(args)
                return remaining
            except TsharkError as e:
                bad = {line.strip() for line in str(e).splitlines()} & set(remaining)
                if not bad:
                    raise
                remaining = [f for f in remaining if f not in bad]
        return remaining

    def cache_path(self, pcap: str | os.PathLike[str], tag: str) -> Path:
        """Cache file of an extraction from a pcap, identified by tag, for the pcap's current size and mtime and the
        tshark version. Raises TsharkError when tshark is missing.
        """
        src = Path(pcap).resolve()
        try:
            st = src.stat()
            version = f"{st.st_size}\0{st.st_mtime_ns}"
        except OSError:
            version = ""
        # Dissectors, and so extractions, change across tshark builds.
        digest = hashlib.sha256(f"{src}\0{version}\0{self.version()}\0{tag}".encode()).hexdigest()[:16]
        return self.work_dir / f"pcap-cache-{digest}.tsv"

    def iter_fields(
        self,
        pcap: str | os.PathLike[str],
        fields: Iterable[str],
        *,
        display_filter: str | None = None,
        tag: str | None = None,
        force: bool = False,
    ) -> Iterator[list[str]]:
        """Yields the values of fields for each frame matching display_filter, as strings, "" when absent.

        Extractions are cached under tag, by default the fields and filter. force runs tshark even on a cache hit.
        """
        fields_list = list(fields)
        tag_str = tag or "+".join(fields_list) + ("|" + display_filter if display_filter else "")
        cf = self.cache_path(pcap, tag_str) if self.cache else None
        hit = cf is not None and cf.exists() and not force
        if cf is not None and self.on_cache is not None:
            self.on_cache(cf, hit)
        if hit:
            with cf.open() as fh:
                for line in fh:
                    line = line.rstrip("\n")
                    if line:
                        yield split_fields(line, len(fields_list))
            return
        args = ["-r", str(self.stage(pcap)), "-T", "fields", "-E", "separator=\t"]
        for f in fields_list:
            args += ["-e", f]
        if display_filter:
            args += ["-Y", display_filter]
        lines, whole = self._run(args)
        # A pcap cut short is read again each time, so that each read warns.
        if cf is not None and whole:
            cf.parent.mkdir(parents=True, exist_ok=True)
            # A cache file counts as complete once it exists, so it appears only once written.
            tmp = _temp_path(cf)
            try:
                tmp.write_text("\n".join(lines) + ("\n" if lines else ""))
                tmp.replace(cf)
            finally:
                tmp.unlink(missing_ok=True)
        for line in lines:
            yield split_fields(line, len(fields_list))


def _temp_path(path: Path) -> Path:
    """A path to write a file at before renaming it to path, unique across processes and threads."""
    return path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")


def _filter_stderr(stderr: str) -> str:
    """tshark stderr without the benign wireshark configuration warnings."""
    keep: list[str] = []
    skip_next = False
    for line in stderr.splitlines():
        if skip_next:
            skip_next = False
            continue
        if any(p in line for p in _NOISE_INTROS):
            # The path is quoted, on this line or the next.
            skip_next = '"' not in line
            continue
        if any(p in line for p in _NOISE_PATTERNS):
            continue
        keep.append(line)
    return "\n".join(keep).strip()
