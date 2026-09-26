#!/usr/bin/env python3
"""
disko -- interactive disk usage explorer
Runs a local web server with a real-time D3.js treemap of your filesystem.
Usage: python3 disko.py [--port PORT] [--path PATH] [--no-browser]
"""

import argparse
import concurrent.futures
import json
import os
import platform
import subprocess
import tempfile
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

__version__ = "1.0.0"
_default_path = "/"

SCAN_WORKERS = 12   # parallel du workers per scan
PREFETCH_WORKERS = 4  # background prefetch workers
CACHE_FILE = os.path.expanduser(os.environ.get('DISKO_CACHE') or '~/.disko_cache.json')
PREFETCH_TOP_N = 10  # prefetch top-N largest subdirs after each scan
PREFETCH_MAX_DEPTH = 1  # how many levels below a scanned dir to prefetch
CACHE_TTL = 300  # seconds; older cache hits are served, then re-scanned and re-streamed
HEARTBEAT_SECS = 2  # SSE keepalive interval while waiting on du (detects disconnects)
DU_TIMEOUT = 300     # seconds per du call (overridable via --du-timeout)

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


def cache_get(path: str):
    with _cache_lock:
        return _cache.get(_cache_key(path))


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
        if existing and existing.get('scanned_at', 0) > scanned_at:
            return False
        _cache[key] = {'children': children, 'scanned_at': scanned_at}
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
        if not entry or entry.get('scanned_at', 0) > scanned_at:
            return
        item = next((c for c in entry['children']
                     if c.get('isDir') and _cache_key(c['path']) == child), None)
        new_status = 'partial' if partial else None
        if item is None or (item.get('size') == new_size and item.get('status') == new_status):
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


def du_single(path: str):
    """Return (size_bytes_or_None, status).

    status is None on success, 'partial' when du exited non-zero but still
    reported a total (e.g. unreadable subdirs), 'timeout', or an error string
    when no size could be determined (size is then None).
    """
    try:
        r = subprocess.run(["du", "-sk", "-x", "--", path],
                           capture_output=True, timeout=DU_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, 'timeout'
    except OSError as e:
        return None, str(e) or 'du failed'
    try:
        kb = int(r.stdout.split(b'\t', 1)[0])
    except ValueError:
        err = r.stderr.decode('utf-8', 'replace').strip().splitlines()
        return None, (err[0][:200] if err else 'du failed (exit %d)' % r.returncode)
    return kb * 1024, ('partial' if r.returncode else None)


_du_slots = threading.BoundedSemaphore(SCAN_WORKERS)  # global cap on concurrent du processes


def du_bounded(path: str, stop=None):
    """du_single, limited by the global du semaphore; skipped if stop is set.

    Returns du_single's (size_or_None, status) tuple.
    """
    with _du_slots:
        if stop is not None and stop.is_set():
            return None, 'cancelled'
        return du_single(path)


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


def scan_to_list(path: str):
    """Scan path and return list of child dicts, or None if it can't be read."""
    path = _resolve_scan_path(path)
    try:
        entries = list(os.scandir(path))
    except OSError:
        return None

    dirs = [e for e in entries if e.is_dir(follow_symlinks=False)]
    files = [e for e in entries if not e.is_dir(follow_symlinks=False)]

    file_total = sum(_alloc_size(e) for e in files)
    dirs, mounts = _split_mounts(path, dirs)

    children = [_mount_child(e) for e in mounts]
    with concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futures = {ex.submit(du_bounded, e.path): e for e in dirs}
        for fut in concurrent.futures.as_completed(futures):
            children.append(_dir_child(futures[fut], fut.result()))

    if file_total > 0:
        children.append({
            'name': '(loose files)',
            'path': path,
            'size': file_total,
            'isDir': False,
        })

    children.sort(key=_size_key)
    return children


# ── Background prefetch pool ─────────────────────────────────────────────────

_prefetch_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=PREFETCH_WORKERS, thread_name_prefix='prefetch')
_prefetching: set = set()
_prefetch_lock = threading.Lock()


def submit_scan(path: str, depth: int = 0) -> bool:
    """Scan path in the background (deduped against in-flight scans), then
    prefetch `depth` levels of its children. Returns False if already running."""
    with _prefetch_lock:
        if path in _prefetching:
            return False
        _prefetching.add(path)
    try:
        _prefetch_pool.submit(_do_prefetch, path, depth)
    except RuntimeError:  # pool shut down (interpreter exiting)
        with _prefetch_lock:
            _prefetching.discard(path)
        return False
    return True


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
        children = scan_to_list(path)
        if children is None:
            return
        if _cacheable(children):
            cache_set(path, children, scanned_at=started)
        schedule_prefetch(children, depth)
    finally:
        with _prefetch_lock:
            _prefetching.discard(path)


def _revalidate(path: str, started: float, result: dict):
    """Rescan a stale cached dir for stream_directory (runs in a worker thread).

    Sets result['fresh'] to the listing to stream ({'children', 'scanned_at'}),
    or leaves it unset if the dir can't be read. Registers in _prefetching so a
    concurrent prefetch of the same path is skipped."""
    with _prefetch_lock:
        owned = path not in _prefetching
        _prefetching.add(path)
    try:
        children = scan_to_list(path)
        if children is None:
            return
        if _cacheable(children):
            cache_set(path, children, scanned_at=started)
            # A newer scan may have won the race; stream whatever the cache now holds.
            result['fresh'] = cache_get(path) or {'children': children, 'scanned_at': started}
        else:
            # Unknown sizes aren't cached (so they get retried) but are still shown.
            result['fresh'] = {'children': children, 'scanned_at': started}
        schedule_prefetch(result['fresh']['children'])
    finally:
        if owned:
            with _prefetch_lock:
                _prefetching.discard(path)


# ── Streaming (SSE) ───────────────────────────────────────────────────────────

def stream_directory(path: str, write_event, force: bool = False, stop=None):
    if stop is None:
        stop = threading.Event()
    path = _resolve_scan_path(path)
    if not os.path.isdir(path):
        write_event({'type': 'error', 'error': 'Not a directory or not found'})
        return

    cached = cache_get(path) if not force else None

    if cached:
        # Serve cache immediately
        write_event({
            'type': 'start',
            'path': path,
            'name': os.path.basename(path) or path,
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
        started = time.time()
        result = {}
        worker = threading.Thread(target=_revalidate, args=(path, started, result), daemon=True)
        worker.start()
        while worker.is_alive() and not stop.is_set():
            worker.join(HEARTBEAT_SECS)
            if worker.is_alive():
                write_event(None)  # heartbeat: detects a disconnected client
        if stop.is_set():
            return
        fresh = result.get('fresh')
        if fresh is None:  # dir became unreadable: keep showing the cached listing
            write_event({'type': 'done'})
            return
        write_event({
            'type': 'refresh',
            'path': path,
            'name': os.path.basename(path) or path,
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
        entries = list(os.scandir(path))
    except OSError as e:
        write_event({'type': 'error', 'error': str(e)})
        return

    dirs = [e for e in entries if e.is_dir(follow_symlinks=False)]
    files = [e for e in entries if not e.is_dir(follow_symlinks=False)]
    file_total = sum(_alloc_size(e) for e in files)
    total_dirs = len(dirs)
    dirs, mounts = _split_mounts(path, dirs)

    write_event({
        'type': 'start',
        'path': path,
        'name': os.path.basename(path) or path,
        'total_dirs': total_dirs,
        'from_cache': False,
    })

    # Mount points are not du'ed (other filesystem); emit them first.
    collected = [_mount_child(e) for e in mounts]
    for child in collected:
        write_event({'type': 'child', **child})
    # Not a `with` block: its exit would wait for every running du after a disconnect
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS)
    futures = {ex.submit(du_bounded, e.path, stop): e for e in dirs}
    pending = set(futures)
    try:
        while pending and not stop.is_set():
            done, pending = concurrent.futures.wait(
                pending, timeout=HEARTBEAT_SECS,
                return_when=concurrent.futures.FIRST_COMPLETED)
            if not done:
                write_event(None)  # heartbeat: detects a disconnected client
            for fut in done:
                child = _dir_child(futures[fut], fut.result())
                collected.append(child)
                write_event({'type': 'child', **child})
    finally:
        for f in pending:  # manual cancel_futures (Python 3.8 compatible)
            f.cancel()
        ex.shutdown(wait=False)
    if stop.is_set():
        return  # client went away: don't cache partial results

    if file_total > 0:
        loose = {'name': '(loose files)', 'path': path, 'size': file_total, 'isDir': False}
        collected.append(loose)
        write_event({'type': 'child', **loose})

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
<script src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>
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
  font-size: 12px; color: #64748b; cursor: pointer;
  padding: 3px 6px; border-radius: 4px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 160px;
  transition: all .15s;
}
.crumb:hover { background: #1e2535; color: #cbd5e1; }
.crumb.active { color: #f1f5f9; font-weight: 500; cursor: default; }
.crumb.active:hover { background: transparent; }
.crumb-sep { color: #2d3748; font-size: 13px; flex-shrink: 0; }

#header-right { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
#path-form { display: flex; }
#path-input {
  font-size: 12px; padding: 4px 10px; border-radius: 6px;
  background: #1e2535; border: 1px solid #2d3748; color: #94a3b8;
  outline: none; width: 230px; font-family: 'SF Mono', monospace; transition: border-color .15s;
}
#path-input:focus { border-color: #e94560; color: #f1f5f9; }
#total-size { font-size: 12px; color: #64748b; white-space: nowrap; }
#total-size span { color: #e94560; font-weight: 700; }

.hdr-btn {
  font-size: 12px; padding: 4px 10px; border-radius: 6px;
  background: #1e2535; border: 1px solid #2d3748; color: #94a3b8;
  cursor: pointer; transition: all .15s; white-space: nowrap;
}
.hdr-btn:hover { background: #2d3748; color: #f1f5f9; }
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
.cell text { pointer-events: none; }
.cell.partial rect { stroke: #cbd5e1; stroke-dasharray: 4 3; }
.cell.unknown rect { fill: #334155; stroke: #64748b; stroke-dasharray: 4 3; }

/* ── Sidebar ── */
#sidebar {
  width: 270px; flex-shrink: 0; background: #161b26;
  border-left: 1px solid #1e2535; display: flex; flex-direction: column; overflow: hidden;
}
#sidebar-header {
  padding: 10px 16px 9px; font-size: 11px; font-weight: 600; color: #475569;
  text-transform: uppercase; letter-spacing: .8px; border-bottom: 1px solid #1e2535;
  display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; gap: 6px;
}
#scan-badge {
  display: flex; align-items: center; gap: 5px;
  font-size: 10px; color: #64748b; font-weight: 400; letter-spacing: 0;
}
.scan-dot {
  width: 6px; height: 6px; border-radius: 50%; background: #e94560;
  animation: pulse 1s ease-in-out infinite; display: none; flex-shrink: 0;
}
.scan-dot.active { display: inline-block; }
@keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.3;transform:scale(.65)} }

#cache-badge {
  font-size: 10px; color: #334155; padding: 2px 6px; border-radius: 4px;
  background: #1e2535; border: 1px solid #2d3748; white-space: nowrap; display: none;
}
#cache-badge.show { display: block; }

#sidebar-list { overflow-y: auto; flex: 1; }
#sidebar-list::-webkit-scrollbar { width: 4px; }
#sidebar-list::-webkit-scrollbar-thumb { background: #2d3748; border-radius: 2px; }

.sitem {
  display: flex; flex-direction: column; padding: 8px 16px 7px;
  border-bottom: 1px solid #1a2030; cursor: pointer; transition: background .1s; gap: 4px;
  animation: fadeSlide .18s ease-out both;
}
@keyframes fadeSlide { from{opacity:0;transform:translateX(8px)} to{opacity:1;transform:translateX(0)} }
.sitem:hover { background: #1e2535; }
.sitem.file { cursor: default; }
.sitem.unknown .sitem-size { color: #f59e0b; font-style: italic; }
.sitem.mount .sitem-name { font-style: italic; }
.sitem.mount .sitem-bar-wrap { visibility: hidden; }
.sitem-top { display: flex; align-items: center; gap: 7px; }
.sitem-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.sitem-name { flex: 1; font-size: 12px; color: #cbd5e1;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sitem-size { font-size: 11px; color: #64748b; white-space: nowrap; }
.sitem-actions { display: flex; align-items: center; gap: 3px; opacity: 0; transition: opacity .15s; }
.sitem:hover .sitem-actions { opacity: 1; }
.sitem-cached { font-size: 9px; color: #1d4ed8; background: #1e3a5f; padding: 1px 4px; border-radius: 3px; }
.sitem-refresh-btn {
  font-size: 11px; padding: 1px 5px; border-radius: 3px; cursor: pointer;
  color: #475569; background: transparent; border: none;
  transition: all .15s; line-height: 1.4;
}
.sitem-refresh-btn:hover { background: #2d3748; color: #e94560; }
.sitem-bar-wrap { height: 2px; background: #1e2535; border-radius: 1px; margin-left: 15px; width: calc(100% - 15px); }
.sitem-bar { height: 100%; border-radius: 1px; opacity: .45; }

/* ── Error ── */
#error-overlay {
  position: absolute; inset: 0; display: none;
  align-items: center; justify-content: center; flex-direction: column; gap: 8px;
  background: rgba(17,19,24,.7);
}
#error-overlay.show { display: flex; }
#error-title { color: #e94560; font-size: 15px; font-weight: 600; }
#error-detail { color: #475569; font-size: 12px; }

/* ── Tooltip ── */
#tooltip {
  position: fixed; z-index: 999; background: #1e2535; border: 1px solid #2d3748;
  border-radius: 9px; padding: 10px 14px; font-size: 13px; pointer-events: none;
  display: none; box-shadow: 0 8px 32px rgba(0,0,0,.5); max-width: 300px;
}
.tt-name { font-weight: 600; color: #f1f5f9; margin-bottom: 3px; word-break: break-word; }
.tt-path { font-size: 11px; color: #475569; margin-bottom: 8px; word-break: break-all; }
.tt-size { font-size: 18px; font-weight: 700; color: #e94560; }
.tt-pct  { font-size: 11px; color: #64748b; margin-top: 2px; }
.tt-cached { font-size: 10px; color: #334155; margin-top: 4px; }
.tt-hint { margin-top: 8px; font-size: 11px; color: #334155; border-top: 1px solid #2d3748; padding-top: 7px; }
</style>
</head>
<body>

<div id="header">
  <span id="logo">🗂</span>
  <span id="app-title">Disk Explorer</span>
  <div id="breadcrumb"></div>
  <div id="header-right">
    <form id="path-form"><input id="path-input" type="text"
      placeholder="Jump to path…" spellcheck="false" autocomplete="off"/></form>
    <div id="total-size">Size: <span>—</span></div>
    <button class="hdr-btn" id="refresh-btn" title="Refresh current folder">↺ Refresh</button>
    <button class="hdr-btn" id="back-btn">← Back</button>
  </div>
</div>

<div id="progress-bar"><div id="progress-fill"></div></div>

<div id="body">
  <div id="treemap-wrap">
    <svg id="treemap"></svg>
    <div id="error-overlay">
      <div id="error-title">⚠ Could not scan</div>
      <div id="error-detail"></div>
    </div>
  </div>
  <div id="sidebar">
    <div id="sidebar-header">
      <span>Contents</span>
      <div style="display:flex;align-items:center;gap:6px">
        <span id="cache-badge"></span>
        <span id="scan-badge">
          <span class="scan-dot" id="scan-dot"></span>
          <span id="scan-text"></span>
        </span>
      </div>
    </div>
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

let navStack = [];
let currentData = null;
let activeES = null;
let navGen = 0;       // bumped on every navigation; stale stream callbacks bail out
let lastPath = null;  // last requested path, so Refresh can retry a failed first load
let pendingRender = null;    // { fn, handle } for the next animation-frame flush
let resizeTimer = null;

function fmt(b) {
  if (!b || b <= 0) return '0 B';
  const u = ['B','KB','MB','GB','TB'];
  const i = Math.min(4, Math.floor(Math.log(b) / Math.log(1024)));
  const v = b / Math.pow(1024, i);
  return (i >= 2 ? v.toFixed(1) : Math.round(v)) + ' ' + u[i];
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
function pctOf(a, b) { return b ? ((a/b)*100).toFixed(1)+'%' : '—'; }
function timeAgo(ts) {
  const s = Math.round(Date.now()/1000 - ts);
  if (s < 60)  return `${s}s ago`;
  if (s < 3600) return `${Math.round(s/60)}m ago`;
  return `${Math.round(s/3600)}h ago`;
}

// Keep the largest (max-1) items and fold the rest into one aggregate entry.
function capChildren(children, max, parentPath) {
  if (children.length <= max) return children;
  const sorted = children.slice().sort((a,b) => b.size-a.size);
  const kept = sorted.slice(0, max-1), rest = sorted.slice(max-1);
  const size = rest.reduce((s,c) => s+c.size, 0);
  kept.push({ name: `${rest.length} smaller items`, path: parentPath||'',
              size, isDir: false, aggregate: true });
  return kept;
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
  if (activeES) { activeES.close(); activeES = null; }
  hideCacheBadge();
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

  let items = [];
  let meta = null, totalDirs = 0, received = 0, total = 0, refreshing = false;

  startStream(path, force, gen, {
    onStart(msg) {
      if (onCommit) onCommit();
      meta = msg; totalDirs = msg.total_dirs;
      if (msg.from_cache) showCacheBadge(msg.scanned_at);
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
      items = []; total = 0; refreshing = true;
    },
    onChild(item) {
      received++;
      // Insert sorted by size
      let lo = 0, hi = items.length;
      while (lo < hi) { const mid = (lo+hi)>>1; (items[mid].size||0) >= (item.size||0) ? lo=mid+1 : hi=mid; }
      items.splice(lo, 0, item);
      total += (item.size||0);
      if (refreshing) return;
      const n = received;
      scheduleRender(() => {
        if (gen !== navGen) return;
        renderData({ path: meta.path, name: meta.name, size: total, children: items.slice() });
        if (totalDirs > 0) setProgress(Math.round((n/totalDirs)*100));
        setScanStatus(`${n} / ${totalDirs}`, false);
      });
    },
    onDone() {
      flushRender();
      if (refreshing) {
        hideCacheBadge();
        renderData({ path: meta.path, name: meta.name, size: total, children: items.slice() });
      }
      setProgress(100);
      setScanStatus('done ✓', true);
      setTimeout(() => { if (gen === navGen) { setProgress(-1); setScanStatus('', true); } }, 1500);
      document.getElementById('refresh-btn').classList.remove('spinning');
    },
    onError(err) {
      flushRender();
      setProgress(-1); setScanStatus('', true);
      document.getElementById('error-detail').textContent = err;
      document.getElementById('error-overlay').classList.add('show');
      document.getElementById('refresh-btn').classList.remove('spinning');
    }
  });
}

function goTo(path, force) {
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
  // Otherwise just invalidate cache silently (server handles it on next visit)
  fetch(`${API}/invalidate?path=${encodeURIComponent(path)}`).catch(()=>{});
}

function goBack() {
  if (!navStack.length) return;
  cancelStream();
  const prev = navStack.pop();
  currentData = prev;
  renderFull(prev);
}

function goToPath() {
  const val = document.getElementById('path-input').value.trim();
  if (!val) return false;
  document.getElementById('path-input').blur();
  navigate(val, false, () => {
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
  renderTreemap(data);
  renderSidebar(data);
  document.getElementById('back-btn').classList.toggle('visible', navStack.length > 0);
  document.querySelector('#total-size span').textContent = fmt(data.size);
}

// Partial update (during streaming) - skip nav stack update
function renderData(data) {
  currentData = data;
  renderTreemap(data);
  renderSidebar(data);
  document.querySelector('#total-size span').textContent = fmt(data.size);
}

function renderFull(data) {
  render(data);
  renderBreadcrumb();
  document.getElementById('back-btn').classList.toggle('visible', navStack.length > 0);
}

// Build an element with textContent only (never innerHTML) so untrusted names can't inject markup.
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function renderBreadcrumb() {
  const bc = document.getElementById('breadcrumb');
  bc.innerHTML = '';
  const chain = [...navStack, currentData].filter(Boolean);
  chain.forEach((item, i) => {
    const name = item.name || (item.path||'').split('/').pop() || item.path;
    const span = document.createElement('span');
    span.className = 'crumb' + (i === chain.length-1 ? ' active' : '');
    span.title = item.path;
    span.textContent = name;
    if (i < chain.length-1) span.onclick = () => { cancelStream(); navStack = navStack.slice(0,i); renderFull(item); };
    bc.appendChild(span);
    if (i < chain.length-1) {
      const sep = document.createElement('span'); sep.className='crumb-sep'; sep.textContent=' › ';
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

function showCacheBadge(ts) {
  const el = document.getElementById('cache-badge');
  el.textContent = '⚡ cached ' + timeAgo(ts);
  el.classList.add('show');
}
function hideCacheBadge() {
  document.getElementById('cache-badge').classList.remove('show');
}

// ── Treemap ──────────────────────────────────────────────────
function renderTreemap(data) {
  const wrap = document.getElementById('treemap-wrap');
  const W = wrap.clientWidth-20, H = wrap.clientHeight-20;
  const svg = d3.select('#treemap').attr('width',W).attr('height',H);
  svg.selectAll('*').remove();
  const children = capChildren((data.children||[]).filter(hasSizeInfo), MAX_LEAVES, data.path);
  if (!children.length) return;
  // Unknown / zero-size partial entries get a small nominal area so they stay visible.
  const knownTotal = children.reduce((s,c)=>s+(c.size||0),0);
  const stubSize = Math.max(1, knownTotal*0.02);

  const root = d3.hierarchy({name:'root',children})
    .sum(d=>d.children?0:(d.size>0?d.size:stubSize)).sort((a,b)=>b.value-a.value);
  d3.treemap().size([W,H]).paddingOuter(3).paddingInner(2).round(true)(root);

  (root.children||[]).forEach((n,i) => { n.colorIdx = i; });
  const colorOf = d => {
    let n=d; while(n.depth>1) n=n.parent;
    return n.data.aggregate ? OTHER_COLOR : PALETTE[n.colorIdx%PALETTE.length];
  };
  const shade = (hex,depth) => { const c=d3.color(hex); return c?c.darker(depth*.35).toString():hex; };
  const tooltip = document.getElementById('tooltip');
  const totalVal = knownTotal||1;

  const cell = svg.selectAll('g.cell').data(root.leaves()).enter()
    .append('g').attr('class', d=>'cell'+(d.data.isDir===false?' file':'')
      +(isUnknown(d.data)?' unknown':(d.data.status?' partial':'')))
    .attr('transform', d=>`translate(${d.x0},${d.y0})`);

  cell.append('rect')
    .attr('width',  d=>Math.max(0,d.x1-d.x0))
    .attr('height', d=>Math.max(0,d.y1-d.y0))
    .attr('fill',   d=>shade(colorOf(d),d.depth-1))
    .attr('rx',3);

  cell.each(function(d) {
    const cw=d.x1-d.x0, ch=d.y1-d.y0, g=d3.select(this);
    if (cw>45&&ch>22) {
      const mc=Math.max(3,Math.floor((cw-12)/7.5));
      const lbl=d.data.name.length>mc?d.data.name.slice(0,mc-1)+'…':d.data.name;
      g.append('text').attr('x',6).attr('y',16)
        .attr('font-size',Math.min(12,Math.max(9,cw/10)))
        .attr('font-weight','500').attr('fill','rgba(255,255,255,.88)').text(lbl);
    }
    if (cw>55&&ch>38)
      g.append('text').attr('x',6).attr('y',30).attr('font-size',10)
        .attr('fill','rgba(255,255,255,.5)').text(sizeLabel(d.data));
  });

  cell
    .on('mousemove',(ev,d) => {
      tooltip.style.display='block';
      tooltip.style.left=Math.min(ev.clientX+14,window.innerWidth-320)+'px';
      tooltip.style.top=Math.max(10,ev.clientY-10)+'px';
      tooltip.textContent = '';
      tooltip.append(
        el('div','tt-name',d.data.name),
        el('div','tt-path',d.data.path),
        el('div','tt-size',sizeLabel(d.data)),
        el('div','tt-pct',pctOf(d.data.size||0,totalVal)+' of this view'));
      if (d.data.status) tooltip.appendChild(el('div','tt-cached',statusText(d.data)));
      if (d.data.isDir!==false) tooltip.appendChild(el('div','tt-hint','Click to drill down →'));
    })
    .on('mouseleave',()=>{ tooltip.style.display='none'; })
    .on('click',(_,d)=>{ if(d.data.isDir===false)return; tooltip.style.display='none'; goTo(d.data.path); });
}

// ── Sidebar ──────────────────────────────────────────────────
function renderSidebar(data) {
  const list = document.getElementById('sidebar-list');
  list.innerHTML = '';
  // Mount points have unknown size (0) but are still listed so they can be opened.
  const all = (data.children||[]).filter(c=>hasSizeInfo(c)||c.mount);
  if (!all.length) return;
  const items = all.length > MAX_SIDEBAR ? all.slice(0, MAX_SIDEBAR) : all;
  const maxSz = items[0].size||1;

  items.forEach((item,i) => {
    const color = PALETTE[i%PALETTE.length];
    const div = document.createElement('div');
    div.className = 'sitem'+(item.isDir===false?' file':'')+(item.status?' unknown':'')+(item.mount?' mount':'');
    if (item.status) div.title = statusText(item);
    div.style.animationDelay = Math.min(i*20,200)+'ms';

    const top = el('div','sitem-top');
    const dot = el('div','sitem-dot'); dot.style.background = color;
    const name = el('div','sitem-name',item.name); name.title = item.path;
    top.append(dot, name, el('div','sitem-size',sizeLabel(item)));
    if (item.isDir!==false) {
      const actions = el('div','sitem-actions');
      const btn = el('button','sitem-refresh-btn','↺'); btn.title = 'Refresh this folder';
      actions.appendChild(btn);
      top.appendChild(actions);
    }
    const barWrap = el('div','sitem-bar-wrap');
    const bar = el('div','sitem-bar');
    bar.style.background = color;
    bar.style.width = Math.max(1,((item.size||0)/maxSz)*100)+'%';
    barWrap.appendChild(bar);
    div.append(top, barWrap);

    if (item.isDir!==false) {
      div.addEventListener('click', e => {
        if (e.target.classList.contains('sitem-refresh-btn')) return;
        goTo(item.path);
      });
      const refreshBtn = div.querySelector('.sitem-refresh-btn');
      if (refreshBtn) refreshBtn.addEventListener('click', e => { e.stopPropagation(); refreshPath(item.path); });
    }
    list.appendChild(div);
  });

  if (all.length > items.length) {
    const hidden = all.slice(items.length);
    const note = document.createElement('div');
    note.className = 'sitem file';
    note.style.animation = 'none';
    note.innerHTML = '<div class="sitem-top"><div class="sitem-name"></div><div class="sitem-size"></div></div>';
    note.querySelector('.sitem-name').textContent = `+ ${hidden.length} smaller items not shown`;
    note.querySelector('.sitem-size').textContent = fmt(hidden.reduce((s,c) => s+c.size, 0));
    list.appendChild(note);
  }
}

// ── Init ─────────────────────────────────────────────────────
document.getElementById('back-btn').addEventListener('click', goBack);
document.getElementById('refresh-btn').addEventListener('click', () => refreshCurrent(true));
document.getElementById('path-form').addEventListener('submit', e => { e.preventDefault(); goToPath(); });
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (currentData) renderTreemap(currentData); }, 150);
});
window.addEventListener('keydown', e => {
  if (document.activeElement === document.getElementById('path-input')) return;
  if (e.key==='Backspace'||e.key==='ArrowLeft') goBack();
});

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
            params = parse_qs(parsed.query, errors='surrogateescape')
            path = params.get('path', [''])[0]
            if path:
                # Same resolution as stream_directory, so '/' on macOS clears the Data volume entry.
                cache_delete(_resolve_scan_path(path))
            self.send_response(200)
            self.end_headers()

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _default_path, DU_TIMEOUT
    parser = argparse.ArgumentParser(prog="disko", description="disko - interactive disk usage explorer")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    parser.add_argument("--path", type=str, default=None, help="Starting path (default: your home directory)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser")
    parser.add_argument("--du-timeout", type=float, default=DU_TIMEOUT,
                        help="Seconds before a single du call is abandoned (default: %d)" % DU_TIMEOUT)
    args = parser.parse_args()
    if args.du_timeout <= 0:
        parser.error("--du-timeout must be positive")
    DU_TIMEOUT = args.du_timeout
    if args.path:
        _default_path = norm_path(args.path)
    else:
        _default_path = os.path.expanduser("~")
    port = args.port
    cache_load()
    server = Server(("localhost", port), Handler)
    url = f"http://localhost:{port}"
    print(f"  disko v{__version__} -> {url}")
    print(f"  Cache: {CACHE_FILE}")
    print("  Ctrl+C to stop")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
