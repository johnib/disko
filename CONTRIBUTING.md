# Contributing to disko

Thank you for your interest in contributing to disko!

## Getting started

1. Fork the repository on GitHub.
2. Clone your fork locally:
   ```bash
   git clone https://github.com/<your-username>/disko.git
   cd disko
   ```
3. Create a feature or fix branch:
   ```bash
   git checkout -b my-feature
   ```
4. Make your changes, commit them, and push to your fork:
   ```bash
   git push origin my-feature
   ```
5. Open a pull request against the `main` branch of the upstream repository.

## Code style

**Python**
- Follow [PEP 8](https://peps.python.org/pep-0008/) with a maximum line length of **120 characters**.
- Use descriptive variable names and add docstrings to public functions.
- Avoid adding third-party dependencies; disko is designed to run with zero external dependencies.

**JavaScript**
- Write vanilla JavaScript consistent with the existing inline scripts in `disko.py`.
- No build tools, transpilers, or bundlers. The JS is served directly by the embedded HTTP server.
- Keep functions small and well-named; add a brief comment for any non-obvious logic.

## Manual testing checklist

Before submitting a pull request, verify the following scenarios work correctly:

- [ ] **Startup** — `python disko.py /some/path` launches without errors and opens (or prints) the URL.
- [ ] **Treemap render** — The D3.js treemap displays folders sized proportionally to their disk usage.
- [ ] **Drill-down** — Clicking a folder zooms into it and updates the breadcrumb trail.
- [ ] **Cache badge on revisit** — Navigating back to a previously scanned folder shows the cache badge and the cached size/time.
- [ ] **Refresh button** — Clicking the per-folder refresh button re-scans that folder and updates the display.
- [ ] **Global refresh** — The global refresh control re-scans the root and clears stale cache entries.
- [ ] **Path jump** — Typing a path into the path jump bar and submitting navigates directly to that folder.
- [ ] **Progress indicator** — A progress indicator is visible during long scans.
- [ ] **CLI flags** — Test at least two CLI flags (e.g. `--port`, `--no-open`) and confirm they behave as documented.
- [ ] **py_compile** — Run `python -m py_compile disko.py` and confirm it exits with no errors.

## Submitting PRs

- Keep pull requests focused on a single change or feature.
- Reference any related issue in the PR description (e.g. `Closes #42`).
- Make sure all items in the manual testing checklist above pass.
- Write a clear description of what changed and why.
- Be responsive to review feedback; mark conversations resolved after addressing them.

## Reporting bugs

When opening a bug report, please include:

- **OS** and version (e.g. macOS 14.5, Ubuntu 24.04)
- **Python version** (`python --version`)
- **Browser** and version
- Steps to reproduce the issue
- Expected behavior vs. actual behavior
- Any relevant error output from the terminal or browser console
