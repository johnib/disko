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
import threading
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

__version__ = "1.0.0"
_default_path = "/"

SCAN_WORKERS = 12   # parallel du workers per scan
PREFETCH_WORKERS = 4  # background prefetch workers
CACHE_FILE = os.path.expanduser('~/.disko_cache.json')
PREFETCH_TOP_N = 10  # prefetch top-N largest subdirs after each scan
DU_TIMEOUT = 300     # seconds per du call (overridable via --du-timeout)

# ── Cache ────────────────────────────────────────────────────────────────────

_cache: dict = {}
_cache_lock = threading.Lock()


def cache_load():
    global _cache
    try:
        with open(CACHE_FILE) as f:
            _cache = json.load(f)
        print(f'  Cache loaded: {len(_cache)} paths from {CACHE_FILE}')
    except FileNotFoundError:
        _cache = {}
    except Exception as e:
        print(f'  Cache load error: {e}')
        _cache = {}


def cache_save():
    with _cache_lock:
        try:
            with open(CACHE_FILE, 'w') as f:
                json.dump(_cache, f)
        except Exception as e:
            print(f'  Cache save error: {e}')


def cache_get(path: str):
    with _cache_lock:
        return _cache.get(path)


def cache_set(path: str, children: list):
    with _cache_lock:
        _cache[path] = {'children': children, 'scanned_at': time.time()}
    cache_save()


def cache_delete(path: str):
    with _cache_lock:
        _cache.pop(path, None)
    cache_save()


# ── Scanning ─────────────────────────────────────────────────────────────────

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
    path = norm_path(path)
    try:
        entries = list(os.scandir(path))
    except OSError:
        return None

    dirs = [e for e in entries if e.is_dir(follow_symlinks=False)]
    files = [e for e in entries if not e.is_dir(follow_symlinks=False)]

    file_total = sum(_file_size(e) for e in files)

    children = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futures = {ex.submit(du_single, e.path): e for e in dirs}
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


def _file_size(entry) -> int:
    try:
        return entry.stat(follow_symlinks=False).st_size
    except OSError:
        return 0


# ── Background prefetch pool ─────────────────────────────────────────────────

_prefetch_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=PREFETCH_WORKERS, thread_name_prefix='prefetch')
_prefetching: set = set()
_prefetch_lock = threading.Lock()


def schedule_prefetch(children: list):
    """Kick off background scans for the top-N largest child dirs."""
    dirs = [c for c in children if c.get('isDir') and (c.get('size') or 0) > 0]
    dirs = sorted(dirs, key=_size_key)[:PREFETCH_TOP_N]
    for d in dirs:
        p = d['path']
        with _prefetch_lock:
            if p in _prefetching or cache_get(p):
                continue
            _prefetching.add(p)
        _prefetch_pool.submit(_do_prefetch, p)


def _do_prefetch(path: str):
    try:
        children = scan_to_list(path)
        if children is None:
            return
        if _cacheable(children):
            cache_set(path, children)
        # One more level deep
        schedule_prefetch(children)
    finally:
        with _prefetch_lock:
            _prefetching.discard(path)


# ── Streaming (SSE) ───────────────────────────────────────────────────────────

def stream_directory(path: str, write_event, force: bool = False):
    path = norm_path(path)
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
        write_event({'type': 'done'})

        # Silent background refresh
        def refresh():
            new_children = scan_to_list(path)
            if new_children is None:
                return
            if _cacheable(new_children):
                cache_set(path, new_children)
            schedule_prefetch(new_children)
        threading.Thread(target=refresh, daemon=True).start()
        return

    # Live scan
    try:
        entries = list(os.scandir(path))
    except OSError as e:
        write_event({'type': 'error', 'error': str(e)})
        return

    dirs = [e for e in entries if e.is_dir(follow_symlinks=False)]
    files = [e for e in entries if not e.is_dir(follow_symlinks=False)]
    file_total = sum(_file_size(e) for e in files)

    write_event({
        'type': 'start',
        'path': path,
        'name': os.path.basename(path) or path,
        'total_dirs': len(dirs),
        'from_cache': False,
    })

    collected = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futures = {ex.submit(du_single, e.path): e for e in dirs}
        for fut in concurrent.futures.as_completed(futures):
            child = _dir_child(futures[fut], fut.result())
            collected.append(child)
            write_event({'type': 'child', **child})

    if file_total > 0:
        loose = {'name': '(loose files)', 'path': path, 'size': file_total, 'isDir': False}
        collected.append(loose)
        write_event({'type': 'child', **loose})

    write_event({'type': 'done'})
    if _cacheable(collected):
        cache_set(path, sorted(collected, key=_size_key))
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

let navStack = [];
let currentData = null;
let activeES = null;

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

// ── Streaming ────────────────────────────────────────────────
function startStream(path, force, callbacks) {
  if (activeES) { activeES.close(); activeES = null; }
  const url = `${API}/stream?path=${encodeURIComponent(path)}${force?'&force=1':''}`;
  const es = new EventSource(url);
  activeES = es;
  es.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'start') callbacks.onStart?.(msg);
    if (msg.type === 'child') callbacks.onChild?.(msg);
    if (msg.type === 'done')  { es.close(); activeES=null; callbacks.onDone?.(); }
    if (msg.type === 'error') { es.close(); activeES=null; callbacks.onError?.(msg.error); }
  };
  es.onerror = () => { es.close(); activeES=null; callbacks.onError?.('Connection lost'); };
}

// ── Navigation ───────────────────────────────────────────────
function navigate(path, force) {
  document.getElementById('error-overlay').classList.remove('show');
  hideCacheBadge();
  setProgress(0);
  setScanStatus('scanning…', false);

  const items = [];
  let meta = null, totalDirs = 0, received = 0;

  startStream(path, force, {
    onStart(msg) {
      meta = msg; totalDirs = msg.total_dirs;
      if (msg.from_cache) showCacheBadge(msg.scanned_at);
      render({ path: msg.path, name: msg.name, size: 0, children: [] });
    },
    onChild(item) {
      received++;
      // Insert sorted by size
      let lo = 0, hi = items.length;
      while (lo < hi) { const mid = (lo+hi)>>1; (items[mid].size||0) >= (item.size||0) ? lo=mid+1 : hi=mid; }
      items.splice(lo, 0, item);
      const total = items.reduce((s,i) => s+(i.size||0), 0);
      renderData({ path: meta.path, name: meta.name, size: total, children: [...items] });
      if (totalDirs > 0) setProgress(Math.round((received/totalDirs)*100));
      setScanStatus(`${received} / ${totalDirs}`, false);
    },
    onDone() {
      setProgress(100);
      setScanStatus('done ✓', true);
      setTimeout(() => { setProgress(-1); setScanStatus('', true); }, 1500);
      document.getElementById('refresh-btn').classList.remove('spinning');
    },
    onError(err) {
      setProgress(-1); setScanStatus('', true);
      document.getElementById('error-detail').textContent = err;
      document.getElementById('error-overlay').classList.add('show');
      document.getElementById('refresh-btn').classList.remove('spinning');
    }
  });
}

function goTo(path, force) {
  if (currentData) navStack.push(currentData);
  currentData = null;
  navigate(path, force);
}

function refreshCurrent(force=true) {
  if (!currentData) return;
  document.getElementById('refresh-btn').classList.add('spinning');
  const path = currentData.path;
  currentData = null;
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
  const prev = navStack.pop();
  currentData = prev;
  renderFull(prev);
}

function goToPath() {
  const val = document.getElementById('path-input').value.trim();
  if (!val) return false;
  navStack = [];
  currentData = null;
  document.getElementById('path-input').value = '';
  document.getElementById('path-input').blur();
  navigate(val, false);
  return false;
}

// ── Render ───────────────────────────────────────────────────
function render(data) {
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
    if (i < chain.length-1) span.onclick = () => { navStack = navStack.slice(0,i); renderFull(item); };
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
  const children = (data.children||[]).filter(hasSizeInfo);
  if (!children.length) return;
  // Unknown / zero-size partial entries get a small nominal area so they stay visible.
  const knownTotal = children.reduce((s,c)=>s+(c.size||0),0);
  const stubSize = Math.max(1, knownTotal*0.02);

  const root = d3.hierarchy({name:'root',children})
    .sum(d=>d.children?0:(d.size>0?d.size:stubSize)).sort((a,b)=>b.value-a.value);
  d3.treemap().size([W,H]).paddingOuter(3).paddingInner(2).round(true)(root);

  const topKids = root.children||[];
  const colorOf = d => { let n=d; while(n.depth>1) n=n.parent; return PALETTE[topKids.indexOf(n)%PALETTE.length]; };
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
      tooltip.innerHTML=`
        <div class="tt-name">${d.data.name}</div>
        <div class="tt-path">${d.data.path}</div>
        <div class="tt-size">${sizeLabel(d.data)}</div>
        <div class="tt-pct">${pctOf(d.data.size||0,totalVal)} of this view</div>
        ${d.data.status?`<div class="tt-cached">${statusText(d.data)}</div>`:''}
        ${d.data.isDir!==false?'<div class="tt-hint">Click to drill down →</div>':''}`;
    })
    .on('mouseleave',()=>{ tooltip.style.display='none'; })
    .on('click',(_,d)=>{ if(d.data.isDir===false)return; tooltip.style.display='none'; goTo(d.data.path); });
}

// ── Sidebar ──────────────────────────────────────────────────
function renderSidebar(data) {
  const list = document.getElementById('sidebar-list');
  list.innerHTML = '';
  const items = (data.children||[]).filter(hasSizeInfo);
  if (!items.length) return;
  const maxSz = items[0].size||1;

  items.forEach((item,i) => {
    const color = PALETTE[i%PALETTE.length];
    const div = document.createElement('div');
    div.className = 'sitem'+(item.isDir===false?' file':'')+(item.status?' unknown':'');
    if (item.status) div.title = statusText(item);
    div.style.animationDelay = Math.min(i*20,200)+'ms';

    const actionsHtml = item.isDir!==false ? `
      <div class="sitem-actions">
        <button class="sitem-refresh-btn" title="Refresh this folder">↺</button>
      </div>` : '';

    div.innerHTML = `
      <div class="sitem-top">
        <div class="sitem-dot" style="background:${color}"></div>
        <div class="sitem-name" title="${item.path}">${item.name}</div>
        <div class="sitem-size">${sizeLabel(item)}</div>
        ${actionsHtml}
      </div>
      <div class="sitem-bar-wrap">
        <div class="sitem-bar" style="background:${color};width:${Math.max(1,((item.size||0)/maxSz)*100)}%"></div>
      </div>`;

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
}

// ── Init ─────────────────────────────────────────────────────
document.getElementById('back-btn').addEventListener('click', goBack);
document.getElementById('refresh-btn').addEventListener('click', () => refreshCurrent(true));
document.getElementById('path-form').addEventListener('submit', e => { e.preventDefault(); goToPath(); });
window.addEventListener('resize', () => { if (currentData) renderTreemap(currentData); });
window.addEventListener('keydown', e => {
  if (document.activeElement === document.getElementById('path-input')) return;
  if (e.key==='Backspace'||e.key==='ArrowLeft') goBack();
});

navigate("%%DEFAULT_PATH%%", false);
</script>
</body>
</html>
"""


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == '/':
            body = HTML.replace("%%DEFAULT_PATH%%", _default_path).encode("utf-8")
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
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            stop = threading.Event()

            def write_event(data):
                if stop.is_set():
                    return
                try:
                    self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    stop.set()

            try:
                stream_directory(path, write_event, force=force)
            except Exception as e:
                write_event({'type': 'error', 'error': 'Scan failed: %s' % (e or type(e).__name__)})

        elif parsed.path == '/invalidate':
            params = parse_qs(parsed.query, errors='surrogateescape')
            path = params.get('path', [''])[0]
            if path:
                cache_delete(norm_path(path))
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _default_path, DU_TIMEOUT
    parser = argparse.ArgumentParser(prog="disko", description="disko - interactive disk usage explorer")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    parser.add_argument("--path", type=str, default=None, help="Starting path (auto-detected if omitted)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser")
    parser.add_argument("--du-timeout", type=float, default=DU_TIMEOUT,
                        help="Seconds before a single du call is abandoned (default: %d)" % DU_TIMEOUT)
    args = parser.parse_args()
    if args.du_timeout <= 0:
        parser.error("--du-timeout must be positive")
    DU_TIMEOUT = args.du_timeout
    if args.path:
        _default_path = norm_path(args.path)
    elif platform.system() == "Darwin":
        _default_path = "/System/Volumes/Data"
    else:
        _default_path = os.path.expanduser("~")
    port = args.port
    cache_load()
    server = HTTPServer(("localhost", port), Handler)
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
