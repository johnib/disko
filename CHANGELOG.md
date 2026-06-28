# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
- **Global refresh** — top-level control to re-scan the root and invalidate stale cache entries.
- **Progress indicator** — live progress display during active scans.
- **Cache badge** — visual indicator on cached folders showing size and scan timestamp.
- **Cross-platform support** — tested on macOS and Linux.
- **CLI flags** — configurable port, auto-open behavior, root path, and other options via command-line arguments.
- **Zero dependencies** — runs entirely on the Python standard library with no third-party packages required.
