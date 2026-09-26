# PRD: File-Type Color Coding & Filtering

Status: Draft
Author: Claude (drafted with jonathanba@nayax.com)
Related: README.md "Features"; disko.py (`_list_entries`, `_iter_children`, `renderTreemap`)

## 1. Summary

Today every disko treemap cell is colored by **sibling position** at the current
view (first child of a folder gets palette color 0, second gets color 1, etc.),
shaded darker with depth. Colors are arbitrary and reset every time you drill
into a new folder.

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
  every view in the session.
- G2: Show a legend with per-type totals for whatever is currently on
  screen.
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

## 5. Background: why this isn't a pure front-end change

disko's scan model (disko.py:335-398):

- `_list_entries(path)` splits a folder's immediate entries into
  subdirectories (each later sized by spawning `du -sk` on it,
  recursively, natively, cheaply) and a **single summed total** of the
  loose (non-directory) entries in that folder.
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
decomposed by type). Phase 2 (§8) sketches an opt-in, on-demand way to get
DaisyDisk-style full-subtree type totals without slowing down normal
browsing.

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
  - `other`: anything unmatched, plus files with no extension.
  - `unknown`: reserved for entries whose stat failed (edge case, rare;
    folded into `other` if simpler).
  Ship the mapping as one Python dict constant, easy to extend later.

### 6.2 Backend changes

- `_list_entries` (disko.py:335): instead of summing all non-dir entries
  into one `file_total` int, group them into a `dict[str, int]` keyed by
  type bucket (classify by `os.path.splitext(e.name)`, still using
  `_alloc_size(e)` for the per-file cost — no new syscalls, just a
  dict-of-sums instead of one sum). This is O(same work) as today.
- `_iter_children` (disko.py:345): yield one child dict per non-empty
  bucket instead of one `(loose files)` dict, sorted by size like
  everything else.
- Cache schema (`cache_set`/`cache_get`, disko.py:98-124): cached
  children already store arbitrary child dicts, so this is forward
  compatible — but bump a cache format version constant so existing
  `~/.disko_cache.json` entries (with the old single `(loose files)`
  shape) are treated as stale and re-scanned once, rather than rendered
  with a missing `fileType`. (Check how `CACHE_VERSION`/similar is
  currently keyed before assuming a bump mechanism exists — add one if
  not.)
- No new SSE event types needed — this rides the existing per-child
  streaming protocol; each bucket just streams as its own child dict.

### 6.3 Frontend changes

- **Color mapping**: replace `colorOf` in `renderTreemap` (disko.py:1379)
  so that a node with `data.fileType` gets its color from a fixed
  `TYPE_COLORS` map (not from `colorIdx`/sibling position); nodes without
  `fileType` keep today's sibling-index coloring unchanged.
- **Legend**: new panel (sidebar or footer) listing each type bucket
  present in the *current view* with its color swatch, label, and total
  size (sum across all cells of that type — for v1 this is just the
  bucket nodes already computed server-side, no extra client math beyond
  a per-view sum since subfolders aren't decomposed).
- **Filter/highlight**: clicking a legend entry toggles a "dim everything
  that isn't this type" state — implemented purely client-side (CSS
  opacity on non-matching `.cell` elements), no re-fetch. Clicking again
  (or a "clear" control) restores normal coloring. This only affects
  cells with a matching `fileType`; subfolders (no `fileType`) always
  dim when a type filter is active, since we don't know their internal
  breakdown in v1 (make this visually obvious — e.g. dim them slightly
  less than "wrong type" cells, or label the legend "within this
  folder" so the scope is clear).
- **Tooltip**: show the resolved type label ("Video files", "Documents")
  for bucketed nodes alongside existing size/percentage info.

### 6.4 Edge cases

- A folder with only one file type present shows only one bucket (no
  empty "(other: 0 B)" nodes).
- Hidden/dotfiles, files disko can't `stat` (permission errors) already
  fall out of `_alloc_size` as 0 — they land in `other` with size 0 and
  are simply invisible in the treemap (same as today).
- Mount points and mid-scan "unknown"/"timed out" subfolder entries are
  unaffected — they have no `fileType` and keep current dashed-tile
  treatment.
- Extension matching is done on the *file name*, not content-sniffed —
  a renamed file will be miscategorized, same limitation DaisyDisk has.

## 7. Rollout plan

1. Backend: bucket classification + cache version bump. Verify via
   existing `tests/` scan tests with a fixture directory containing
   mixed file types.
2. Frontend: type-colored buckets rendering correctly, sibling-indexed
   coloring unchanged for subfolders/mounts.
3. Legend UI + click-to-filter/dim.
4. Update README "Features" list and screenshot.

Each step is independently shippable and testable; no SSE protocol
version bump required since new fields are additive.

## 8. Phase 2 (future, explicitly out of scope for v1)

An on-demand **"Analyze types"** action per folder (a button next to the
existing per-folder refresh button) that does a real recursive walk of
that subtree only when the user asks for it — mirroring how DaisyDisk's
full scan works, but scoped to one subtree instead of the whole disk, so
normal browsing stays instant. Output: a summary (not a treemap
necessarily) of size-by-type totals across the whole subtree, cached
separately from the regular scan cache since it's a materially heavier
operation. This is the feature that would make "filter by type" meaningful
across multiple folder levels at once, matching DaisyDisk more closely —
deliberately deferred so v1 ships fast and cheap.

## 9. Open questions for you

- Q1: Is the "only loose files in the current folder get typed, subfolders
  don't" scope acceptable for v1, or is Phase 2 (opt-in recursive
  analyze) actually the minimum bar you want to ship?
- Q2: Any preference on the default type→color palette, or is DaisyDisk's
  rough category set (video/image/audio/archive/document/code/other)
  good enough to start?
- Q3: Should the type legend be global (all types disko knows about,
  even ones absent from the current view, grayed out) or view-scoped
  (only types actually present)?
