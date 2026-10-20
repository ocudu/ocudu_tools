# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Command line entry point: parse sources, serve them and open the browser."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys
import tempfile
import threading
import webbrowser
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import parallel
from .registry import SourceRegistry, error_text
from .sources.base import SourceType
from .sources.log_metrics import LogMetricsSource
from .sources.pcap import PcapSource
from .sources.schedtrace import SchedtraceSource
from .store import Store, StoreCache, default_cache_dir



def source_types(work_dir: Path) -> list[SourceType]:
    """The source types, in the order they are tried. work_dir holds the pcaps staged for tshark."""
    return [LogMetricsSource(), PcapSource(work_dir), SchedtraceSource()]


class _Progress:
    """One progress line on stderr for files parsed together, rewritten in place: the files done and the least advanced
    of the others.
    """

    def __init__(self, names: list[str]):
        self._names = names
        self._fractions = [0.0] * len(names)
        self._lock = threading.Lock()

    def reporter(self, index: int) -> Callable[[int, int], None]:
        """Progress function of the file at index."""

        def report(done: int, total: int) -> None:
            with self._lock:
                self._fractions[index] = done / total if total else 1.0
                self._print()

        return report

    def finish(self) -> None:
        with self._lock:
            self._fractions = [1.0] * len(self._names)
            self._print()
        print(file=sys.stderr, flush=True)

    def _print(self) -> None:
        nof_done = sum(1 for f in self._fractions if f >= 1.0)
        line = f"Parsing {len(self._names)} file(s): {nof_done} done"
        pending = [(f, name) for f, name in zip(self._fractions, self._names) if f < 1.0]
        if pending:
            fraction, name = min(pending)
            line += f", {name} {int(100 * fraction)}%"
        # Padded, so that a shorter line overwrites a longer one.
        print(f"\r{line:<70}", end="", file=sys.stderr, flush=True)


def _free_port(host: str, preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket() as s:
            # Same as the server socket, so that connections closing from a previous run do not count as busy.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
            except OSError:
                continue
            return s.getsockname()[1]
    raise OSError("No free port.")


def _default_roots() -> list[Path]:
    """The home directory and the temp directory, where gnb logs are often written."""
    roots = [Path.home().resolve()]
    tmp = Path(tempfile.gettempdir()).resolve()
    if not tmp.is_relative_to(roots[0]):
        roots.append(tmp)
    return roots


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="ocudu-viz", description="Browser-based visualizer for OCUDU artifacts.")
    p.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="Artifacts to open, each in its own tab: files, e.g. gnb.log, or directories of gNB artifacts. More can be "
        "opened from the page.",
    )
    p.add_argument("--host", default="127.0.0.1", help="Address to serve on (default: %(default)s).")
    p.add_argument("--port", type=int, default=8765, help="Port to serve on, or a free one if busy (default: %(default)s).")
    p.add_argument("--no-browser", action="store_true", help="Do not open the browser.")
    p.add_argument("--cache-dir", type=Path, help=f"Directory of parse caches (default: {default_cache_dir()}).")
    p.add_argument("--cache-size", type=float, default=2.0, help="Cache size limit in GB (default: %(default)s).")
    p.add_argument("--no-cache", action="store_true", help="Keep parsed data in memory only.")
    p.add_argument(
        "--clear-cache", action="store_true", help="Remove all parse caches, and pcaps staged for tshark, before starting."
    )
    p.add_argument(
        "-j",
        "--jobs",
        type=int,
        metavar="K",
        help="Worker processes that parse large logs, 1 to parse in the server process (default: one per CPU, up to "
        f"{parallel.MAX_WORKERS}). Logs under 8 MB are always parsed in the server process.",
    )
    p.add_argument(
        "--root",
        action="append",
        type=Path,
        default=[],
        help="Directory whose files can be opened from the page. Repeatable (default: the home and temp directories).",
    )
    return p.parse_args(argv)


def _open_runs(
    registry: SourceRegistry, cache: StoreCache, runs: list[tuple[Path, bool, list[tuple[Path, SourceType]]]]
) -> None:
    """Parses the files of runs given as (path, is_dir, [(file, source type)]) and adds the runs to the registry. A
    file that fails to parse is added in status "error", as when opened from the page.
    """

    # All the files are parsed at once, like the files of a run opened from the page: logs in the worker pool, pcaps
    # in tshark processes. Files of a directory that their type parses only on request, e.g. MAC and RLC pcaps, are
    # left for the page to parse, unless cached.
    def on_request(f: Path, st: SourceType, is_dir: bool) -> bool:
        check = getattr(st, "on_request", None)
        return is_dir and check is not None and check(f) and not cache.is_cached(f, st)

    tasks = [(f, st) for _, is_dir, typed in runs for f, st in typed if not on_request(f, st, is_dir)]
    results: dict[tuple[Path, SourceType], Store | Exception] = {}
    if tasks:
        progress = _Progress([f.name for f, _ in tasks])
        with ThreadPoolExecutor(min(len(tasks), os.cpu_count() or 1), thread_name_prefix="parse") as executor:
            futures = [executor.submit(cache.open, f, st, progress.reporter(i)) for i, (f, st) in enumerate(tasks)]
            for task, future in zip(tasks, futures):
                try:
                    results[task] = future.result()
                except Exception as e:
                    results[task] = e
        progress.finish()
    for path, is_dir, typed in runs:
        source_ids = []
        for f, st in typed:
            if on_request(f, st, is_dir):
                source_ids.append(registry.add_deferred(f, st).id)
                continue
            result = results[(f, st)]
            if isinstance(result, Exception):
                text = error_text(f, result)
                print(f"ocudu-viz: {f}: {text}", file=sys.stderr)
                source_ids.append(registry.add_error(f, st, f"{f.name}: {text}").id)
            else:
                source_ids.append(registry.add_store(result, st).id)
        registry.add_run(path, is_dir, source_ids)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    cache = StoreCache(args.cache_dir, int(args.cache_size * (1 << 30)), enabled=not args.no_cache)
    tshark_dir = cache.cache_dir / "tshark"
    if args.clear_cache:
        cache.clear()
        # Pcaps staged for tshark, copies when tshark cannot read them in place.
        shutil.rmtree(tshark_dir, ignore_errors=True)
    roots = [r.resolve() for r in args.root] or _default_roots()
    for r in roots:
        if not r.is_dir():
            print(f"ocudu-viz: {r}: not a directory.", file=sys.stderr)
            return 2
    if args.jobs is not None and args.jobs < 1:
        print("ocudu-viz: --jobs must be at least 1.", file=sys.stderr)
        return 2
    parallel.configure(args.jobs)
    parallel.warm_up()
    types = source_types(tshark_dir)
    registry = SourceRegistry(cache, types, roots)

    # The files of each run given, checked before any is parsed.
    runs: list[tuple[Path, bool, list[tuple[Path, SourceType]]]] = []
    for path in args.files:
        is_dir = path.is_dir()
        if is_dir:
            files = registry.supported_files(path)
            if not files:
                print(f"ocudu-viz: {path}: no supported files in the directory.", file=sys.stderr)
                return 2
        elif path.is_file():
            files = [path]
        else:
            print(f"ocudu-viz: {path}: not a file or directory.", file=sys.stderr)
            return 2
        typed = []
        for f in files:
            source_type = next((st for st in types if st.accepts(f)), None)
            if source_type is None:
                print(f"ocudu-viz: {f}: unsupported file type.", file=sys.stderr)
                return 2
            typed.append((f.resolve(), source_type))
        runs.append((path.resolve(), is_dir, typed))

    _open_runs(registry, cache, runs)

    # Imported late so that --help and argument errors do not pay for the web stack.
    import uvicorn

    from .server import create_app

    port = _free_port(args.host, args.port)
    # Listening on all interfaces, e.g. inside a container, still serves the loopback address.
    url_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{url_host}:{port}/"
    nof_runs = len(registry.runs())
    print(f"Serving {nof_runs} run(s) at {url}, more can be opened from the page (Ctrl+C to stop)", file=sys.stderr)
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, (url,)).start()
    # The pool is shut down with the server: on SIGTERM, uvicorn exits through the default handler, skipping atexit.
    uvicorn.run(create_app(registry, on_shutdown=parallel.shutdown), host=args.host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
