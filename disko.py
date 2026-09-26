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

def du_single(path: str) -> int:
    try:
        if platform.system() == "Darwin":
            cmd = ["du", "-sk", "-x", path]
        else:
            cmd = ["du", "-sk", "--one-file-system", path]
        r = subprocess.run(cmd,
                           capture_output=True, text=True, timeout=30)
        for line in r.stdout.strip().split('\n'):
            if '\t' in line:
                kb, _ = line.split('\t', 1)
                return int(kb.strip()) * 1024
    except Exception:
        pass
    return 0


def scan_to_list(path: str) -> list:
    """Scan path and return list of child dicts."""
    path = os.path.normpath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return []
    try:
        entries = list(os.scandir(path))
    except PermissionError:
        return []

    dirs = [e for e in entries if e.is_dir(follow_symlinks=False)]
    files = [e for e in entries if not e.is_dir(follow_symlinks=False)]

    file_total = sum(
        e.stat(follow_symlinks=False).st_size for e in files
        if _safe_stat(e)
    )

    children = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futures = {ex.submit(du_single, e.path): e for e in dirs}
        for fut in concurrent.futures.as_completed(futures):
            entry = futures[fut]
            children.append({
                'name': entry.name,
                'path': entry.path,
                'size': fut.result(),
                'isDir': True,
            })

    if file_total > 0:
        children.append({
            'name': '(loose files)',
            'path': path,
            'size': file_total,
            'isDir': False,
        })

    children.sort(key=lambda c: -c['size'])
    return children


def _safe_stat(entry):
    try:
        entry.stat(follow_symlinks=False)
        return True
    except OSError:
        return False


# ── Background prefetch pool ─────────────────────────────────────────────────

_prefetch_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=PREFETCH_WORKERS, thread_name_prefix='prefetch')
_prefetching: set = set()
_prefetch_lock = threading.Lock()


def schedule_prefetch(children: list):
    """Kick off background scans for the top-N largest child dirs."""
    dirs = [c for c in children if c.get('isDir') and c['size'] > 0]
    dirs = sorted(dirs, key=lambda c: -c['size'])[:PREFETCH_TOP_N]
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
        cache_set(path, children)
        # One more level deep
        schedule_prefetch(children)
    finally:
        with _prefetch_lock:
            _prefetching.discard(path)


# ── Streaming (SSE) ───────────────────────────────────────────────────────────

def stream_directory(path: str, write_event, force: bool = False):
    path = os.path.normpath(os.path.expanduser(path))
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
            cache_set(path, new_children)
            schedule_prefetch(new_children)
        threading.Thread(target=refresh, daemon=True).start()
        return

    # Live scan
    try:
        entries = list(os.scandir(path))
    except PermissionError as e:
        write_event({'type': 'error', 'error': str(e)})
        return

    dirs = [e for e in entries if e.is_dir(follow_symlinks=False)]
    files = [e for e in entries if not e.is_dir(follow_symlinks=False)]
    file_total = sum(
        e.stat(follow_symlinks=False).st_size for e in files if _safe_stat(e)
    )

    write_event({
        'type': 'start',
        'path': path,
        'name': os.path.basename(path) or path,
        # +1 for the "(loose files)" entry sent after the dirs, so progress never exceeds 100%
        'total_dirs': len(dirs) + (1 if file_total > 0 else 0),
        'from_cache': False,
    })

    collected = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futures = {ex.submit(du_single, e.path): e for e in dirs}
        for fut in concurrent.futures.as_completed(futures):
            entry = futures[fut]
            child = {
                'name': entry.name,
                'path': entry.path,
                'size': fut.result(),
                'isDir': True,
            }
            collected.append(child)
            write_event({'type': 'child', **child})

    if file_total > 0:
        loose = {'name': '(loose files)', 'path': path, 'size': file_total, 'isDir': False}
        collected.append(loose)
        write_event({'type': 'child', **loose})

    write_event({'type': 'done'})
    cache_set(path, sorted(collected, key=lambda c: -c['size']))
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
.cell text { pointer-events: none; }

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

/* ── Error ── */
#error-overlay {
  position: absolute; inset: 0; display: none;
  align-items: center; justify-content: center; flex-direction: column; gap: 8px;
  background: rgba(17,19,24,.7);
}
#error-overlay.show { display: flex; }
#error-title { color: #e94560; font-size: 15px; font-weight: 600; }
#error-detail { color: #94a3b8; font-size: 12px; }

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
.tt-cached { font-size: 10px; color: #334155; margin-top: 4px; }
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
    <svg id="treemap" role="img" aria-label="Treemap of folder contents by size"></svg>
    <div id="empty-state">This folder is empty</div>
    <div id="error-overlay" role="alert">
      <div id="error-title">⚠ Could not scan</div>
      <div id="error-detail"></div>
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
      while (lo < hi) { const mid = (lo+hi)>>1; items[mid].size >= item.size ? lo=mid+1 : hi=mid; }
      items.splice(lo, 0, item);
      const total = items.reduce((s,i) => s+i.size, 0);
      renderData({ path: meta.path, name: meta.name, size: total, children: [...items] });
      if (totalDirs > 0) setProgress(Math.min(100, Math.round((received/totalDirs)*100)));
      setScanStatus(`${received} / ${totalDirs}`, false);
    },
    onDone() {
      if (currentData) { currentData.done = true; updateEmptyState(currentData); }
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
  if (refreshingPaths.has(path)) return;
  // Otherwise force a rescan of that folder (which also refreshes its cache entry),
  // show a spinner on its row meanwhile, and update its size in the current view.
  const parentPath = currentData ? currentData.path : null;
  refreshingPaths.add(path);
  setRowRefreshState(path, 'spinning');
  let total = 0, finished = false;
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
    currentData.children.sort((a, b) => b.size - a.size);
    currentData.size = currentData.children.reduce((s, c) => s + c.size, 0);
    renderData(currentData);
    if (currentData.done) updateEmptyState(currentData);
  };
  es.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'child') total += msg.size || 0;
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
  updateEmptyState(data);
  document.getElementById('back-btn').classList.toggle('visible', navStack.length > 0);
  document.getElementById('up-btn').disabled = !data.path || data.path === '/';
  document.querySelector('#total-size span').textContent = fmt(data.size);
}

// "Empty" is only known once the scan finished (data.done); hide it while children stream in.
function updateEmptyState(data) {
  const empty = !!data.done && !(data.children || []).some(c => c.size > 0);
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
  renderTreemap(data);
  renderSidebar(data);
  document.querySelector('#total-size span').textContent = fmt(data.size);
}

function renderFull(data) {
  render(data);
  renderBreadcrumb();
  document.getElementById('back-btn').classList.toggle('visible', navStack.length > 0);
}

// Crumbs are derived from the current path itself (not click history), so every
// ancestor up to "/" is reachable.
function renderBreadcrumb() {
  const bc = document.getElementById('breadcrumb');
  bc.textContent = '';
  if (!currentData || !currentData.path) return;
  const parts = currentData.path.split('/').filter(Boolean);
  const chain = [{ name: '/', path: '/' }];
  parts.forEach((part, i) => chain.push({ name: part, path: '/' + parts.slice(0, i + 1).join('/') }));
  chain.forEach((item, i) => {
    const last = i === chain.length-1;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'crumb' + (last ? ' active' : '');
    btn.title = item.path;
    btn.textContent = item.name;
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

function showCacheBadge(ts) {
  const el = document.getElementById('cache-badge');
  el.textContent = '⚡ cached ' + timeAgo(ts);
  el.classList.add('show');
}
function hideCacheBadge() {
  document.getElementById('cache-badge').classList.remove('show');
}

// ── Treemap ──────────────────────────────────────────────────
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
  const line = (cls, txt) => {
    const el = document.createElement('div'); el.className = cls; el.textContent = txt; tooltip.appendChild(el);
  };
  line('tt-name', d.data.name);
  line('tt-path', d.data.path);
  line('tt-size', fmt(d.data.size||d.value));
  line('tt-pct', `${pctOf(d.data.size||d.value,totalVal)} of this view`);
  if (d.data.isDir!==false) line('tt-hint', 'Click to drill down →');
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
  const el = document.elementFromPoint(lastMouse.x, lastMouse.y);
  const g = el && el.closest ? el.closest('#treemap g.cell') : null;
  if (!g) { tooltip.style.display='none'; return; }
  showTooltip(d3.select(g).datum(), totalVal, lastMouse.x, lastMouse.y);
}

function renderTreemap(data) {
  const tooltip = document.getElementById('tooltip');
  const wrap = document.getElementById('treemap-wrap');
  const W = Math.max(0, wrap.clientWidth-20), H = Math.max(0, wrap.clientHeight-20);
  const svg = d3.select('#treemap').attr('width',W).attr('height',H);
  svg.selectAll('*').remove();
  const children = (data.children||[]).filter(c=>c.size>0);
  if (!children.length) { tooltip.style.display='none'; return; }

  const root = d3.hierarchy({name:'root',children})
    .sum(d=>d.size||0).sort((a,b)=>b.value-a.value);
  d3.treemap().size([W,H]).paddingOuter(3).paddingInner(2).round(true)(root);

  const topKids = root.children||[];
  const colorOf = d => { let n=d; while(n.depth>1) n=n.parent; return PALETTE[topKids.indexOf(n)%PALETTE.length]; };
  const shade = (hex,depth) => { const c=d3.color(hex); return c?c.darker(depth*.35).toString():hex; };
  const totalVal = root.value||1;

  const cell = svg.selectAll('g.cell').data(root.leaves()).enter()
    .append('g').attr('class', d=>'cell'+(d.data.isDir===false?' file':''))
    .attr('transform', d=>`translate(${d.x0},${d.y0})`);

  cell.append('rect')
    .attr('width',  d=>Math.max(0,d.x1-d.x0))
    .attr('height', d=>Math.max(0,d.y1-d.y0))
    .attr('fill',   d=>shade(colorOf(d),d.depth-1))
    .attr('rx',3);

  cell.each(function(d) {
    const cw=d.x1-d.x0, ch=d.y1-d.y0, g=d3.select(this);
    const ink=labelColor(shade(colorOf(d),d.depth-1));
    if (cw>45&&ch>22) {
      const mc=Math.max(3,Math.floor((cw-12)/7.5));
      g.append('text').attr('x',6).attr('y',16)
        .attr('font-size',Math.min(12,Math.max(9,cw/10)))
        .attr('font-weight','600').attr('fill',ink).text(truncateLabel(d.data.name, mc));
    }
    if (cw>55&&ch>38)
      g.append('text').attr('x',6).attr('y',30).attr('font-size',10)
        .attr('fill',ink).text(fmt(d.data.size||d.value));
  });

  cell
    .on('mousemove',(ev,d) => {
      lastMouse = { x: ev.clientX, y: ev.clientY };
      showTooltip(d, totalVal, ev.clientX, ev.clientY);
    })
    .on('mouseleave',()=>{ tooltip.style.display='none'; })
    .on('click',(_,d)=>{ if(d.data.isDir===false)return; tooltip.style.display='none'; goTo(d.data.path); });

  refreshTooltip(totalVal);
}

// ── Sidebar ──────────────────────────────────────────────────
function renderSidebar(data) {
  const list = document.getElementById('sidebar-list');
  list.textContent = '';
  const items = (data.children||[]).filter(c=>c.size>0);
  if (!items.length) return;
  const maxSz = items[0].size||1;

  const el = (tag, cls) => { const e = document.createElement(tag); if (cls) e.className = cls; return e; };
  items.forEach((item,i) => {
    const color = PALETTE[i%PALETTE.length];
    const isDir = item.isDir!==false;
    const div = el('div', 'sitem'+(isDir?'':' file'));
    div.style.animationDelay = Math.min(i*20,200)+'ms';

    // Folders get a real <button> (keyboard + screen reader); the refresh button is a sibling, not nested.
    const main = el(isDir ? 'button' : 'div', 'sitem-main');
    if (isDir) {
      main.type = 'button';
      main.setAttribute('aria-label', `Open ${item.name}, ${fmt(item.size)}`);
      main.addEventListener('click', () => goTo(item.path));
    }
    const top = el('div', 'sitem-top');
    const dot = el('div', 'sitem-dot'); dot.style.background = color;
    const name = el('div', 'sitem-name'); name.title = item.path; name.textContent = item.name;
    const size = el('div', 'sitem-size'); size.textContent = fmt(item.size);
    top.append(dot, name, size);
    const barWrap = el('div', 'sitem-bar-wrap');
    const bar = el('div', 'sitem-bar');
    bar.style.background = color;
    bar.style.width = Math.max(1,(item.size/maxSz)*100)+'%';
    barWrap.appendChild(bar);
    main.append(top, barWrap);
    div.appendChild(main);

    if (isDir) {
      const actions = el('div', 'sitem-actions');
      const btn = el('button', 'sitem-refresh-btn');
      btn.type = 'button';
      btn.textContent = '↺';
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
}

// ── Init ─────────────────────────────────────────────────────
document.getElementById('back-btn').addEventListener('click', goBack);
document.getElementById('up-btn').addEventListener('click', goUp);
document.getElementById('refresh-btn').addEventListener('click', () => refreshCurrent(true));
document.getElementById('path-form').addEventListener('submit', e => { e.preventDefault(); goToPath(); });
window.addEventListener('resize', () => { if (currentData) renderTreemap(currentData); });
window.addEventListener('keydown', e => {
  // Never hijack browser/OS shortcuts (Cmd/Ctrl/Alt/Shift+Arrow etc.) or typing in form fields
  if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
  const t = e.target;
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
  if (e.key==='Backspace'||e.key==='ArrowLeft') { e.preventDefault(); goBack(); }
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
            params = parse_qs(parsed.query)
            path = params.get('path', [os.path.expanduser('~')])[0]
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

            stream_directory(path, write_event, force=force)

        elif parsed.path == '/invalidate':
            params = parse_qs(parsed.query)
            path = params.get('path', [''])[0]
            if path:
                cache_delete(path)
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
    global _default_path
    parser = argparse.ArgumentParser(prog="disko", description="disko - interactive disk usage explorer")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    parser.add_argument("--path", type=str, default=None, help="Starting path (auto-detected if omitted)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser")
    args = parser.parse_args()
    if args.path:
        _default_path = os.path.normpath(os.path.expanduser(args.path))
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
