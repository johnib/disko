# PRD: File-Type Color Coding & Filtering

Status: Draft (revised after technical review against disko.py)
Author: Claude (drafted with jonathanba@nayax.com)
Related: README.md "Features"; disko.py (`_list_entries`, `_iter_children`, `renderTreemap`)

## 1. Summary

Today every disko treemap cell is colored by **sibling position** at the
current view (first child of a folder gets palette color 0, second gets
color 1, etc.). Colors are arbitrary and reset every time you drill into a
new folder. `renderTreemap`'s color pipeline also runs each color through a
generic depth-darkening step (`shade(colorOf(d), d.depth-1)`, disko.py:1394),
but that step is currently a no-op in the shipped app: disko's treemap is
always exactly one level deep (root → leaves), so every rendered cell has
`d.depth === 1` and `shade(color, 0)` returns the color unchanged. The
depth-darkening code path exists but never actually darkens anything today.

This PRD adds **file-type coloring**: cells representing files (or file
groups) get a color drawn from a fixed type→color mapping (Video, Images,
Archives, Documents, Code, Other, ...) that stays the same everywhere in the
app, plus a legend that lets you click a type to highlight it and dim
everything else.

## 2. Problem / Motivation

- Colors carry no information today — you can't tell "this red is all videos"
  because red might be a `node_modules` folder in one view and a Downloads
  folder in another.
- Users cleaning up disk space usually think in terms of *kind* of content
  ("where are my videos/old installers/archives"), not folder names.
- This is one of DaisyDisk's most recognizable features and the most
  natural next step after the treemap/breadcrumb work already shipped.

## 3. Goals

- G1: Assign a stable, meaningful color per file type, consistent across
  every view in the session — including the sidebar list, not just the
  treemap (see §6.3).
- G2: Show a legend with per-type totals for whatever is currently on
  screen, and make its scope (current folder's loose files only) explicit
  in the UI itself.
- G3: Let users click a legend entry to highlight matching cells and dim
  the rest (in-view filter, no re-scan).
- G4: Ship without regressing disko's core value proposition: fast,
  no-upfront-full-scan browsing driven by `du`.

## 4. Non-goals (v1)

- NG1: A single whole-volume "type report" like DaisyDisk's initial scan
  (Phase 2 candidate, see §8).
- NG2: Deleting/collecting files by type (that's feature #2 from the
  original list — separate PRD).
- NG3: User-customizable type→color mapping or user-defined categories
  (v1 ships a fixed, sensible default set).
- NG4: Per-file-level treemap leaves for every file in a folder (disko
  intentionally keeps one row per *subfolder*; this PRD only changes how
  the **loose files directly in the current folder** are represented and
  colored — see §6).
- NG5: Multi-select legend filtering (combining two types into one filter,
  e.g. "Video+Audio" as "media"). v1 supports one active filter type at a
  time; see §6.3 and open question Q4.

## 5. Background: why this isn't a pure front-end change

disko's scan model (disko.py:335-398):

- `_list_entries(path)` splits a folder's immediate entries into
  subdirectories (each later sized by spawning `du -sk -x --` on it,
  recursively, natively, cheaply — `-x` stays on one filesystem, matching
  `_split_mounts`'s own mount-boundary handling) and a **single summed
  total** of the loose (non-directory) entries in that folder.
- Subfolder sizes never carry any file-level detail — `du` gives back one
  integer per subfolder. disko does not know, and today has no cheap way
  to know, "how many of the 4GB under `Videos/` are `.mp4` vs `.srt`."

DaisyDisk's global, consistent-across-the-tree type coloring depends on
having per-file extension data for *everything currently rendered*, which
in DaisyDisk's case is "the whole disk," scanned recursively file-by-file
up front. Doing the same thing in disko (recursively `scandir`-ing every
file under every subfolder instead of delegating to `du`) would trade
away the exact performance property that makes disko fast on huge trees
(parallel native `du` processes vs. a Python-side recursive walk).

So v1 deliberately narrows scope: **classify only the files disko already
touches directly** — the loose files in whatever folder you're currently
looking at — and treat subfolders as before (sized as a whole, not
decomposed by type). This is close to free: `_list_entries` already calls
`_alloc_size(e)` on every loose file today to build the single
`file_total` sum (disko.py:341); turning that reduce-to-one-int into a
reduce-to-a-dict-keyed-by-extension adds **zero new syscalls** and O(1)
extra CPU per entry — the same order of cost as today, which is the
concrete argument for why this stays inside goal G4's performance budget.
Phase 2 (§8) sketches an opt-in, on-demand way to get DaisyDisk-style
full-subtree type totals without slowing down normal browsing.

### 5.1 Alternatives considered (rejected)

- **One-level sampling** (scandir each subfolder's own direct loose files
  to guess its color, without a full recursive walk): rejected. disko
  never `scandir`s subdirectories today — only `du_bounded` is dispatched
  per subdir (disko.py:358-359) — so sampling would add a new syscall
  per subfolder, and worse, it produces a *confidently colored but
  frequently wrong* signal: a folder's direct children are often
  unrepresentative of what's several levels deeper. A user trusting a
  sampled color swatch to decide what to delete could make a worse
  decision than with no signal at all. Leaving subfolders uncolored is
  the safer default.
- **Background full-tree indexing** (a daemon that walks the whole disk
  once and keeps a live type index): rejected for v1 as a much larger
  scope and cost than this PRD's goal; the closest legitimate version of
  this idea is Phase 2's opt-in, subtree-scoped, on-demand analysis.
- **`find`/`du`-based full walk of subfolders for type totals**: rejected;
  this is exactly the "trade away the performance property" case flagged
  above and the reason v1 stops at the current folder's loose files.

## 6. Proposed design (v1)

### 6.1 Data model change

Replace the single synthetic `(loose files)` child with one synthetic
child **per file-type bucket** present in that folder, e.g.:

```jsonc
// today (disko.py:377-383):
{ "name": "(loose files)", "path": "/foo", "size": 41943040, "isDir": false }

// proposed:
{ "name": "(images)",   "path": "/foo", "size": 20971520, "isDir": false, "fileType": "image" }
{ "name": "(video)",    "path": "/foo", "size": 15728640, "isDir": false, "fileType": "video" }
{ "name": "(other)",    "path": "/foo", "size":  5242880, "isDir": false, "fileType": "other" }
```

- `path` stays the folder's path (these are still synthetic aggregate
  nodes, not real files — clicking them does nothing today, same as
  `(loose files)` today).
- A new optional `fileType` key drives color; absent/`null` means
  "color by sibling index" (subfolders, mounts — unchanged behavior).
- Default type buckets (extension-keyed, case-insensitive), matching
  DaisyDisk's rough categories:
  - `image`: jpg, jpeg, png, gif, heic, webp, svg, bmp, tiff, raw, ...
  - `video`: mp4, mov, mkv, avi, webm, m4v, ...
  - `audio`: mp3, wav, flac, aac, m4a, ogg, ...
  - `archive`: zip, tar, gz, tgz, bz2, xz, 7z, rar, dmg, iso, ...
  - `document`: pdf, doc(x), xls(x), ppt(x), txt, md, epub, ...
  - `code`: py, js, ts, go, rs, c, cpp, java, json, yaml, html, css, ...
  - `other`: anything unmatched, plus files with no extension, plus
    entries whose `stat()` failed (folded together — see §6.4 for why
    these two are otherwise easy to mistakenly conflate).
  Ship the mapping as one Python dict constant, easy to extend later.
- `os.path.splitext` only ever sees the *last* dot-segment. `.tar.gz` and
  `.tar.bz2` happen to resolve correctly today because their last segment
  (`gz`/`bz2`) is itself in the archive list, but other compound archive
  extensions (`.tar.zst`, `.tar.lz4`, `.tar.xz`) would silently fall to
  `other` under a naive single-suffix match. v1 special-cases the same
  handful of compound suffixes the extension table already implies
  (`tar.gz`, `tar.bz2`, `tar.xz`, `tar.zst`, `tar.lz4`) by checking the
  last two dot-segments before falling back to the last one alone.
- A dotfile with no real extension (`os.path.splitext('.bashrc')` →
  `('.bashrc', '')`, no extension) lands in `other`, same as any other
  extensionless file — it is not a special case.
- Symlinks: `os.scandir`'s `e.is_dir(follow_symlinks=False)` is `False`
  for a symlink even when it points at a directory, so symlinks already
  count as loose entries today, measured via `_alloc_size`'s own
  `stat(follow_symlinks=False)` (near-zero size — just the link path
  length). Under the new scheme a symlink is bucketed by its *link
  name's* extension, same as any other loose entry. This is unchanged,
  intended behavior, not a new edge case to design around.
- `TYPE_COLORS.other` must be a color **visually distinct** from the
  existing `OTHER_COLOR` (`#475569`, disko.py:909) already used for
  `capChildren`'s "N smaller items" overflow aggregate (see §6.3) — those
  are two unrelated concepts (a real file-type bucket vs. a generic
  overflow placeholder) and must not look the same in the same view.

### 6.2 Backend changes

- `_list_entries` (disko.py:335) changes shape from `(dirs, file_total:
  int)` to `(dirs, file_totals: dict[str, int])`, grouping non-dir
  entries by type bucket (classified via `os.path.splitext(e.name)` per
  §6.1) instead of summing them into one int, still using `_alloc_size(e)`
  for the per-entry cost — no new syscalls, just a dict-of-sums instead
  of one sum. Update its docstring (currently "Return (subdirs, loose
  file total)").
- `_iter_children` (disko.py:345) takes `file_totals: dict[str, int]` in
  place of `file_total: int` and, where it currently yields a single
  `(loose files)` dict (disko.py:377-383), instead loops over
  `sorted(file_totals.items())` and yields one child dict per non-empty
  bucket: `{'name': f'({label})', 'path': path, 'size': total,
  'isDir': False, 'fileType': bucket}`. Update its docstring and its two
  call sites: `scan_to_list` (disko.py:396) and `stream_directory`'s live
  scan (disko.py:608).
- **`stream_directory`'s progress-count regression**: the live-scan path
  (disko.py:594-596) hard-codes the SSE `start` event's `total_dirs` as
  `len(dirs) + (1 if file_total > 0 else 0)` — one slot reserved for the
  single `(loose files)` entry. Once loose files split into N type
  buckets, this undercounts and the stream will send more `child` events
  than `total_dirs` promised (a progress bar reading, e.g., "6/5"). This
  must become `len(dirs) + number_of_nonempty_buckets`, computed after
  classification runs and before the `start` event is written. The
  cached/stale-refresh path's `refresh` event (disko.py:578,
  `'total_dirs': len(fresh['children'])`) already counts actual children
  generically and needs no change — only the *live*-scan formula is
  wrong.
- **Cache schema** (`cache_get`/`cache_set`, disko.py:98-123): there is no
  `CACHE_VERSION` or any versioning field anywhere in the cache code today
  (confirmed by grep — only `__version__`, the app version string,
  exists). `cache_load()` (disko.py:48-63) accepts any dict-shaped JSON
  file wholesale into `_cache`, and `cache_set()` stores
  `{'children': ..., 'scanned_at': ...}` per path key with nothing else.
  Two shapes for a version bump were weighed:
  - a whole-file version envelope (e.g. `{"version": N, "paths": {...}}`)
    — rejected: an *old*, un-enveloped cache file (`{path: {...}, ...}`)
    is still a valid dict, so `cache_load` would silently assign the
    *new* envelope object itself to `_cache` on first load after upgrade,
    before any entry exists in the new shape — every subsequent
    `cache_get` would then return `None` forever (a **permanent** cache
    miss), not the intended one-time re-scan.
  - a **per-entry `version` field**, stored inside each `_cache[key]`
    dict alongside `children`/`scanned_at`, checked by `cache_get` (and
    written by `cache_set`) — **adopted**. It's backward compatible for
    free: an old on-disk entry simply lacks the key, `cache_get` treats a
    missing/mismatched version as a miss (returns `None`; the stale entry
    is naturally replaced on the next `cache_save`), and neither the
    on-disk file shape nor `cache_load`'s `isinstance(data, dict)` check
    needs to change.
  This makes an old `(loose files)`-shaped entry fail closed as exactly
  the "one clean re-scan" this PRD wants, rather than the whole-envelope
  approach's risk of a silent, permanent regression. Invalidation is
  lazy/per-access, not an eager full-cache wipe — only a folder actually
  reopened after the upgrade pays for a re-scan, so there's no rescan
  storm right after upgrade. A folder mid-migration shows the ordinary
  loading/streaming state, the same as any other cache miss — never a
  stale mixed old/new shape.
- No new SSE event types needed — this rides the existing per-child
  streaming protocol; each bucket just streams as its own child dict.

### 6.3 Frontend changes

- **Color mapping**: `renderTreemap`'s `colorOf` (disko.py:1379) and
  `renderSidebar`'s per-row coloring (disko.py:1451, currently
  `PALETTE[i%PALETTE.length]` unconditionally for every item) must
  resolve through the **same** shared color-resolution logic: a node
  with `data.fileType` gets its color from a fixed `TYPE_COLORS` map;
  a node without `fileType` keeps today's sibling-index coloring.
  Extracting `colorOf` into a small shared helper both call sites use is
  the natural way to do this. Without this change, sidebar dots for a
  type bucket would stay arbitrary/index-colored while the treemap uses
  the fixed type color for the same node — breaking goal G1's
  cross-view consistency the moment this ships.
- **Interaction with `capChildren`/`MAX_LEAVES`**: type-bucket nodes are
  exempt from `capChildren`'s (disko.py:961) `MAX_LEAVES` (500) folding.
  A folder has at most a handful of buckets (one per `TYPE_COLORS`
  category, ≤ 8), so `capChildren` reserves the fold for ordinary
  subfolder/mount children and always keeps every bucket node as its own
  cell, even past the fold threshold. This avoids a folder's child count
  tipping past 500 and silently absorbing a bucket node — the very thing
  the new legend describes — into the generic gray "N smaller items"
  aggregate.
- **Legend**: a new panel inside the existing sidebar column (so it
  inherits the sidebar's current ≤760px stacking behavior, disko.py:832,
  with no separate responsive breakpoint to design), listing each type
  bucket present in the *current view* — **view-scoped, not global**
  (resolves open question Q3): only types with a nonzero bucket in the
  folder currently on screen are listed, matching v1's "in-view only"
  philosophy used elsewhere in this design. The legend states its scope
  explicitly in its own UI, not just in this document's prose — a
  persistent caption such as "sizes shown are for loose files in this
  folder only" — since totals never include subfolder contents and
  DaisyDisk (the cited mental model) would otherwise lead users to
  misread a partial total as a whole-subtree total. Each entry shows a
  color swatch, label, and total size (sum across all bucket nodes
  currently visible — for v1 this is arithmetic disko already has, no
  extra per-view scan).
- **Filter/highlight**: clicking a legend entry sets a piece of JS state
  (`activeFilterType`: a single bucket key or `null`) rather than just
  toggling a CSS class, and a render-time step in **both**
  `renderTreemap` and `renderSidebar` applies dim/highlight styling from
  that state every time either function runs. This matters because
  `renderTreemap` fully rebuilds the SVG DOM on every frame during
  streaming — the same "state gets wiped on every re-render" problem the
  codebase already solves for tooltips via `refreshTooltip`
  (disko.py:1352) — so a plain one-time CSS toggle would be silently
  lost on the next streamed child or the next resize-driven re-render.
  `activeFilterType` persists across a same-folder refresh/revalidate
  (the filter describes *this view's* content, and a refresh doesn't
  change what's being asked about) and is cleared on `goTo` navigation
  into a different folder (a different folder has an unrelated set of
  buckets, so carrying a stale filter forward would silently hide things
  with no visible cause). v1 supports **one active filter type at a
  time** (NG5) — clicking a second entry replaces the filter rather than
  combining with it; clicking the active entry again (or a "clear"
  control) restores normal coloring. Only cells/rows with a matching
  `fileType` count as "this type" for highlighting; subfolders (no
  `fileType`) always dim to a distinct, intermediate opacity when *any*
  filter is active — visibly different from both full brightness and the
  "wrong type" dim level — since v1 doesn't know their internal
  breakdown. Dimming is purely visual: the existing cell click/keydown
  handlers (disko.py:1418, 1430) are unchanged, so a dimmed subfolder
  still opens normally. The dim/highlight state and its visual treatment
  apply to **both** the treemap's `.cell` elements and the sidebar's
  `.sitem` rows for the same view, so a legend click doesn't leave one
  list bright while the other dims.
- **Legend accessibility**, matching the app's existing conventions
  (`role=button`, `aria-label`, keyboard activation, `:focus-visible`,
  `aria-live=polite` status regions already used elsewhere in this file):
  each legend entry gets `role="button"` and `aria-pressed` reflecting
  whether it's the active filter, Enter/Space to toggle (consistent with
  the existing cell keyboard handler), `Escape` to clear the active
  filter (consistent with the existing Backspace/Left-arrow navigation
  shortcuts), and an `aria-live="polite"` region announcing the resulting
  filter state (e.g. "Showing video files only" / "Filter cleared").
- **Color accessibility**: the `TYPE_COLORS` palette must be run through
  a color-blindness simulator and checked against `labelColor()`'s
  contrast logic (disko.py:1319) before it's finalized — this is the
  first time color becomes semantically meaningful (a hue means "this is
  a video") rather than purely positional, so a hue collision is a real
  usability regression, not just an aesthetic one. Below the existing
  label threshold (`cw>45&&ch>22`, disko.py:1401) type is conveyed by hue
  alone with no text fallback; v1 accepts this (consistent with how
  size/position already work at that scale), but the chosen palette must
  keep same-size cells of different types clearly distinguishable even
  under common color-vision deficiencies.
- **Tooltip**: show the resolved type label ("Video files", "Documents")
  for bucketed nodes alongside existing size/percentage info.

### 6.4 Edge cases

- A folder with only one file type present shows only one bucket (no
  empty "(other: 0 B)" nodes).
- Two distinct cases that are easy to conflate, and must be handled
  differently:
  - Files that fail `stat()` (permission errors, races) fall out of
    `_alloc_size` as `0` today (disko.py:186-193) and stay invisible in
    the treemap under the new scheme too — same as today, no behavior
    change.
  - Hidden/dotfiles that **can** be `stat`'ed — the common case, since
    most dotfiles are just small readable config files — get their real
    allocated size and are classified like any other file with no
    recognized extension (`other`), with that real size, **visibly**
    contributing to the `other` bucket's total. They are not invisible.
- Symlinks: unchanged/intended behavior — see §6.1.
- Mount points and mid-scan "unknown"/"timed out" subfolder entries are
  unaffected — they have no `fileType` and keep current dashed-tile
  treatment.
- Extension matching is done on the *file name*, not content-sniffed — a
  renamed file will be miscategorized, same limitation DaisyDisk has.
  Matching is case-insensitive and checks compound suffixes (`.tar.gz`
  etc., see §6.1) before falling back to the last single suffix.

## 7. Rollout plan

1. **Backend**: bucket classification (`_list_entries`/`_iter_children`
   signature change per §6.2), `stream_directory`'s `total_dirs` fix, and
   the per-entry cache version field. The existing scan tests do **not**
   already cover multiple type buckets — the only fixture (`_make_tree`,
   tests/test_disko.py:48) has loose files that are all `.txt`, so every
   currently-passing assertion about `(loose files)` would keep passing
   by accident (one bucket, coincidentally) without ever exercising the
   new multi-bucket path. This step must:
   - rewrite the tests that hardcode the old single-`(loose
     files)`-entry shape: `test_disko.py:140`
     (`{'big','small','empty','(loose files)'}`), `:147-154` (single
     loose-total assertion), `:166`
     (`test_sparse_file_counts_allocated_blocks`, filters on the literal
     name), `:207` (`test_scan_normalizes_path`), and
     `TestHTTPSmoke.test_stream_emits_events_then_done` (`:674-679`,
     asserts both the name set and `total_dirs == 4`);
   - extend `_make_tree` (or add a dedicated fixture) with loose files
     spanning ≥ 3 type buckets (e.g. one image, one video, one archive)
     in the same folder, and assert: multiple bucket entries appear with
     correct per-bucket sizes, a folder with only one type produces
     exactly one bucket, and the streamed child count matches
     `total_dirs` exactly (regression test for the progress-percentage
     bug fixed in §6.2);
   - add fixture-level cases for the extension-matching edge cases in
     §6.4: a dotfile with no real extension, a mixed-case extension
     (`IMG.JPG`), and a multi-part extension (`archive.tar.gz`);
   - add a cache-versioning test: write an old-format (no `version` key)
     entry directly to a temp `CACHE_FILE`, call `cache_load()`, and
     assert `cache_get()` treats it as a miss (returns `None`, triggering
     exactly one re-scan) rather than crashing or rendering the stale
     shape.
2. **Frontend**: type-colored buckets rendering correctly in both the
   treemap (`colorOf`) and the sidebar (shared color-resolution helper,
   §6.3), sibling-indexed coloring unchanged for subfolders/mounts. The
   repo has no JS/browser test tooling today (no `package.json`, no
   jsdom/Playwright/Jest, and `TestHTTPSmoke` only checks that SSE/HTML
   bytes come back, never DOM behavior) — this step pairs a small
   testability seam (the bucket→color resolution and classification
   logic written as plain, non-DOM functions, so they could later back a
   headless smoke test) with a concrete manual QA checklist instead of a
   claim of automated coverage: open a folder with ≥ 3 loose file types
   and confirm treemap cells and sidebar dots for the same bucket match;
   resize the window through the 760px breakpoint; force a slow scan (a
   large folder) and confirm the progress counter never exceeds 100%.
3. **Legend UI + click-to-filter/dim**, per the accessibility and
   lifecycle spec in §6.3. Manual QA: click a legend entry, confirm
   treemap + sidebar dim together and the state survives a per-folder
   refresh; navigate into a subfolder and confirm the filter clears;
   drive the legend by keyboard only (Tab, Enter/Space, Escape) and
   confirm the `aria-live` announcement fires.
4. Update README "Features" list and screenshot.

Each step is independently shippable; step 1 is testable with the
existing Python `unittest` harness (extended as above), steps 2-3 rely on
manual QA in the absence of frontend test tooling (see step 2). No SSE
protocol version bump required since the new field (`fileType`) is
additive.

## 8. Phase 2 (future, explicitly out of scope for v1)

An on-demand **"Analyze types"** action per folder (a button next to the
existing per-folder refresh button) that does a real recursive walk of
that subtree only when the user asks for it — mirroring how DaisyDisk's
full scan works, but scoped to one subtree instead of the whole disk, so
normal browsing stays instant. To keep the performance principle from §5
intact into Phase 2, this must be implemented as a **native subprocess**
(e.g. a single `find`-based pass, mirroring `du_single`'s subprocess
pattern, disko.py:223-254) rather than a Python-side `os.walk`/`scandir`
recursion — a GIL-bound Python walk over a subtree with millions of files
(a `node_modules`, a Time-Machine-style backup, a network share) would
reintroduce exactly the I/O contention this PRD's v1 design avoids. It
should be routed through the existing `_bg_slots`/`_live_cv` priority
scheme (disko.py:257-315) so it never blocks a live scan. Output: a
summary (not a treemap necessarily) of size-by-type totals across the
whole subtree, cached separately from the regular scan cache since it's a
materially heavier operation. This is the feature that would make "filter
by type" meaningful across multiple folder levels at once, matching
DaisyDisk more closely — deliberately deferred so v1 ships fast and
cheap.

## 9. Open questions for you

- Q1 (resolved): the "only loose files in the current folder get typed,
  subfolders don't" scope is the right v1 bar. §5.1 lists why the
  alternatives (sampling, background indexing, full walk) don't clear
  goal G4's cost bar; Phase 2 (§8) is the answer for anyone who wants
  full-subtree totals, not a v1 blocker.
- Q2: no exact `TYPE_COLORS` hex values are finalized yet — DaisyDisk's
  rough category set (video/image/audio/archive/document/code/other) is
  the right starting taxonomy, but the specific colors still need to
  pass the color-blindness and contrast check required by §6.3 before
  shipping, and `other` must stay visually distinct from the existing
  `OTHER_COLOR` aggregate gray.
- Q3 (resolved): the legend is view-scoped (types present in the current
  folder only), not global — see §6.3.
- Q4 (new): is a single-select legend filter (v1's decision, §6.3 / NG5)
  sufficient, or is combining categories (e.g. "Video+Audio" as one
  filter) worth the added interaction complexity for a later iteration?
