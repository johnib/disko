# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- **Local-only requests** — the server now rejects requests whose `Host` header is not `localhost`/`127.0.0.1`/`[::1]` on its port (DNS rebinding), and cross-site requests by `Origin` or `Sec-Fetch-Site`. The wildcard `Access-Control-Allow-Origin` header was removed.
- **XSS** — file and folder names are no longer inserted as HTML, and every `<` in the injected default path is escaped so it cannot close the page's `<script>`.
- **Pinned D3** — D3 is loaded as version 7.9.0 with Subresource Integrity; if it fails to load, an overlay explains why instead of a blank page.
- **Cache file** — written atomically (temp file + rename) with mode `0600`, never through a symlink, and never saved when running as root.
- **Private vulnerability reporting** — `SECURITY.md` points to GitHub's private reporting.

### Added

- **`--du-timeout SECONDS`** — seconds before a single `du` call is abandoned (default 300; must be positive).
- **`--verbose`** — log HTTP requests to stderr (quiet by default).
- **`DISKO_CACHE` environment variable** — overrides the cache file location (default `~/.disko_cache.json`).
- **Stale-while-revalidate** — cached results older than 5 minutes are shown immediately, then rescanned and swapped in when the rescan finishes. Concurrent views of the same stale folder share one rescan, and a rescanned folder's new size is written into its cached parents.
- **Unit test suite** — stdlib `unittest` tests under `tests/`, run in CI.

### Changed

- **Python 3.9+ required** — the minimum Python version is now 3.9 (was 3.8). CI tests 3.9, 3.11, 3.12 and 3.13.
- **Default path** — when `--path` is omitted, disko now starts in your home directory (`~`) on every platform. Previously macOS defaulted to `/System/Volumes/Data`, a slow whole-volume scan.
- **Concurrent, bounded scanning** — the HTTP server is threaded; at most 12 `du` processes run at once across all scans and prefetches; a scan stops launching `du` when its client disconnects; prefetching goes one level below the scanned folder.
- **Allocated size** — folders and loose files are both measured by allocated disk blocks, so sparse files count the same from the parent and after drilling in.
- **Mount points** — other filesystems mounted inside a scanned folder are not measured (they may be network shares or external disks), are shown as "mount" and are kept out of the parent's totals. Open one to scan it.
- **Friendly port error** — a clear message suggesting another `--port` when the port is already in use.
- **Packaging** — `pyproject.toml` uses the standard `setuptools.build_meta` backend so `pip install` works, with an SPDX license and the version read from `disko.py`.

### Fixed

- **`du` failures** — a folder whose `du` failed, timed out or could only be partly read is now shown as unknown, "timed out" or a lower bound ("≥") instead of 0, and such results are not cached.
- **Frontend stream lifecycle** — Back, errors and the path bar no longer leave stale streams updating the view.
- **Render batching** — stream events are rendered at most once per animation frame, and the treemap caps the number of cells, so large folders stay responsive.
- **Per-row refresh** — refreshing a folder row shows a spinner (or a failure state) and updates that row's size in the current view.
- **Accessibility** — folder rows and the refresh button are real buttons with labels, visible keyboard focus, breadcrumb `aria-current`, and better contrast.
- **macOS root** — asking for `/` scans the Data volume but keeps `/` as the displayed path, so the breadcrumb and Up button treat it as the root.

## [1.0.0] - 2026-06-28

### Added

- **Parallel scanning** — concurrent directory traversal for significantly faster results on large trees.
- **SSE streaming** — scan progress is streamed to the browser in real time via Server-Sent Events.
- **Persistent cache** — scan results are cached on disk so revisiting a folder is instant.
- **Auto-prefetch** — visible child folders are prefetched in the background after a scan completes.
- **D3.js treemap** — interactive treemap visualization sized proportionally to folder disk usage.
- **Breadcrumbs** — clickable breadcrumb trail reflecting the current drill-down path.
- **Path jump bar** — type any absolute path to navigate to it directly.
- **Per-folder refresh** — refresh button on each folder to re-scan only that subtree.
- **Progress indicator** — live progress display during active scans.
- **Cache badge** — visual indicator on cached folders showing size and scan timestamp.
- **Cross-platform support** — tested on macOS and Linux.
- **CLI flags** — configurable port (`--port`), starting path (`--path`) and browser auto-open (`--no-browser`).
- **Zero dependencies** — runs entirely on the Python standard library with no third-party packages required.
