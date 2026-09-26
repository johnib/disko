# disko

**Interactive disk usage explorer**

[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)](https://github.com/johnib/disko)

---

![disko screenshot](docs/screenshot.png)

---

## Features

- **Parallel scanning** — sizes subfolders with up to 12 concurrent `du` processes (a global cap shared by all scans and prefetches) for fast results on large trees
- **Real-time streaming** — directory sizes stream to the browser live via Server-Sent Events (SSE) as the scan progresses
- **Persistent cache with background refresh** — previously scanned folders load instantly from the cache; entries older than 5 minutes are shown immediately, then re-scanned and the view updates with the fresh sizes when the rescan finishes
- **Zoomable D3.js treemap** — navigate disk usage visually; click any node to zoom in and explore
- **Breadcrumb navigation** — always know where you are in the tree and jump back to any ancestor in one click
- **Per-folder refresh** — re-scan any individual folder on demand without restarting the server
- **Path jump bar** — type any absolute path to jump directly to it
- **Zero Python dependencies** — runs entirely on the standard library; no `pip install` required

---

## Requirements

- Python 3.9 or later
- macOS or Linux
- A modern web browser (Chrome, Firefox, Safari, or Edge)

---

## Quick Start

```bash
git clone https://github.com/johnib/disko.git
cd disko
python3 disko.py
```

disko will start a local web server and open your browser automatically.

---

## CLI Reference

```
python3 disko.py [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--port PORT` | `8765` | Port for the local web server (binds to `localhost`) |
| `--path PATH` | `~` (your home directory) | Starting directory to explore |
| `--no-browser` | — | Start the server without opening a browser tab |
| `--du-timeout SECONDS` | `300` | Give up on a single folder's `du` after this many seconds (must be positive); the folder is then shown as "timed out" |
| `--help` | — | Show help and exit |

**Examples:**

```bash
# Scan a specific directory on a custom port
python3 disko.py --path /var/log --port 9000

# Run headlessly (useful in SSH sessions)
python3 disko.py --path /data --no-browser
```

---

## How It Works

1. **Scanning** — disko lists the immediate children of the current folder. Each subfolder is sized by running `du -sk` (staying on one filesystem) in a `concurrent.futures.ThreadPoolExecutor`; at most 12 `du` processes run at once across all live scans and background prefetches. The server handles requests concurrently, so a long scan does not block page loads or other requests, and if the browser disconnects mid-scan the remaining work is cancelled and the partial result is not cached. Loose files directly in the folder are summed with `stat` and shown as a single "(loose files)" tile. If `du` cannot size a folder (permission denied, timeout, other error) it is shown as a grey dashed "unknown" / "timed out" tile rather than as 0; if `du` reports a total but hit unreadable subfolders, the size is shown as a lower bound ("≥ N") with a dashed outline.
2. **Streaming** — as each subfolder is sized, the result is pushed to the browser over an SSE (`text/event-stream`) connection so the treemap updates in real time. While waiting on slow `du` calls, a `: keepalive` SSE comment is sent every 2 seconds (ignored by the browser) so a closed tab is noticed quickly.
3. **Visualization** — the browser renders an interactive, zoomable treemap using [D3.js](https://d3js.org/). Node area is proportional to disk usage.
4. **Prefetch** — after a scan, the 10 largest subfolders are scanned in the background by a shared pool of 4 prefetch workers (one level deep only, and never the same folder twice at once), so drilling down is usually instant.
5. **Cache** — every completed folder scan is stored in `~/.disko_cache.json` (scans containing a folder of unknown size are not cached, so they are retried next time). When you open a cached folder, the cached result is served immediately; if the entry is more than 5 minutes old, the folder is then re-scanned while the page stays connected (showing "refreshing…"), and the fresh result replaces the cached one on screen and in the cache. That rescan completes and updates the cache even if you navigate away.

### What the sizes mean

- **Allocated size.** Folders are sized with `du -k` and loose files with their allocated blocks (`st_blocks × 512`), so both use the same measure. A sparse file (such as `Docker.raw`) counts as the space it actually uses on disk, not its apparent length.
- **Mount points.** A subfolder that is on a different filesystem, such as an external disk, a network share, or `/Volumes/*`, is listed as a `mount` entry with no size. It isn't scanned as part of its parent; click it to scan it separately.
- **macOS `/`.** Scanning `/` on macOS actually scans `/System/Volumes/Data`. Firmlinks make the Data volume reachable through several paths on the same device, so scanning `/` would count it more than once.
- **Hard links (known limitation).** Each subfolder is sized by its own `du` process so the results can stream in parallel. `du` only removes duplicate hard links within one run, so a file hard-linked into two sibling folders is counted in both, and the parent's total can come out higher than its real usage. This mostly affects pnpm stores, nix, ccache, and backup trees.

---

## Cache

| Detail | Value |
|---|---|
| Location | `~/.disko_cache.json` (in your home directory, outside the repository; override with the `DISKO_CACHE` environment variable) |
| Clear cache | `rm ~/.disko_cache.json` |
| Writes | Atomic (temp file + rename), file mode `0600`; never saved when running as root |
| Corrupt file | Ignored with a warning; disko starts with an empty cache |

The cache stores one entry per scanned folder (including prefetched folders). Entries are keyed by absolute, normalized path (`~`, `..` and trailing slashes resolve to the same entry) and hold the folder's immediate children with their sizes, plus a `scanned_at` timestamp recording when the scan started, which the UI shows as the scan time.

Cached results are shown instantly. If an entry is older than `CACHE_TTL` (5 minutes by default), disko keeps the connection open, rescans the folder in the background, and swaps in the fresh results when the rescan finishes. When a folder is rescanned, its new size is also written into the cached entries of its parent folders. A scan never overwrites an entry written by a scan that started later.

---

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on how to open issues, submit pull requests, run the unit tests, and manually test your changes.

---

## License

MIT License. Copyright (c) [Jonathan Barazany](https://barazany.dev).

See [LICENSE](LICENSE) for the full text.
