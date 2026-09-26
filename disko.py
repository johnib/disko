#!/usr/bin/env python3
"""
disko -- interactive disk usage explorer
Runs a local web server with a real-time D3.js treemap of your filesystem.
Usage: python3 disko.py [--port PORT] [--path PATH] [--no-browser] [--du-timeout SECS] [--verbose]
"""

import argparse
import concurrent.futures
import errno
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

__version__ = "1.1.0"
_default_path = "/"
_verbose = False  # --verbose: log HTTP requests

SCAN_WORKERS = 12   # max concurrent du processes (global cap across all scans and prefetches)
PREFETCH_WORKERS = 4  # background prefetch workers
CACHE_FILE = os.path.expanduser(os.environ.get('DISKO_CACHE') or '~/.disko_cache.json')
CACHE_VERSION = 2  # bump when the cached child-dict shape changes incompatibly
                    # (v2: loose files became per-type-bucket entries with 'fileType')
PREFETCH_TOP_N = 10  # prefetch top-N largest subdirs after each scan
PREFETCH_MAX_DEPTH = 1  # how many levels below a scanned dir to prefetch
CACHE_TTL = 300  # seconds; older cache hits are served, then re-scanned and re-streamed
HEARTBEAT_SECS = 2  # SSE keepalive interval while waiting on du (detects disconnects)
DU_TIMEOUT = 300     # seconds per du call (overridable via --du-timeout)
DU_POLL_SECS = 0.2   # how often a running du checks whether it should be killed
BG_DU_SLOTS = 4      # of SCAN_WORKERS, how many du processes background work may hold

# ── Cache ────────────────────────────────────────────────────────────────────

_cache: dict = {}
_cache_lock = threading.Lock()
_save_lock = threading.Lock()  # serializes file writes without blocking cache_get
# Never persist as root: avoids writing a root-owned file into a (possibly sudo-inherited) home dir.
_persist_cache = not (hasattr(os, 'geteuid') and os.geteuid() == 0)


def cache_load():
    global _cache
    if not _persist_cache:
        print('  Running as root: cache will not be saved to disk.')
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError('expected a JSON object')
        _cache = data
        print(f'  Cache loaded: {len(_cache)} paths from {CACHE_FILE}')
    except FileNotFoundError:
        _cache = {}
    except Exception as e:
        print(f'  Warning: ignoring unreadable cache file {CACHE_FILE} ({e}); starting with an empty cache')
        _cache = {}


def cache_save():
    if not _persist_cache:
        return
    # Snapshot under the cache lock, but do the (slow) file write outside it.
    # Snapshot inside the save lock so an older snapshot can't overwrite a newer one.
    with _save_lock:
        with _cache_lock:
            data = json.dumps(_cache)
        tmp = None
        try:
            # mkstemp creates the file 0600 in the same dir, so os.replace is an atomic rename
            # that swaps the directory entry (never writes through a symlink at CACHE_FILE).
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CACHE_FILE), prefix='.disko_cache.', suffix='.tmp')
            with os.fdopen(fd, 'w') as f:
                f.write(data)
            os.replace(tmp, CACHE_FILE)
            tmp = None
        except Exception as e:
            print(f'  Cache save error: {e}')
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def _cache_key(path: str) -> str:
    # Same normalization as norm_path(), so /invalidate?path=/a/b/ hits the /a/b entry.
    return os.path.abspath(os.path.expanduser(path))


def _sound_entry(entry) -> bool:
    """True if entry is a well-formed cache dict (has the keys every reader relies on).

    Guards every raw _cache[...]/_cache.get(...) read (cache_get, cache_set's staleness
    check, _propagate_size's ancestor read) against a malformed/foreign value ending up
    in _cache -- cache_load only validates that the top-level JSON is a dict, never each
    per-path value, so a hand-edited or corrupted cache file can put anything at a key."""
    return isinstance(entry, dict) and 'children' in entry and 'scanned_at' in entry


def cache_get(path: str):
    with _cache_lock:
        entry = _cache.get(_cache_key(path))
        if not _sound_entry(entry) or entry.get('version') != CACHE_VERSION:
            return None  # old/foreign/malformed shape: treat as a miss, not a crash or stale render
        return entry


def cache_set(path: str, children: list, scanned_at: float = None) -> bool:
    """Store children for path, stamped with the scan *start* time.

    Skips the write if the existing entry comes from a scan that started
    later (e.g. a slow prefetch finishing after a forced refresh).
    Also propagates the new total size into cached ancestor entries.
    Returns True if the cache was updated.
    """
    key = _cache_key(path)
    if scanned_at is None:
        scanned_at = time.time()
    with _cache_lock:
        existing = _cache.get(key)
        if _sound_entry(existing) and existing.get('scanned_at', 0) > scanned_at:
            return False
        _cache[key] = {'children': children, 'scanned_at': scanned_at, 'version': CACHE_VERSION}
        _propagate_size(key, sum(c.get('size') or 0 for c in children), scanned_at,
                        any(c.get('status') for c in children))
    cache_save()
    return True


def _propagate_size(key: str, new_size: int, scanned_at: float, partial: bool = False):
    """Update key's size inside cached ancestor entries (caller holds _cache_lock).

    `partial` marks the dir's entry in its parent as 'partial' when the rescan
    itself had partial children (du couldn't read everything)."""
    child = key
    parent = os.path.dirname(child)
    while parent != child:
        entry = _cache.get(parent)
        if not _sound_entry(entry) or entry.get('scanned_at', 0) > scanned_at:
            return
        item = next((c for c in entry['children']
                     if c.get('isDir') and _cache_key(c['path']) == child), None)
        new_status = 'partial' if partial else None
        # Mount points stay out of their parent's totals (du -x semantics), even once scanned.
        if item is None or item.get('mount') or (item.get('size') == new_size and item.get('status') == new_status):
            return
        updated = {k: v for k, v in item.items() if k != 'status'}
        updated['size'] = new_size
        if new_status:
            updated['status'] = new_status
        # Build a new list (don't mutate one a stream may be iterating)
        children = [updated if c is item else c for c in entry['children']]
        children.sort(key=_size_key)
        entry['children'] = children
        new_size = sum(c.get('size') or 0 for c in children)
        partial = any(c.get('status') for c in children)
        child, parent = parent, os.path.dirname(parent)


def cache_delete(path: str):
    with _cache_lock:
        _cache.pop(_cache_key(path), None)
    cache_save()


# ── Scanning ─────────────────────────────────────────────────────────────────
#
# Sizes are allocated disk usage (blocks), both for directories (du -k) and
# for loose files (st_blocks * 512), so sparse files are measured the same way
# from the parent and after drilling in.
#
# Known limitation -- hard links: each child directory is measured by its own
# du process (in parallel, so results can stream in quickly). du only
# de-duplicates hard links within a single run, so a file hard-linked into two
# sibling directories is counted in both (pnpm, nix, ccache trees, Time
# Machine-style backups). The totals shown for a folder can therefore exceed
# its real usage. Each child's own size is still correct in isolation.


def _resolve_scan_path(path: str) -> str:
    path = norm_path(path)
    # On macOS "/" is the read-only system volume, and the Data volume is
    # reachable both through firmlinks (/Users, /Applications, ...) and via
    # /System/Volumes/Data -- all on the same st_dev, so du -x counts it more
    # than once. Scan the Data volume directly instead.
    if path in ('/', '//') and platform.system() == "Darwin":
        return "/System/Volumes/Data"
    return path


def _alloc_size(entry) -> int:
    """Allocated size of a non-directory entry (0 if it can't be stat'ed)."""
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return 0
    blocks = getattr(st, 'st_blocks', None)
    return blocks * 512 if blocks is not None else st.st_size


def _split_mounts(path: str, dirs: list):
    """Split dirs into (same-filesystem dirs, mount point dirs)."""
    try:
        parent_dev = os.stat(path).st_dev
    except OSError:
        return dirs, []
    local, mounts = [], []
    for e in dirs:
        try:
            dev = e.stat(follow_symlinks=False).st_dev
        except OSError:
            dev = parent_dev
        (mounts if dev != parent_dev else local).append(e)
    return local, mounts


def _mount_child(entry) -> dict:
    # Another filesystem is mounted here: don't run du on it (it could be a
    # network share or an external disk). Size is unknown; drill in to scan it.
    return {'name': entry.name, 'path': entry.path, 'size': 0, 'isDir': True, 'mount': True}


def norm_path(path: str) -> str:
    """Absolute, user-expanded, normalized path (never starts with '-')."""
    return os.path.abspath(os.path.expanduser(path))


def du_single(path: str, cancel=None):
    """Return (size_bytes_or_None, status).

    status is None on success, 'partial' when du exited non-zero but still
    reported a total (e.g. unreadable subdirs), 'timeout', 'cancelled' (the
    `cancel` callable returned True, so du was killed), or an error string
    when no size could be determined (size is then None).
    """
    try:
        proc = subprocess.Popen(["du", "-sk", "-x", "--", path],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        return None, str(e) or 'du failed'
    deadline = time.monotonic() + DU_TIMEOUT
    while True:
        try:
            out, err = proc.communicate(timeout=DU_POLL_SECS)
            break
        except subprocess.TimeoutExpired:
            cancelled = _shutting_down.is_set() or (cancel is not None and cancel())
            status = 'cancelled' if cancelled else (
                'timeout' if time.monotonic() >= deadline else None)
            if status:
                proc.kill()
                proc.communicate()
                return None, status
    try:
        kb = int(out.split(b'\t', 1)[0])
    except ValueError:
        lines = err.decode('utf-8', 'replace').strip().splitlines()
        return None, (lines[0][:200] if lines else 'du failed (exit %d)' % proc.returncode)
    return kb * 1024, ('partial' if proc.returncode else None)


_du_slots = threading.BoundedSemaphore(SCAN_WORKERS)  # global cap on concurrent du processes
_bg_slots = threading.BoundedSemaphore(BG_DU_SLOTS)   # background share of it: live scans keep the rest

# Live (user-facing) scans take priority over background work: background du calls
# don't start while a live scan runs, and running prefetch du calls get killed when
# one starts (their results are dropped; the folder is scanned again when opened).
_live_cv = threading.Condition()
_live_count = 0
_live_gen = 0  # bumped each time a live scan starts
_shutting_down = threading.Event()  # set on Ctrl+C/SIGTERM: kill every du, stop waiting


class live_scan(object):
    """Context manager marking a user-facing scan as in progress."""

    def __enter__(self):
        global _live_count, _live_gen
        with _live_cv:
            _live_count += 1
            _live_gen += 1

    def __exit__(self, *exc):
        global _live_count
        with _live_cv:
            _live_count -= 1
            _live_cv.notify_all()


def _wait_for_no_live_scan() -> int:
    """Block until no live scan is running; return the live-scan generation then."""
    with _live_cv:
        while _live_count and not _shutting_down.is_set():
            _live_cv.wait(0.5)
        return _live_gen


def du_bounded(path: str, stop=None, background=None):
    """du_single, limited by the global du semaphore.

    Live scans (background=None) are skipped/killed once `stop` is set.
    background='prefetch' waits for live scans to finish and is killed if a new one
    starts; background='revalidate' only waits (its result replaces a listing the
    user is looking at, so it isn't thrown away).

    Returns du_single's (size_or_None, status) tuple.
    """
    if background:
        with _bg_slots:
            gen = _wait_for_no_live_scan()
            with _du_slots:
                if _shutting_down.is_set():
                    return None, 'cancelled'
                if background == 'prefetch':
                    return du_single(path, lambda: _live_gen != gen)
                return du_single(path)
    with _du_slots:
        if stop is not None and stop.is_set():
            return None, 'cancelled'
        return du_single(path, stop.is_set if stop is not None else None)


def _dir_child(entry, result) -> dict:
    size, status = result
    child = {'name': entry.name, 'path': entry.path, 'size': size, 'isDir': True}
    if status:
        child['status'] = status
    return child


def _size_key(child) -> int:
    return -(child.get('size') or 0)


def _cacheable(children) -> bool:
    """Scans with unknown-size children are not cached, so they get retried."""
    return children is not None and all(c.get('size') is not None for c in children)


FILE_TYPE_EXTENSIONS = {
    'image':   {'jpg', 'jpeg', 'png', 'gif', 'heic', 'webp', 'svg', 'bmp', 'tiff', 'tif', 'raw', 'cr2', 'nef', 'ico'},
    'video':   {'mp4', 'mov', 'mkv', 'avi', 'webm', 'm4v', 'flv', 'wmv', 'mpg', 'mpeg'},
    'audio':   {'mp3', 'wav', 'flac', 'aac', 'm4a', 'ogg', 'wma', 'opus'},
    'archive': {'zip', 'tar', 'gz', 'tgz', 'bz2', 'xz', '7z', 'rar', 'dmg', 'iso', 'zst', 'lz4'},
    'document': {'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'txt', 'md', 'epub', 'rtf', 'odt', 'csv'},
    'code':    {'py', 'js', 'ts', 'jsx', 'tsx', 'go', 'rs', 'c', 'cc', 'cpp', 'h', 'hpp', 'java', 'json',
                'yaml', 'yml', 'html', 'css', 'sh', 'rb', 'php', 'swift', 'kt'},
}
# Checked before the last single suffix: os.path.splitext only sees the last dot-segment,
# so 'archive.tar.zst' would otherwise fall to 'other'. Every suffix here also has its
# trailing segment (gz/bz2/xz/zst/lz4) independently mapped to 'archive' above (a lone
# gzipped file is legitimately an archive too), so this branch is redundant for every
# case currently listed -- it exists for a future compound suffix whose trailing segment
# ISN'T independently archive-mapped, and is cheap and self-documenting either way.
COMPOUND_ARCHIVE_SUFFIXES = {'tar.gz', 'tar.bz2', 'tar.xz', 'tar.zst', 'tar.lz4'}

_EXT_TO_TYPE = {ext: t for t, exts in FILE_TYPE_EXTENSIONS.items() for ext in exts}

_BUCKET_LABELS = {'image': 'images', 'video': 'video', 'audio': 'audio',
                   'archive': 'archives', 'document': 'documents', 'code': 'code', 'other': 'other'}


def classify_file(name: str) -> str:
    """Return name's file-type bucket (image/video/audio/archive/document/code/other).

    Case-insensitive; checks the last two dot-segments against COMPOUND_ARCHIVE_SUFFIXES
    before falling back to the single last suffix. 'other' covers unmatched, extensionless,
    and dotfile-with-no-extension names -- this function never raises."""
    lower = name.lower()
    parts = lower.rsplit('.', 2)
    if len(parts) == 3 and '.'.join(parts[1:]) in COMPOUND_ARCHIVE_SUFFIXES:
        return 'archive'
    ext = os.path.splitext(lower)[1].lstrip('.')
    return _EXT_TO_TYPE.get(ext, 'other')


def _list_entries(path: str):
    """Return (subdirs, file_totals) for path. Raises OSError if it can't be listed.

    file_totals is {type_bucket: summed_allocated_size}, classified via classify_file();
    loose files are measured by allocated size (_alloc_size), like du."""
    entries = list(os.scandir(path))
    dirs = [e for e in entries if e.is_dir(follow_symlinks=False)]
    file_totals: dict = {}
    for e in entries:
        if e.is_dir(follow_symlinks=False):
            continue
        bucket = classify_file(e.name)
        file_totals[bucket] = file_totals.get(bucket, 0) + _alloc_size(e)
    return dirs, file_totals


def _iter_children(path: str, dirs: list, file_totals: dict, stop=None, background=None):
    """Yield path's child dicts: mount points first (never du'ed), then each subdir
    as its du completes, then one entry per non-empty file-type bucket (e.g.
    '(images)', '(documents)'), each carrying 'fileType' so the frontend can
    color/group/filter loose files by type.

    Yields None every HEARTBEAT_SECS while waiting on du, so a streaming caller can
    send a keepalive (and notice a disconnected client). If `stop` gets set, queued
    du calls are skipped and iteration ends early without the bucket entries.
    Pending du work is cancelled when the generator is closed or exhausted.
    """
    dirs, mounts = _split_mounts(path, dirs)
    for e in mounts:
        yield _mount_child(e)
    # Not a `with` block: its exit would wait for every running du after a disconnect
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS)
    futures = {ex.submit(du_bounded, e.path, stop, background): e for e in dirs}
    pending = set(futures)
    try:
        while pending and not (stop is not None and stop.is_set()):
            done, pending = concurrent.futures.wait(
                pending, timeout=HEARTBEAT_SECS,
                return_when=concurrent.futures.FIRST_COMPLETED)
            if not done:
                yield None
            for fut in done:
                yield _dir_child(futures[fut], fut.result())
    finally:
        for f in pending:  # manual cancel_futures (Python 3.8 compatible)
            f.cancel()
        ex.shutdown(wait=False)
    if stop is not None and stop.is_set():
        return

    for bucket, total in sorted(file_totals.items()):
        if total <= 0:
            continue
        yield {
            'name': f'({_BUCKET_LABELS.get(bucket, bucket)})',
            'path': path,
            'size': total,
            'isDir': False,
            'fileType': bucket,
        }


def scan_to_list(path: str, background=None):
    """Scan path and return list of child dicts, or None if it can't be read.

    `background` is passed to du_bounded ('prefetch' / 'revalidate')."""
    path = _resolve_scan_path(path)
    try:
        dirs, file_totals = _list_entries(path)
    except OSError:
        return None

    children = [c for c in _iter_children(path, dirs, file_totals, background=background) if c is not None]
    children.sort(key=_size_key)
    return children


# ── Background prefetch pool ─────────────────────────────────────────────────

_prefetch_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=PREFETCH_WORKERS, thread_name_prefix='prefetch')
_prefetching: set = set()
_prefetch_futures: set = set()  # pending/running prefetch futures (for shutdown)
_prefetch_lock = threading.Lock()
_revalidations: dict = {}  # path -> in-flight stale-cache rescan job (shared by all streams)


def submit_scan(path: str, depth: int = 0) -> bool:
    """Scan path in the background (deduped against in-flight scans), then
    prefetch `depth` levels of its children. Returns False if already running
    or the pool has been shut down (Ctrl+C / interpreter exit)."""
    with _prefetch_lock:
        if path in _prefetching:
            return False
        _prefetching.add(path)
    try:
        fut = _prefetch_pool.submit(_do_prefetch, path, depth)
    except RuntimeError:  # pool already shut down
        with _prefetch_lock:
            _prefetching.discard(path)
        return False
    with _prefetch_lock:
        _prefetch_futures.add(fut)
    fut.add_done_callback(lambda f: _prefetch_done(path, f))
    return True


def _prefetch_done(path: str, fut):
    with _prefetch_lock:
        _prefetch_futures.discard(fut)
        if fut.cancelled():  # _do_prefetch never ran, so its finally didn't clean up
            _prefetching.discard(path)
    if fut.cancelled():
        return
    exc = fut.exception()
    if exc is not None:
        print(f'  Prefetch error for {path}: {exc!r}', file=sys.stderr)


def stop_prefetch():
    """Cancel queued prefetch scans, kill running du processes, stop accepting new scans."""
    _shutting_down.set()
    with _prefetch_lock:
        pending = list(_prefetch_futures)
    for fut in pending:
        fut.cancel()
    _prefetch_pool.shutdown(wait=False)


def schedule_prefetch(children: list, depth: int = PREFETCH_MAX_DEPTH):
    """Kick off background scans for the top-N largest uncached child dirs,
    going at most `depth` levels down. Unknown (None) sizes are never prefetched."""
    if depth <= 0:
        return
    dirs = [c for c in children if c.get('isDir') and (c.get('size') or 0) > 0]
    dirs = sorted(dirs, key=_size_key)[:PREFETCH_TOP_N]
    for d in dirs:
        p = d['path']
        if cache_get(p):
            continue
        submit_scan(p, depth - 1)


def _do_prefetch(path: str, depth: int):
    try:
        started = time.time()
        children = scan_to_list(path, background='prefetch')
        if children is None or any(c.get('status') == 'cancelled' for c in children):
            return  # unreadable, or preempted by a live scan
        if _cacheable(children):
            cache_set(path, children, scanned_at=started)
        schedule_prefetch(children, depth)
    finally:
        with _prefetch_lock:
            _prefetching.discard(path)


def _start_revalidation(path: str) -> dict:
    """Return the in-flight stale-cache rescan job for path, starting one if none.

    Deduplicated per path, so many tabs/reloads on the same stale folder share a
    single rescan. The job dict gets 'fresh' (see _revalidate) and its 'done'
    event is set when the rescan finishes."""
    with _prefetch_lock:
        job = _revalidations.get(path)
        if job is not None:
            return job
        job = {'done': threading.Event(), 'fresh': None}
        _revalidations[path] = job
    threading.Thread(target=_revalidate, args=(path, job), daemon=True).start()
    return job


def _revalidate(path: str, job: dict):
    """Rescan a stale cached dir for stream_directory (runs in a worker thread).

    Sets job['fresh'] to the listing to stream ({'children', 'scanned_at'}),
    or leaves it None if the dir can't be read. Registers in _prefetching so a
    concurrent prefetch of the same path is skipped."""
    started = time.time()
    with _prefetch_lock:
        owned = path not in _prefetching
        _prefetching.add(path)
    try:
        children = scan_to_list(path, background='revalidate')
        if children is None:
            return
        if _cacheable(children):
            cache_set(path, children, scanned_at=started)
            # A newer scan may have won the race; stream whatever the cache now holds.
            job['fresh'] = cache_get(path) or {'children': children, 'scanned_at': started}
        else:
            # Unknown sizes aren't cached (so they get retried) but are still shown.
            job['fresh'] = {'children': children, 'scanned_at': started}
        schedule_prefetch(job['fresh']['children'])
    finally:
        with _prefetch_lock:
            if owned:
                _prefetching.discard(path)
            _revalidations.pop(path, None)
        job['done'].set()


# ── Streaming (SSE) ───────────────────────────────────────────────────────────

def stream_directory(path: str, write_event, force: bool = False, stop=None):
    if stop is None:
        stop = threading.Event()
    # Report the path the client asked for ('/' stays '/' even though macOS scans the
    # Data volume), so the breadcrumb and Up button treat it as the root.
    shown = norm_path(path)
    if shown == '//':
        shown = '/'
    path = _resolve_scan_path(path)
    if not os.path.isdir(path):
        write_event({'type': 'error', 'error': 'Not a directory or not found'})
        return

    cached = cache_get(path) if not force else None

    if cached:
        # Serve cache immediately
        write_event({
            'type': 'start',
            'path': shown,
            'name': os.path.basename(shown) or shown,
            'total_dirs': len(cached['children']),
            'from_cache': True,
            'scanned_at': cached['scanned_at'],
        })
        for child in cached['children']:
            write_event({'type': 'child', **child})

        if time.time() - cached['scanned_at'] < CACHE_TTL:
            write_event({'type': 'done'})
            return

        # Stale: keep the stream open, re-scan, and stream the refreshed listing.
        # The scan runs in a worker so we can heartbeat (and notice disconnects)
        # meanwhile; it finishes and updates the cache even if the client leaves.
        write_event({'type': 'revalidating'})
        job = _start_revalidation(path)  # joins an in-flight rescan of path if there is one
        while not stop.is_set() and not job['done'].wait(HEARTBEAT_SECS):
            write_event(None)  # heartbeat: detects a disconnected client
        if stop.is_set():
            return
        fresh = job['fresh']
        if fresh is None:  # dir became unreadable: keep showing the cached listing
            write_event({'type': 'done'})
            return
        write_event({
            'type': 'refresh',
            'path': shown,
            'name': os.path.basename(shown) or shown,
            'total_dirs': len(fresh['children']),
            'scanned_at': fresh['scanned_at'],
        })
        for child in fresh['children']:
            write_event({'type': 'child', **child})
        write_event({'type': 'done'})
        return

    # Live scan
    started = time.time()
    try:
        dirs, file_totals = _list_entries(path)
    except OSError as e:
        write_event({'type': 'error', 'error': str(e)})
        return

    # Counted before the mount split (mounts are emitted as children too), plus one slot
    # per non-empty type bucket that _iter_children will yield after the dirs, so progress
    # never exceeds 100% even when loose files span multiple buckets.
    nonempty_buckets = sum(1 for v in file_totals.values() if v > 0)
    total_dirs = len(dirs) + nonempty_buckets

    write_event({
        'type': 'start',
        'path': shown,
        'name': os.path.basename(shown) or shown,
        'total_dirs': total_dirs,
        'from_cache': False,
    })

    collected = []
    with live_scan():  # background du work yields to this scan
        children = _iter_children(path, dirs, file_totals, stop)
        try:
            for child in children:
                if child is None:
                    write_event(None)  # heartbeat: detects a disconnected client
                    continue
                collected.append(child)
                write_event({'type': 'child', **child})
        finally:
            children.close()  # cancels queued du calls if we bail out early
    if stop.is_set():
        return  # client went away: don't cache partial results

    write_event({'type': 'done'})
    if _cacheable(collected):
        cache_set(path, sorted(collected, key=_size_key), scanned_at=started)
    schedule_prefetch(collected)


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Disk Explorer</title>
<script src="https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"
  integrity="sha384-CjloA8y00+1SDAUkjs099PVfnY2KmDC2BZnws9kh8D/lX1s46w6EPhpXdqMfjK6i"
  crossorigin="anonymous"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', sans-serif;
  background: #111318; color: #e2e8f0;
  height: 100vh; display: flex; flex-direction: column; overflow: hidden;
}

/* ── Header ── */
#header {
  display: flex; align-items: center; gap: 12px;
  padding: 0 16px; height: 50px;
  background: #161b26; border-bottom: 1px solid #1e2535; flex-shrink: 0;
}
#logo { font-size: 17px; }
#app-title { font-size: 14px; font-weight: 700; color: #f1f5f9; white-space: nowrap; }
#breadcrumb { display: flex; align-items: center; flex: 1; gap: 2px; overflow: hidden; min-width: 0; }
.crumb {
  font: inherit; font-size: 12px; color: #94a3b8; cursor: pointer;
  background: transparent; border: none;
  padding: 3px 6px; border-radius: 4px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 160px;
  min-width: 24px; flex-shrink: 1; transition: all .15s;
}
.crumb:hover { background: #1e2535; color: #cbd5e1; }
.crumb.active { color: #f1f5f9; font-weight: 500; cursor: default; flex-shrink: 0; }
.crumb.active:hover { background: transparent; }
.crumb-sep { color: #8391a7; font-size: 13px; flex-shrink: 0; }

#header-right { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
#path-form { display: flex; }
#path-input {
  font-size: 12px; padding: 4px 10px; border-radius: 6px;
  background: #1e2535; border: 1px solid #2d3748; color: #94a3b8;
  outline: none; width: 230px; font-family: 'SF Mono', monospace; transition: border-color .15s;
}
#path-input:focus { border-color: #e94560; color: #f1f5f9; }
#total-size { font-size: 12px; color: #94a3b8; white-space: nowrap; }
#total-size span { color: #e94560; font-weight: 700; }

.hdr-btn {
  font-size: 12px; padding: 4px 10px; border-radius: 6px;
  background: #1e2535; border: 1px solid #2d3748; color: #94a3b8;
  cursor: pointer; transition: all .15s; white-space: nowrap;
}
.hdr-btn:hover { background: #2d3748; color: #f1f5f9; }
.hdr-btn:disabled { opacity: .5; cursor: default; }
.hdr-btn:disabled:hover { background: #1e2535; color: #94a3b8; }
button:focus-visible, [tabindex]:focus-visible { outline: 2px solid #e94560; outline-offset: 1px; }
.sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0;
}
#back-btn { display: none; }
#back-btn.visible { display: block; }
#refresh-btn.spinning { animation: spin .7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Progress bar ── */
#progress-bar { height: 2px; background: #1e2535; flex-shrink: 0; transition: opacity .4s; }
#progress-fill { height: 100%; background: #e94560; width: 0;
  transition: width .2s ease-out; box-shadow: 0 0 6px #e94560; }

/* ── Body ── */
#body { flex: 1; display: flex; overflow: hidden; }

/* ── Treemap ── */
#treemap-wrap { flex: 1; position: relative; padding: 10px; overflow: hidden; }
#treemap { width: 100%; height: 100%; display: block; }
.cell { cursor: pointer; }
.cell rect { stroke: #111318; stroke-width: 1.5px; transition: opacity .1s; }
.cell:hover rect { opacity: .8; stroke-width: 0; }
.cell.file { cursor: default; }
.cell:focus { outline: none; }
.cell:focus-visible rect { stroke: #f8fafc; stroke-width: 2.5px; opacity: 1; }
.cell text { pointer-events: none; }
.cell.partial rect { stroke: #cbd5e1; stroke-dasharray: 4 3; }
.cell.unknown rect { fill: #334155; stroke: #64748b; stroke-dasharray: 4 3; }

/* ── Sidebar ── */
#sidebar {
  width: 270px; flex-shrink: 0; background: #161b26;
  border-left: 1px solid #1e2535; display: flex; flex-direction: column; overflow: hidden;
}
#sidebar-header {
  padding: 10px 16px 9px; font-size: 11px; font-weight: 600; color: #8391a7;
  text-transform: uppercase; letter-spacing: .8px; border-bottom: 1px solid #1e2535;
  display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; gap: 6px;
}
#scan-badge {
  display: flex; align-items: center; gap: 5px;
  font-size: 10px; color: #94a3b8; font-weight: 400; letter-spacing: 0;
}
.scan-dot {
  width: 6px; height: 6px; border-radius: 50%; background: #e94560;
  animation: pulse 1s ease-in-out infinite; display: none; flex-shrink: 0;
}
.scan-dot.active { display: inline-block; }
@keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.3;transform:scale(.65)} }

#cache-badge {
  font-size: 10px; color: #8391a7; padding: 2px 6px; border-radius: 4px;
  background: #1e2535; border: 1px solid #2d3748; white-space: nowrap; display: none;
}
#cache-badge.show { display: block; }

#sidebar-list { overflow-y: auto; flex: 1; }
#sidebar-list::-webkit-scrollbar { width: 4px; }
#sidebar-list::-webkit-scrollbar-thumb { background: #2d3748; border-radius: 2px; }

.sitem {
  display: flex; align-items: center; gap: 3px; padding-right: 10px;
  border-bottom: 1px solid #1a2030; transition: background .1s;
  animation: fadeSlide .18s ease-out both;
}
.sitem-main {
  flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px;
  padding: 8px 6px 7px 16px; font: inherit; color: inherit; text-align: left;
  background: transparent; border: none; cursor: pointer;
}
@keyframes fadeSlide { from{opacity:0;transform:translateX(8px)} to{opacity:1;transform:translateX(0)} }
.sitem:hover { background: #1e2535; }
.sitem.file .sitem-main { cursor: default; }
.sitem.unknown .sitem-size { color: #f59e0b; font-style: italic; }
.sitem.mount .sitem-name { font-style: italic; }
.sitem.mount .sitem-bar-wrap { visibility: hidden; }
.sitem-top { display: flex; align-items: center; gap: 7px; }
.sitem-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.sitem-name { flex: 1; font-size: 12px; color: #cbd5e1;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sitem-size { font-size: 11px; color: #94a3b8; white-space: nowrap; }
.sitem-actions { display: flex; align-items: center; gap: 3px; opacity: 0; transition: opacity .15s; }
.sitem:hover .sitem-actions, .sitem:focus-within .sitem-actions { opacity: 1; }
@media (hover: none) { .sitem-actions { opacity: 1; } }
.sitem-cached { font-size: 9px; color: #1d4ed8; background: #1e3a5f; padding: 1px 4px; border-radius: 3px; }
.sitem-refresh-btn {
  font-size: 11px; padding: 1px 5px; border-radius: 3px; cursor: pointer;
  color: #94a3b8; background: transparent; border: none;
  transition: all .15s; line-height: 1.4;
}
.sitem-refresh-btn:hover { background: #2d3748; color: #f47a8f; }
.sitem-refresh-btn.spinning { animation: spin .7s linear infinite; color: #f47a8f; cursor: progress; }
.sitem-refresh-btn.failed { color: #f47a8f; }
.sitem-bar-wrap { height: 2px; background: #1e2535; border-radius: 1px; margin-left: 15px; width: calc(100% - 15px); }
.sitem-bar { height: 100%; border-radius: 1px; opacity: .45; }

/* ── Legend ── */
#legend { display: none; flex-direction: column; gap: 2px; padding: 8px 16px 6px; border-bottom: 1px solid #1e2535; }
.legend-caption { font-size: 10px; color: #8391a7; margin-bottom: 3px; }
.legend-item {
  display: flex; align-items: center; gap: 7px; padding: 3px 4px; border-radius: 5px;
  cursor: pointer; transition: background .15s;
}
.legend-item:hover { background: #1e2535; }
.legend-item[aria-pressed="true"] { background: #1e2535; }
.legend-swatch { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.legend-label { flex: 1; font-size: 11px; color: #cbd5e1; }
.legend-size { font-size: 10px; color: #94a3b8; white-space: nowrap; }

/* Dim states applied to treemap cells and sidebar rows while a legend filter is active.
   Every visible part of a dimmed cell/row is covered (rect AND label text; dot, name,
   size AND the usage bar), not just the background, so a dimmed item can't still read
   as "active" via a crisp label or size left at full opacity.
   :hover overrides pin the dimmed opacity so the existing .cell:hover rect rule (same
   specificity) can't accidentally undim a filtered-out cell on hover. */
.cell.dim-other rect, .cell.dim-other text  { opacity: .25; }
.cell.dim-neutral rect, .cell.dim-neutral text { opacity: .55; }
.cell.dim-other:hover rect  { opacity: .25; }
.cell.dim-neutral:hover rect { opacity: .55; }
.sitem.dim-other  .sitem-dot, .sitem.dim-other  .sitem-name,
.sitem.dim-other  .sitem-size, .sitem.dim-other  .sitem-bar  { opacity: .25; }
.sitem.dim-neutral .sitem-dot, .sitem.dim-neutral .sitem-name,
.sitem.dim-neutral .sitem-size, .sitem.dim-neutral .sitem-bar { opacity: .55; }

/* ── Error ── */
#error-overlay, #d3-overlay {
  position: absolute; inset: 0; display: none;
  align-items: center; justify-content: center; flex-direction: column; gap: 8px;
  background: rgba(17,19,24,.7);
}
#error-overlay.show, #d3-overlay.show { display: flex; }
#error-title, #d3-title { color: #e94560; font-size: 15px; font-weight: 600; }
#error-detail, #d3-detail { color: #94a3b8; font-size: 12px; }

/* ── Loading ── */
#loading-overlay {
  position: absolute; inset: 0; display: none;
  align-items: center; justify-content: center; flex-direction: column; gap: 10px;
  background: rgba(17,19,24,.55); pointer-events: none;
}
#loading-overlay.show { display: flex; }
.loading-spinner {
  width: 28px; height: 28px; border-radius: 50%;
  border: 3px solid #334155; border-top-color: #e94560;
  animation: spin .8s linear infinite;
}
#loading-title { color: #e2e8f0; font-size: 14px; font-weight: 600; max-width: 80%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#loading-detail { color: #94a3b8; font-size: 12px; }
@media (prefers-reduced-motion: reduce) { .loading-spinner { animation-duration: 2.4s; } }

/* ── Empty state ── */
#empty-state {
  position: absolute; inset: 0; display: none; align-items: center; justify-content: center;
  color: #94a3b8; font-size: 13px; pointer-events: none;
}
#empty-state.show { display: flex; }
.sidebar-empty { padding: 14px 16px; font-size: 12px; color: #94a3b8; }

/* ── Tooltip ── */
#tooltip {
  position: fixed; z-index: 999; background: #1e2535; border: 1px solid #2d3748;
  border-radius: 9px; padding: 10px 14px; font-size: 13px; pointer-events: none;
  display: none; box-shadow: 0 8px 32px rgba(0,0,0,.5); max-width: 300px;
}
.tt-name { font-weight: 600; color: #f1f5f9; margin-bottom: 3px; word-break: break-word; }
.tt-path { font-size: 11px; color: #8391a7; margin-bottom: 8px; word-break: break-all; }
.tt-size { font-size: 18px; font-weight: 700; color: #f26b83; }
.tt-pct  { font-size: 11px; color: #94a3b8; margin-top: 2px; }
.tt-cached { font-size: 10px; color: #94a3b8; margin-top: 4px; }
.tt-type { font-size: 11px; color: #94a3b8; margin-top: 2px; }
.tt-hint { margin-top: 8px; font-size: 11px; color: #8391a7; border-top: 1px solid #2d3748; padding-top: 7px; }

/* ── Narrow windows ── */
@media (max-width: 760px) {
  #header { flex-wrap: wrap; height: auto; padding: 6px 10px; gap: 6px 10px; }
  #app-title { display: none; }
  #breadcrumb { order: 3; flex-basis: 100%; }
  #header-right { flex: 1; justify-content: flex-end; flex-wrap: wrap; min-width: 0; }
  #path-form { flex: 1; min-width: 120px; }
  #path-input { width: 100%; }
  #body { flex-direction: column; }
  #treemap-wrap { flex: 1 1 55%; min-height: 160px; }
  #sidebar { width: auto; flex: 1 1 45%; min-height: 0; border-left: none; border-top: 1px solid #1e2535; }
}
</style>
</head>
<body>

<div id="header">
  <span id="logo" aria-hidden="true">🗂</span>
  <span id="app-title">Disk Explorer</span>
  <nav id="breadcrumb" aria-label="Current path"></nav>
  <div id="header-right">
    <form id="path-form" role="search"><label for="path-input" class="sr-only">Jump to path</label><input
      id="path-input" type="text" placeholder="Jump to path…" spellcheck="false" autocomplete="off"/></form>
    <div id="total-size">Size: <span>—</span></div>
    <button class="hdr-btn" id="up-btn" title="Go to parent folder" aria-label="Go to parent folder">↑ Up</button>
    <button class="hdr-btn" id="refresh-btn" title="Refresh current folder">↺ Refresh</button>
    <button class="hdr-btn" id="back-btn" title="Back (Backspace or Left arrow)">← Back</button>
  </div>
</div>

<div id="progress-bar"><div id="progress-fill"></div></div>

<div id="body">
  <div id="treemap-wrap">
    <svg id="treemap" role="group" aria-label="Treemap of folder contents by size"></svg>
    <div id="empty-state">This folder is empty</div>
    <div id="loading-overlay" role="status" aria-live="polite">
      <div class="loading-spinner"></div>
      <div id="loading-title"></div>
      <div id="loading-detail"></div>
    </div>
    <div id="error-overlay" role="alert">
      <div id="error-title">⚠ Could not scan</div>
      <div id="error-detail"></div>
    </div>
    <div id="d3-overlay" role="alert">
      <div id="d3-title">⚠ Treemap unavailable</div>
      <div id="d3-detail">Could not load the D3 library from cdn.jsdelivr.net (offline or CDN blocked).
        The sidebar list still works.</div>
    </div>
  </div>
  <div id="sidebar">
    <div id="sidebar-header">
      <span>Contents</span>
      <div style="display:flex;align-items:center;gap:6px">
        <span id="cache-badge"></span>
        <span id="scan-badge" role="status">
          <span class="scan-dot" id="scan-dot"></span>
          <span id="scan-text"></span>
        </span>
      </div>
    </div>
    <div id="legend" role="group" aria-label="File types in this folder"></div>
    <div id="legend-status" class="sr-only" aria-live="polite"></div>
    <div id="sidebar-list"></div>
  </div>
</div>

<div id="tooltip"></div>

<script>
const API = window.location.origin;
const PALETTE = [
  '#e94560','#f97316','#eab308','#22c55e','#06b6d4',
  '#6366f1','#a855f7','#ec4899','#14b8a6','#84cc16',
  '#f59e0b','#3b82f6','#10b981','#8b5cf6','#ef4444',
];

const MAX_LEAVES = 500;      // treemap cells rendered before aggregating the rest
const MAX_SIDEBAR = 500;     // sidebar rows rendered before showing a "more" note
const OTHER_COLOR = '#475569';

// File-type bucket colors: fixed and consistent across the treemap, sidebar, and legend
// (unlike PALETTE, which is positional and resets per view). Verified with an approximate
// protanopia/deuteranopia/tritanopia simulation (linear RGB transform, not a full physiological
// model) over every pairwise combination, including against OTHER_COLOR below: worst-case
// simulated separation is ~68/255 (video vs. audio under tritanopia is the closest pair),
// versus ~11/255 for an earlier candidate palette that put 'code' and 'audio' at nearly the
// same hue -- so this is a real, sizeable improvement, not a guarantee of perfect
// distinguishability for every color-vision deficiency. 'other' is kept clearly distinct
// (much lighter, different hue) from OTHER_COLOR's darker aggregate-overflow gray.
const TYPE_COLORS = {
  image: '#0ea5e9', video: '#f472b6', audio: '#facc15', archive: '#a78bfa',
  document: '#4ade80', code: '#ea580c', other: '#e2e8f0',
};
const TYPE_LABELS = {
  image: 'Images', video: 'Video', audio: 'Audio', archive: 'Archives',
  document: 'Documents', code: 'Code', other: 'Other',
};
function colorForNode(data, siblingIdx) {
  if (data.fileType) return TYPE_COLORS[data.fileType] || TYPE_COLORS.other;
  return PALETTE[siblingIdx % PALETTE.length];
}

let activeFilterType = null;  // single file-type bucket key being highlighted, or null (one at a time)

let navStack = [];
let currentData = null;
let activeES = null;
let navGen = 0;       // bumped on every navigation; stale stream callbacks bail out
let lastPath = null;  // last requested path, so Refresh can retry a failed first load
let pendingRender = null;    // { fn, handle } for the next animation-frame flush
let resizeTimer = null;
const refreshingPaths = new Set();  // per-row refreshes in flight
let lastMouse = null;               // last pointer position over the treemap (for live tooltip)

function fmt(b) {
  if (!b || b <= 0) return '0 B';
  const u = ['B','KB','MB','GB','TB'];
  let i = Math.min(4, Math.floor(Math.log(b) / Math.log(1024)));
  const shown = k => { const v = b / Math.pow(1024, k); return k >= 2 ? v.toFixed(1) : String(Math.round(v)); };
  // Roll over to the next unit when rounding reaches 1024 (e.g. never show "1024.0 MB")
  if (i < 4 && Number(shown(i)) >= 1024) i++;
  return shown(i) + ' ' + u[i];
}
// Children whose du failed carry size:null plus a status ('timeout' or an
// error); du totals that skipped unreadable subdirs carry status 'partial'.
function isUnknown(c) { return c.size == null; }
function hasSizeInfo(c) { return c.size > 0 || !!c.status; }
function sizeLabel(c) {
  if (c.mount) return 'mount';
  if (isUnknown(c)) return c.status === 'timeout' ? 'timed out' : 'unknown';
  return (c.status ? '≥ ' : '') + fmt(c.size);
}
function statusText(c) {
  if (!c.status) return '';
  if (c.status === 'partial') return 'Partial: some subfolders could not be read';
  if (c.status === 'timeout') return 'Size unknown: du timed out';
  return 'Size unknown: du failed';
}
function sumSizes(list) { return list.reduce((s,c) => s+(c.size||0), 0); }
function pctOf(a, b) { return b ? ((a/b)*100).toFixed(1)+'%' : '—'; }
function timeAgo(ts) {
  const s = Math.max(0, Math.round(Date.now()/1000 - ts));
  if (s < 60)  return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s/60)}m ago`;
  if (s < 86400) return `${Math.floor(s/3600)}h ago`;
  return `${Math.floor(s/86400)}d ago`;
}
function parentOf(p) {
  const t = (p || '').replace(/\/+$/, '');
  const i = t.lastIndexOf('/');
  return i <= 0 ? '/' : t.slice(0, i);
}

// Shared bucket-exemption partition: type-bucket nodes (loose-file groups) are always
// kept whole; only ordinary subfolders/mounts are subject to the size-ranked cap. Used
// by both the treemap's capChildren (folds the rest into an aggregate row) and the
// sidebar's MAX_SIDEBAR truncation (just stops rendering more) so a bucket can never be
// silently dropped from one view while still shown in the other.
function selectVisible(children, maxSlots) {
  const buckets = children.filter(c => c.fileType);
  const others  = children.filter(c => !c.fileType);
  const budget = Math.max(0, maxSlots - buckets.length);
  const sorted = others.slice().sort((a,b) => (b.size||0)-(a.size||0));
  return { buckets, kept: sorted.slice(0, budget), rest: sorted.slice(budget) };
}

// Keep every type bucket plus the largest ordinary items, folding the rest into one
// aggregate entry (reserving one slot for it).
function capChildren(children, max, parentPath) {
  if (children.length <= max) return children;
  const { buckets, kept, rest } = selectVisible(children, max - 1);
  const size = rest.reduce((s,c) => s+(c.size||0), 0);
  const agg = { name: `${rest.length} smaller items`, path: parentPath||'',
                size, isDir: false, aggregate: true };
  // Folded entries with partial/unknown sizes make the aggregate a lower bound.
  if (rest.some(c => c.status)) agg.status = 'partial';
  return rest.length ? buckets.concat(kept, [agg]) : buckets.concat(kept);
}

// ── Render batching ─────────────────────────────────────────
// Coalesce bursts of stream events into at most one render per animation frame.
function scheduleRender(fn) {
  if (pendingRender) { pendingRender.fn = fn; return; }
  const job = { fn, handle: 0 };
  pendingRender = job;
  job.handle = requestAnimationFrame(() => {
    if (pendingRender !== job) return;
    pendingRender = null;
    job.fn();
  });
}
function flushRender() {
  const job = pendingRender;
  if (!job) return;
  pendingRender = null;
  cancelAnimationFrame(job.handle);
  job.fn();
}
function cancelRender() {
  if (!pendingRender) return;
  cancelAnimationFrame(pendingRender.handle);
  pendingRender = null;
}

// ── Streaming ────────────────────────────────────────────────
function startStream(path, force, gen, callbacks) {
  if (activeES) { activeES.close(); activeES = null; }
  const url = `${API}/stream?path=${encodeURIComponent(path)}${force?'&force=1':''}`;
  const es = new EventSource(url);
  activeES = es;
  es.onmessage = e => {
    if (gen !== navGen) { es.close(); return; }
    const msg = JSON.parse(e.data);
    if (msg.type === 'start') callbacks.onStart?.(msg);
    if (msg.type === 'child') callbacks.onChild?.(msg);
    if (msg.type === 'revalidating') callbacks.onRevalidating?.(msg);
    if (msg.type === 'refresh') callbacks.onRefresh?.(msg);
    if (msg.type === 'done')  { es.close(); activeES=null; callbacks.onDone?.(); }
    if (msg.type === 'error') { es.close(); activeES=null; callbacks.onError?.(msg.error); }
  };
  es.onerror = () => {
    if (gen !== navGen) { es.close(); return; }
    es.close(); activeES=null; callbacks.onError?.('Connection lost');
  };
}

// Abort any in-flight scan and reset the scan UI (used by Back / breadcrumb).
function cancelStream() {
  navGen++;
  cancelRender();
  if (activeES) { activeES.close(); activeES = null; }
  hideCacheBadge();
  hideLoading();
  setProgress(-1);
  setScanStatus('', true);
  document.getElementById('refresh-btn').classList.remove('spinning');
}

// ── Navigation ───────────────────────────────────────────────
// onCommit runs in onStart, i.e. only once the server has accepted the path.
function navigate(path, force, onCommit) {
  const gen = ++navGen;
  lastPath = path;
  cancelRender();
  document.getElementById('error-overlay').classList.remove('show');
  hideCacheBadge();
  setProgress(0);
  setScanStatus('scanning…', false);
  showLoading(path);

  let items = [];
  let meta = null, totalDirs = 0, received = 0, refreshing = false;

  startStream(path, force, gen, {
    onStart(msg) {
      if (onCommit) onCommit();
      meta = msg; totalDirs = msg.total_dirs;
      if (msg.from_cache) showCacheBadge(msg.scanned_at);
      else if (msg.total_dirs > 0) {
        setLoadingPhase(`Measuring ${msg.total_dirs} item${msg.total_dirs === 1 ? '' : 's'}`);
        setScanStatus(`0 / ${msg.total_dirs}`, false);
      }
      render({ path: msg.path, name: msg.name, size: 0, children: [] });
    },
    onRevalidating() {
      flushRender();  // show the full cached listing before revalidation starts
      setProgress(-1);
      setScanStatus('refreshing…', false);
    },
    onRefresh(msg) {
      // Stale cache was re-scanned: buffer the fresh listing, swap it in on done
      flushRender();
      meta = msg; totalDirs = msg.total_dirs; received = 0;
      items = []; refreshing = true;
    },
    onChild(item) {
      received++;
      if (received === 1) hideLoading();
      // Insert sorted by size
      let lo = 0, hi = items.length;
      while (lo < hi) { const mid = (lo+hi)>>1; (items[mid].size||0) >= (item.size||0) ? lo=mid+1 : hi=mid; }
      items.splice(lo, 0, item);
      if (refreshing) return;
      const n = received;
      scheduleRender(() => {
        if (gen !== navGen) return;
        // Summed once per frame (not per event) so a row refresh's new size is picked up too.
        renderData({ path: meta.path, name: meta.name, size: sumSizes(items), children: items.slice() });
        if (totalDirs > 0) setProgress(Math.min(100, Math.round((n/totalDirs)*100)));
        setScanStatus(`${n} / ${totalDirs}`, false);
      });
    },
    onDone() {
      hideLoading();
      flushRender();
      if (refreshing) {
        hideCacheBadge();
        renderData({ path: meta.path, name: meta.name, size: sumSizes(items), children: items.slice() });
      }
      if (currentData) { currentData.done = true; updateEmptyState(currentData); }
      setProgress(100);
      setScanStatus('done ✓', true);
      setTimeout(() => { if (gen === navGen) { setProgress(-1); setScanStatus('', true); } }, 1500);
      document.getElementById('refresh-btn').classList.remove('spinning');
    },
    onError(err) {
      hideLoading();
      flushRender();
      setProgress(-1); setScanStatus('', true);
      document.getElementById('error-detail').textContent = err;
      document.getElementById('error-overlay').classList.add('show');
      document.getElementById('refresh-btn').classList.remove('spinning');
    }
  });
}

function goTo(path, force) {
  activeFilterType = null;  // a different folder has an unrelated set of type buckets
  const prev = currentData;
  navigate(path, force, () => { if (prev) navStack.push(prev); });
}

function refreshCurrent(force=true) {
  const path = currentData ? currentData.path : lastPath;
  if (!path) return;
  document.getElementById('refresh-btn').classList.add('spinning');
  navigate(path, force);
}

function refreshPath(path) {
  // If this IS the current path, just refresh current
  if (currentData && currentData.path === path) { refreshCurrent(true); return; }
  if (refreshingPaths.has(path)) return;
  // Otherwise force a rescan of that folder (which also refreshes its cache entry),
  // show a spinner on its row meanwhile, and update its size in the current view.
  const parentPath = currentData ? currentData.path : null;
  refreshingPaths.add(path);
  setRowRefreshState(path, 'spinning');
  let total = 0, partial = false, finished = false;
  const es = new EventSource(`${API}/stream?path=${encodeURIComponent(path)}&force=1`);
  const finish = ok => {
    if (finished) return;
    finished = true;
    es.close();
    refreshingPaths.delete(path);
    setRowRefreshState(path, ok ? '' : 'failed');
    if (!ok || !currentData || currentData.path !== parentPath) return;
    const item = (currentData.children || []).find(c => c.path === path && c.isDir !== false);
    if (!item) return;
    item.size = total;  // shared with the stream's item list, so later child events keep it
    // A child with an unknown or partial size makes the new total a lower bound.
    if (partial) item.status = 'partial'; else delete item.status;
    currentData.children.sort((a, b) => (b.size||0) - (a.size||0));
    currentData.size = sumSizes(currentData.children);
    renderData(currentData);
    if (currentData.done) updateEmptyState(currentData);
  };
  es.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'child') { total += msg.size || 0; if (msg.status) partial = true; }
    if (msg.type === 'done') finish(true);
    if (msg.type === 'error') finish(false);
  };
  es.onerror = () => finish(false);
}

function setRowRefreshState(path, state) {
  document.querySelectorAll('#sidebar-list .sitem-refresh-btn').forEach(btn => {
    if (btn.dataset.path !== path) return;
    btn.classList.toggle('spinning', state === 'spinning');
    btn.classList.toggle('failed', state === 'failed');
    btn.setAttribute('aria-busy', state === 'spinning' ? 'true' : 'false');
    btn.title = state === 'failed' ? 'Refresh failed; click to retry' : 'Refresh this folder';
  });
}

function goUp() {
  if (!currentData || !currentData.path || currentData.path === '/') return;
  goTo(parentOf(currentData.path));
}

function goBack() {
  if (!navStack.length) return;
  activeFilterType = null;  // a different folder has an unrelated set of type buckets
  cancelStream();
  const prev = navStack.pop();
  currentData = prev;
  render(prev);
}

function goToPath() {
  const val = document.getElementById('path-input').value.trim();
  if (!val) return false;
  document.getElementById('path-input').blur();
  navigate(val, false, () => {
    activeFilterType = null;  // a different folder has an unrelated set of type buckets
    navStack = [];
    document.getElementById('path-input').value = '';
  });
  return false;
}

// ── Render ───────────────────────────────────────────────────
function render(data) {
  document.getElementById('error-overlay').classList.remove('show');
  currentData = data;
  renderBreadcrumb();
  renderSidebar(data);
  renderTreemap(data);
  renderLegend(data);
  updateEmptyState(data);
  document.getElementById('back-btn').classList.toggle('visible', navStack.length > 0);
  document.getElementById('up-btn').disabled = !data.path || data.path === '/';
  document.querySelector('#total-size span').textContent = fmt(data.size);
}

// "Empty" is only known once the scan finished (data.done); hide it while children stream in.
function updateEmptyState(data) {
  // Unknown-size (du failed) entries and mount points still count as content.
  const empty = !!data.done && !(data.children || []).some(c => hasSizeInfo(c) || c.mount);
  document.getElementById('empty-state').classList.toggle('show', empty);
  if (!empty) return;
  const list = document.getElementById('sidebar-list');
  list.textContent = '';
  const msg = document.createElement('div');
  msg.className = 'sidebar-empty';
  msg.textContent = 'No items';
  list.appendChild(msg);
}

// Partial update (during streaming) - skip nav stack update
function renderData(data) {
  currentData = data;
  renderSidebar(data);
  renderTreemap(data);
  renderLegend(data);
  document.querySelector('#total-size span').textContent = fmt(data.size);
}

// Build an element with textContent only (never innerHTML) so untrusted names can't inject markup.
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// Crumbs are derived from the current path itself (not click history), so every
// ancestor up to "/" is reachable. Clicks go through goTo(), which cancels any
// in-flight stream (navGen bump) and pushes the current view onto the Back stack.
function renderBreadcrumb() {
  const bc = document.getElementById('breadcrumb');
  bc.textContent = '';
  if (!currentData || !currentData.path) return;
  const parts = currentData.path.split('/').filter(Boolean);
  const chain = [{ name: '/', path: '/' }];
  parts.forEach((part, i) => chain.push({ name: part, path: '/' + parts.slice(0, i + 1).join('/') }));
  chain.forEach((item, i) => {
    const last = i === chain.length-1;
    const btn = el('button', 'crumb' + (last ? ' active' : ''), item.name);
    btn.type = 'button';
    btn.title = item.path;
    if (last) btn.setAttribute('aria-current', 'page');
    else btn.addEventListener('click', () => goTo(item.path));
    bc.appendChild(btn);
    if (!last && i > 0) {
      const sep = document.createElement('span'); sep.className='crumb-sep'; sep.textContent=' › ';
      sep.setAttribute('aria-hidden', 'true');
      bc.appendChild(sep);
    }
  });
}

function setProgress(pct) {
  const bar = document.getElementById('progress-bar');
  const fill = document.getElementById('progress-fill');
  if (pct < 0) { bar.style.opacity='0'; return; }
  bar.style.opacity='1'; fill.style.width=pct+'%';
}

function setScanStatus(txt, done) {
  document.getElementById('scan-text').textContent = txt;
  document.getElementById('scan-dot').classList.toggle('active', !done);
}

// Shown from the moment a folder is opened until its first size arrives, so a slow
// du never looks like a frozen UI.
let loadingTimer = null;
function showLoading(path) {
  hideLoading();
  const started = Date.now();
  const name = path.split('/').filter(Boolean).pop() || path;
  document.getElementById('loading-title').textContent = 'Scanning ' + name + '…';
  const detail = document.getElementById('loading-detail');
  let phase = 'Listing folder';
  const tick = () => {
    const secs = Math.floor((Date.now() - started) / 1000);
    detail.textContent = phase + (secs >= 1 ? ' · ' + secs + 's' : '');
  };
  tick();
  loadingTimer = setInterval(tick, 1000);
  loadingTimer.setPhase = t => { phase = t; tick(); };
  document.getElementById('loading-overlay').classList.add('show');
}
function setLoadingPhase(text) {
  if (loadingTimer) loadingTimer.setPhase(text);
}
function hideLoading() {
  if (loadingTimer) { clearInterval(loadingTimer); loadingTimer = null; }
  document.getElementById('loading-overlay').classList.remove('show');
}

function showCacheBadge(ts) {
  const el = document.getElementById('cache-badge');
  el.textContent = '⚡ cached ' + timeAgo(ts);
  el.classList.add('show');
}
function hideCacheBadge() {
  document.getElementById('cache-badge').classList.remove('show');
}

// ── Treemap ──────────────────────────────────────────────────
function d3Available() {
  const ok = typeof d3 !== 'undefined';
  document.getElementById('d3-overlay').classList.toggle('show', !ok);
  return ok;
}

// Pick black or white label text, whichever contrasts more with the cell fill (WCAG luminance).
function labelColor(fill) {
  const c = d3.rgb(fill);
  const lin = v => { v /= 255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); };
  const L = 0.2126*lin(c.r) + 0.7152*lin(c.g) + 0.0722*lin(c.b);
  return (L + 0.05) / 0.05 >= 1.05 / (L + 0.05) ? '#000000' : '#ffffff';
}

function truncateLabel(name, max) {
  const chars = Array.from(name);  // code points, so surrogate pairs are never split
  return chars.length > max ? chars.slice(0, max-1).join('') + '…' : name;
}

function showTooltip(d, totalVal, x, y) {
  const tooltip = document.getElementById('tooltip');
  tooltip.textContent = '';
  tooltip.append(
    el('div', 'tt-name', d.data.name),
    el('div', 'tt-path', d.data.path),
    el('div', 'tt-size', sizeLabel(d.data)),
    el('div', 'tt-pct', `${pctOf(d.data.size||0, totalVal)} of this view`));
  if (d.data.fileType) tooltip.appendChild(el('div', 'tt-type', TYPE_LABELS[d.data.fileType] || d.data.fileType));
  if (d.data.status) tooltip.appendChild(el('div', 'tt-cached', statusText(d.data)));
  if (d.data.isDir!==false) tooltip.appendChild(el('div', 'tt-hint', 'Click to drill down →'));
  tooltip.style.display='block';
  // Keep the whole tooltip inside the viewport on both axes
  const w = tooltip.offsetWidth, h = tooltip.offsetHeight;
  let left = x + 14, top = y - 10;
  if (left + w > window.innerWidth - 8) left = x - w - 14;
  if (top + h > window.innerHeight - 8) top = window.innerHeight - h - 8;
  tooltip.style.left = Math.max(8, left) + 'px';
  tooltip.style.top = Math.max(8, top) + 'px';
}

// After a re-render (e.g. while streaming) refresh the tooltip for whatever cell is now under the pointer.
function refreshTooltip(totalVal) {
  const tooltip = document.getElementById('tooltip');
  if (tooltip.style.display !== 'block' || !lastMouse) return;
  const hit = document.elementFromPoint(lastMouse.x, lastMouse.y);
  const g = hit && hit.closest ? hit.closest('#treemap g.cell') : null;
  if (!g) { tooltip.style.display='none'; return; }
  showTooltip(d3.select(g).datum(), totalVal, lastMouse.x, lastMouse.y);
}

function renderTreemap(data) {
  if (!d3Available()) return;
  const tooltip = document.getElementById('tooltip');
  const wrap = document.getElementById('treemap-wrap');
  const W = Math.max(0, wrap.clientWidth-20), H = Math.max(0, wrap.clientHeight-20);
  const svg = d3.select('#treemap').attr('width',W).attr('height',H);
  svg.selectAll('*').remove();
  const children = capChildren((data.children||[]).filter(hasSizeInfo), MAX_LEAVES, data.path);
  if (!children.length) { tooltip.style.display='none'; return; }
  // Unknown / zero-size partial entries get a small nominal area so they stay visible.
  const knownTotal = children.reduce((s,c)=>s+(c.size||0),0);
  const stubSize = Math.max(1, knownTotal*0.02);

  const root = d3.hierarchy({name:'root',children})
    .sum(d=>d.children?0:(d.size>0?d.size:stubSize)).sort((a,b)=>b.value-a.value);
  d3.treemap().size([W,H]).paddingOuter(3).paddingInner(2).round(true)(root);

  (root.children||[]).forEach((n,i) => { n.colorIdx = i; });
  const colorOf = d => {
    let n=d; while(n.depth>1) n=n.parent;
    return n.data.aggregate ? OTHER_COLOR : colorForNode(n.data, n.colorIdx);
  };
  const shade = (hex,depth) => { const c=d3.color(hex); return c?c.darker(depth*.35).toString():hex; };
  const totalVal = knownTotal||1;

  const cell = svg.selectAll('g.cell').data(root.leaves()).enter()
    .append('g').attr('class', d=>{
      let cls = 'cell'+(d.data.isDir===false?' file':'')
        +(isUnknown(d.data)?' unknown':(d.data.status?' partial':''));
      if (activeFilterType) {
        if (d.data.fileType && d.data.fileType !== activeFilterType) cls += ' dim-other';
        else if (!d.data.fileType) cls += ' dim-neutral';
      }
      return cls;
    })
    .attr('transform', d=>`translate(${d.x0},${d.y0})`);

  cell.append('rect')
    .attr('width',  d=>Math.max(0,d.x1-d.x0))
    .attr('height', d=>Math.max(0,d.y1-d.y0))
    .attr('fill',   d=>shade(colorOf(d),d.depth-1))
    .attr('rx',3);

  cell.each(function(d) {
    const cw=d.x1-d.x0, ch=d.y1-d.y0, g=d3.select(this);
    // .cell.unknown rects are drawn #334155 by CSS, so pick the label colour for that fill.
    const ink=labelColor(isUnknown(d.data) ? '#334155' : shade(colorOf(d),d.depth-1));
    if (cw>45&&ch>22) {
      const mc=Math.max(3,Math.floor((cw-12)/7.5));
      g.append('text').attr('x',6).attr('y',16)
        .attr('font-size',Math.min(12,Math.max(9,cw/10)))
        .attr('font-weight','600').attr('fill',ink).text(truncateLabel(d.data.name, mc));
    }
    if (cw>55&&ch>38)
      g.append('text').attr('x',6).attr('y',30).attr('font-size',10)
        .attr('fill',ink).text(sizeLabel(d.data));
  });

  cell
    .on('mousemove',(ev,d) => {
      lastMouse = { x: ev.clientX, y: ev.clientY };
      showTooltip(d, totalVal, ev.clientX, ev.clientY);
    })
    .on('mouseleave',()=>{ tooltip.style.display='none'; })
    .on('click',(_,d)=>{ if(d.data.isDir===false)return; tooltip.style.display='none'; goTo(d.data.path); });

  // Keyboard access: folder cells are focusable buttons (Tab, then Enter/Space opens).
  cell.filter(d => d.data.isDir !== false)
    .attr('tabindex', 0)
    .attr('role', 'button')
    .attr('aria-label', d => `Open ${d.data.name}, ${sizeLabel(d.data)}`)
    .on('focus', function(_, d) {
      const r = this.getBoundingClientRect();
      showTooltip(d, totalVal, r.left + r.width / 2, r.top + r.height / 2);
    })
    .on('blur', () => { tooltip.style.display='none'; })
    .on('keydown', (ev, d) => {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      ev.preventDefault();
      tooltip.style.display='none';
      goTo(d.data.path);
    });

  refreshTooltip(totalVal);
}

// ── Sidebar ──────────────────────────────────────────────────
function renderSidebar(data) {
  const list = document.getElementById('sidebar-list');
  list.textContent = '';
  // Mount points have unknown size (0) but are still listed so they can be opened.
  const all = (data.children||[]).filter(c=>hasSizeInfo(c)||c.mount);
  if (!all.length) return;
  // Type buckets are exempt from the MAX_SIDEBAR cap (selectVisible), so a bucket can
  // never be dropped from the sidebar while still shown in the treemap (see capChildren).
  let items = all, hidden = [];
  if (all.length > MAX_SIDEBAR) {
    const sel = selectVisible(all, MAX_SIDEBAR);
    items = sel.buckets.concat(sel.kept).sort((a,b) => (b.size||0)-(a.size||0));
    hidden = sel.rest;
  }
  const maxSz = items[0].size||1;

  items.forEach((item,i) => {
    const color = colorForNode(item, i);
    const isDir = item.isDir!==false;
    let cls = 'sitem'+(isDir?'':' file')+(item.status?' unknown':'')+(item.mount?' mount':'');
    if (activeFilterType) {
      if (item.fileType && item.fileType !== activeFilterType) cls += ' dim-other';
      else if (!item.fileType) cls += ' dim-neutral';
    }
    const div = el('div', cls);
    if (item.status) div.title = statusText(item);
    div.style.animationDelay = Math.min(i*20,200)+'ms';

    // Folders get a real <button> (keyboard + screen reader); the refresh button is a sibling, not nested.
    const main = el(isDir ? 'button' : 'div', 'sitem-main');
    if (isDir) {
      main.type = 'button';
      main.setAttribute('aria-label', `Open ${item.name}, ${sizeLabel(item)}`);
      main.addEventListener('click', () => goTo(item.path));
    }
    const top = el('div', 'sitem-top');
    const dot = el('div', 'sitem-dot'); dot.style.background = color;
    const name = el('div', 'sitem-name', item.name); name.title = item.path;
    top.append(dot, name, el('div', 'sitem-size', sizeLabel(item)));
    const barWrap = el('div', 'sitem-bar-wrap');
    const bar = el('div', 'sitem-bar');
    bar.style.background = color;
    bar.style.width = Math.max(1,((item.size||0)/maxSz)*100)+'%';
    barWrap.appendChild(bar);
    main.append(top, barWrap);
    div.appendChild(main);

    if (isDir && !item.mount) {  // mounts stay out of totals, so there is no size to refresh
      const actions = el('div', 'sitem-actions');
      const btn = el('button', 'sitem-refresh-btn', '↺');
      btn.type = 'button';
      btn.dataset.path = item.path;
      btn.setAttribute('aria-label', `Refresh ${item.name}`);
      btn.title = 'Refresh this folder';
      if (refreshingPaths.has(item.path)) { btn.classList.add('spinning'); btn.setAttribute('aria-busy', 'true'); }
      btn.addEventListener('click', e => { e.stopPropagation(); refreshPath(item.path); });
      actions.appendChild(btn);
      div.appendChild(actions);
    }
    list.appendChild(div);
  });

  if (hidden.length) {
    const note = document.createElement('div');
    note.className = 'sitem file';
    note.style.animation = 'none';
    const main = el('div', 'sitem-main');
    const top = el('div', 'sitem-top');
    top.append(el('div', 'sitem-name', `+ ${hidden.length} smaller items not shown`),
               el('div', 'sitem-size', fmt(sumSizes(hidden))));
    main.appendChild(top);
    note.appendChild(main);
    list.appendChild(note);
  }
}

// ── Legend (file-type coloring & filtering) ───────────────────
// View-scoped: totals are for the loose files in the current folder only (subfolders
// aren't decomposed by type), and the legend states that explicitly in its caption.
function renderLegend(data) {
  const box = document.getElementById('legend');
  box.textContent = '';
  const totals = {};
  (data.children || []).forEach(c => { if (c.fileType) totals[c.fileType] = (totals[c.fileType]||0) + (c.size||0); });
  const present = Object.keys(totals).filter(k => totals[k] > 0);
  if (!present.length) { box.style.display = 'none'; return; }
  box.style.display = 'flex';
  box.appendChild(el('div', 'legend-caption', 'Sizes shown are for loose files in this folder only'));
  present.sort((a,b) => totals[b]-totals[a]).forEach(bucket => {
    const item = el('div', 'legend-item');
    item.setAttribute('role', 'button');
    item.setAttribute('tabindex', '0');
    item.setAttribute('aria-pressed', String(activeFilterType === bucket));
    item.setAttribute('aria-label', `${TYPE_LABELS[bucket] || bucket}, ${fmt(totals[bucket])}`);
    const swatch = el('span', 'legend-swatch'); swatch.style.background = TYPE_COLORS[bucket] || TYPE_COLORS.other;
    item.append(swatch, el('span', 'legend-label', TYPE_LABELS[bucket] || bucket),
                el('span', 'legend-size', fmt(totals[bucket])));
    item.addEventListener('click', () => toggleFilter(bucket));
    item.addEventListener('keydown', ev => {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      ev.preventDefault();  // Space would otherwise also scroll the sidebar/page
      toggleFilter(bucket);
    });
    box.appendChild(item);
  });
}

function announceFilter() {
  document.getElementById('legend-status').textContent = activeFilterType
    ? `Showing ${TYPE_LABELS[activeFilterType] || activeFilterType} files only`
    : 'Filter cleared';
}

// Single active filter type at a time: clicking a second entry replaces it, clicking
// the active entry again (or Escape) clears it.
function toggleFilter(bucket) {
  activeFilterType = (activeFilterType === bucket) ? null : bucket;
  announceFilter();
  if (currentData) { renderTreemap(currentData); renderSidebar(currentData); renderLegend(currentData); }
}

// ── Init ─────────────────────────────────────────────────────
document.getElementById('back-btn').addEventListener('click', goBack);
document.getElementById('up-btn').addEventListener('click', goUp);
document.getElementById('refresh-btn').addEventListener('click', () => refreshCurrent(true));
document.getElementById('path-form').addEventListener('submit', e => { e.preventDefault(); goToPath(); });
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (currentData) renderTreemap(currentData); }, 150);
});
window.addEventListener('keydown', e => {
  // Never hijack browser/OS shortcuts (Cmd/Ctrl/Alt/Shift+Arrow etc.) or typing in form fields
  if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
  const t = e.target;
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
  if (e.key==='Backspace'||e.key==='ArrowLeft') { e.preventDefault(); goBack(); }
  if (e.key==='Escape' && activeFilterType) { e.preventDefault(); toggleFilter(activeFilterType); }
});

d3Available();
navigate(%%DEFAULT_PATH%%, false);
</script>
</body>
</html>
"""


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def _allowed(self):
        # Block DNS rebinding (Host) and cross-site requests (Origin / Sec-Fetch-Site).
        port = self.server.server_address[1]
        hosts = (f'localhost:{port}', f'127.0.0.1:{port}', f'[::1]:{port}')
        if self.headers.get('Host') not in hosts:
            return False
        origin = self.headers.get('Origin')
        if origin and origin not in tuple(f'http://{h}' for h in hosts):
            return False
        return self.headers.get('Sec-Fetch-Site') not in ('cross-site', 'same-site')

    def do_GET(self):
        if not self._allowed():
            self.send_error(403)
            return

        parsed = urlparse(self.path)

        if parsed.path == '/':
            # Escape every "<" so the path can't close the script ("</script>") or open a comment ("<!--").
            js_path = json.dumps(_default_path).replace("<", "\\u003c")
            body = HTML.replace("%%DEFAULT_PATH%%", js_path).encode("utf-8")
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path == '/stream':
            params = parse_qs(parsed.query, errors='surrogateescape')
            path = norm_path(params.get('path', ['~'])[0])
            force = params.get('force', ['0'])[0] == '1'

            self.send_response(200)
            self.send_header('Content-Type',  'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()

            stop = threading.Event()

            def write_event(data):
                if stop.is_set():
                    return
                try:
                    if data is None:  # SSE comment, ignored by EventSource
                        self.wfile.write(b": keepalive\n\n")
                    else:
                        self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
                    self.wfile.flush()
                except OSError:  # BrokenPipe, ConnectionReset, etc.
                    stop.set()

            try:
                stream_directory(path, write_event, force=force, stop=stop)
            except Exception as e:
                write_event({'type': 'error', 'error': 'Scan failed: %s' % (e or type(e).__name__)})

        elif parsed.path == '/invalidate':
            self.send_response(405)  # state-changing: POST only
            self.send_header('Allow', 'POST')
            self.end_headers()

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if not self._allowed():
            self.send_error(403)
            return

        parsed = urlparse(self.path)
        if parsed.path == '/invalidate':
            params = parse_qs(parsed.query, errors='surrogateescape')
            path = params.get('path', [''])[0]
            if path:
                # Same resolution as stream_directory, so '/' on macOS clears the Data volume entry.
                cache_delete(_resolve_scan_path(path))
            self.send_response(200)
        else:
            self.send_response(404)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, format, *args):
        if _verbose:
            super().log_message(format, *args)


class Server(ThreadingHTTPServer):
    daemon_threads = True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _default_path, _verbose, DU_TIMEOUT
    parser = argparse.ArgumentParser(prog="disko", description="disko - interactive disk usage explorer")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    parser.add_argument("--path", type=str, default=None, help="Starting path (default: your home directory)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser")
    parser.add_argument("--du-timeout", type=float, default=DU_TIMEOUT,
                        help="Seconds before a single du call is abandoned (default: %d)" % DU_TIMEOUT)
    parser.add_argument("--verbose", action="store_true", help="Log HTTP requests to stderr")
    args = parser.parse_args()
    if args.du_timeout <= 0:
        parser.error("--du-timeout must be positive")
    DU_TIMEOUT = args.du_timeout
    _verbose = args.verbose
    if args.path:
        _default_path = norm_path(args.path)
    else:
        _default_path = os.path.expanduser("~")
    port = args.port
    try:
        server = Server(("127.0.0.1", port), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"  Error: port {port} is already in use (another disko instance?).\n"
                  f"  Try a different port, e.g.: disko --port {port + 1}", file=sys.stderr)
        else:
            print(f"  Error: could not start server on localhost:{port}: {e.strerror or e}", file=sys.stderr)
        sys.exit(1)
    cache_load()
    url = f"http://localhost:{port}"
    print(f"  disko v{__version__} -> {url}")
    print(f"  Cache: {CACHE_FILE}")
    print("  Ctrl+C to stop")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    def on_sigterm(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down...")
    finally:
        stop_prefetch()
        server.server_close()
    print("  Stopped.")


if __name__ == "__main__":
    main()
