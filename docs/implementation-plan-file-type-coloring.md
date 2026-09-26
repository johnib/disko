# Implementation + Test Plan: File-Type Color Coding & Filtering

Status: Final (drafted, peer-reviewed across 5 engineering lenses, synthesized)
Related: `docs/prd-file-type-coloring.md`

This plan was produced in three passes: (1) an agent drafted it directly
from the finalized PRD and the live codebase; (2) five independent agents
reviewed the draft from separate engineering lenses — feasibility,
test rigor, performance/scale, frontend/UX/accessibility, and
backward-compatibility/migration; (3) a synthesis pass incorporated every
valid finding, resolving two confirmed blocking bugs the draft would have
shipped (a `NameError` from a rename split across two "independent"
commits, and a sidebar truncation path that bypassed the treemap's
`>500`-children bucket-exemption guarantee). See the plan body for full
detail on every fix and the reasoning behind it.

---

## Implementation + test plan — file-type color coding & filtering (final)

Grounded against the live repo: `docs/prd-file-type-coloring.md` (449 lines) and the current `disko.py` (1680 lines) / `tests/test_disko.py` (776 lines). All line references cited below were re-verified directly against the live files during this review pass (including the specific lines the peer reviews flagged — `disko.py:589/596` inside `stream_directory`, `cache_set`'s raw `_cache.get(key)` at `disko.py:115`, `_propagate_size`'s raw `_cache.get(parent)` at `disko.py:133`, `cache_load`'s top-level-only validation at `disko.py:48-63`, `schedule_prefetch`/`_do_prefetch` at `disko.py:453-479` with `PREFETCH_TOP_N=10`/`PREFETCH_MAX_DEPTH=1`/`SCAN_WORKERS=12`, `capChildren`'s single call site from `renderTreemap` at `disko.py:1368`, `renderSidebar`'s independent `MAX_SIDEBAR=500` slice at `disko.py:908/1447`, and the pre-existing assertion at `test_disko.py:677`) — all matched exactly, no drift.

Five independent review passes (feasibility, test rigor, performance, frontend/UX, backward-compat) surfaced two **blocking correctness bugs** in the draft that are fixed below (a `NameError` on every live scan, and a plan that never mirrors its own `>500 children` bucket-exemption into the sidebar), plus a cluster of hardening fixes to tests, cache-migration safety, and accessibility. Every fix is incorporated in place; conflicting recommendations are resolved with an explicit judgement call noted inline where they occur, rather than in a separate list.

The PRD's §7 groups "backend" as one step. This plan still separates the backend work into three clearly-scoped diffs (classification → dict-of-buckets/counter fix → cache versioning) for **review** clarity, but — unlike the draft — it now states plainly that the last two of those diffs cannot be independently committed to trunk or deployed alone (see "Commit/PR granularity" for why, and the specific bug that proves it).

---

### Step 1 — Classification constants + pure `classify_file()` helper

**What/where:** New code only, inserted in the "Scanning" section just above `_list_entries` (disko.py:335), colocated with the code that will call it in Step 2.

```python
FILE_TYPE_EXTENSIONS = {
    'image':   {'jpg','jpeg','png','gif','heic','webp','svg','bmp','tiff','tif','raw','cr2','nef','ico'},
    'video':   {'mp4','mov','mkv','avi','webm','m4v','flv','wmv','mpg','mpeg'},
    'audio':   {'mp3','wav','flac','aac','m4a','ogg','wma','opus'},
    'archive': {'zip','tar','gz','tgz','bz2','xz','7z','rar','dmg','iso','zst','lz4'},
    'document':{'pdf','doc','docx','xls','xlsx','ppt','pptx','txt','md','epub','rtf','odt','csv'},
    'code':    {'py','js','ts','jsx','tsx','go','rs','c','cc','cpp','h','hpp','java','json',
                'yaml','yml','html','css','sh','rb','php','swift','kt'},
}
# Checked before the last single suffix (PRD §6.1): os.path.splitext only sees the last
# dot-segment, so 'archive.tar.zst' would otherwise fall to 'other'.
# NOTE (test-rigor review): every suffix in this set has its trailing segment (gz/bz2/xz/
# zst/lz4) ALSO listed standalone in FILE_TYPE_EXTENSIONS['archive'] above, because those are
# legitimate standalone-file extensions too (e.g. a lone gzipped file, `access.log.gz`). That
# means this compound branch is currently redundant for every listed suffix -- the single-
# suffix fallback alone already gets '.tar.gz' etc. right. It stays in the design because (a) a
# future compound suffix might reference a segment that ISN'T independently archive-mapped, and
# (b) it's cheap and self-documenting. See Step 1's tests for how this redundancy is verified
# and isolated rather than glossed over.
COMPOUND_ARCHIVE_SUFFIXES = {'tar.gz', 'tar.bz2', 'tar.xz', 'tar.zst', 'tar.lz4'}

_EXT_TO_TYPE = {ext: t for t, exts in FILE_TYPE_EXTENSIONS.items() for ext in exts}

def classify_file(name: str) -> str:
    """Return name's file-type bucket (image/video/audio/archive/document/code/other).

    Case-insensitive; checks the last two dot-segments against COMPOUND_ARCHIVE_SUFFIXES
    before falling back to the single last suffix. 'other' covers unmatched, extensionless,
    and dotfile-with-no-extension names (see PRD §6.1/§6.4) -- this function never raises."""
    lower = name.lower()
    parts = lower.rsplit('.', 2)
    if len(parts) == 3 and '.'.join(parts[1:]) in COMPOUND_ARCHIVE_SUFFIXES:
        return 'archive'
    ext = os.path.splitext(lower)[1].lstrip('.')
    return _EXT_TO_TYPE.get(ext, 'other')
```

**Before/after behavior:** Before: no concept of file type exists in the backend. After: a pure, stateless function exists and is unit-testable, but nothing calls it yet — zero behavior change to any existing endpoint or test.

**Tests to add** (new `TestClassifyFile(unittest.TestCase)` class in test_disko.py, no fixture/tree needed since it's pure):

- `test_common_extensions_map_to_expected_bucket` — table-driven: `photo.jpg`→image, `movie.mp4`→video, `song.mp3`→audio, `archive.zip`→archive, `report.pdf`→document, `script.py`→code.
- `test_case_insensitive` — `IMG.JPG`→image, `Movie.MP4`→video.
- `test_unmatched_and_extensionless_fall_to_other` — `data.xyz123`→other, `noextension`→other, `.bashrc`→other (dotfile with no real extension, PRD §6.1's explicit non-special-case).
- `test_compound_archive_suffixes` — table-driven over `x.tar.gz`/`x.tar.bz2`/`x.tar.xz`/`x.tar.zst`/`x.tar.lz4`, each asserted → `archive`. **Kept as an end-user-correctness check, but reframed** (test-rigor review, critical): a code comment on the test states explicitly that this loop passes via the single-suffix fallback alone for every listed case, and is *not* proof the compound branch executed — see the next test for that proof.
- **`test_compound_suffix_branch_is_actually_exercised` (new)** — isolates the compound branch from the fallback by removing the trailing segment's own single-extension mapping inside the test, so a pass can only come from `COMPOUND_ARCHIVE_SUFFIXES`:
  ```python
  def test_compound_suffix_branch_is_actually_exercised(self):
      with unittest.mock.patch.dict(disko._EXT_TO_TYPE):
          del disko._EXT_TO_TYPE['gz']  # patch.dict restores the full original mapping on exit
          self.assertEqual(disko.classify_file('x.tar.gz'), 'archive')
  ```
  This is the actual regression guard for PRD §6.1's gap; the previous test alone was not.
- `test_case_insensitive_compound_suffix` (new, minor per test-rigor review) — `ARCHIVE.TAR.GZ` → archive, since PRD §6.4 describes case-insensitivity and compound-suffix handling as one combined behavior and the draft only ever tested them in isolation.
- `test_no_extension_listed_in_multiple_buckets` (new, minor per test-rigor review) — table invariant guarding against a future copy/paste duplicating an extension across two buckets (silently order-dependent via dict overwrite otherwise, with no test catching it):
  ```python
  def test_no_extension_listed_in_multiple_buckets(self):
      seen = {}
      for bucket, exts in disko.FILE_TYPE_EXTENSIONS.items():
          for ext in exts:
              self.assertNotIn(ext, seen, f'{ext!r} listed in both {seen.get(ext)!r} and {bucket!r}')
              seen[ext] = bucket
  ```

**Manual verification:** N/A — fully unit-testable.

**Risk:** Low. Pure function, additive, no call sites yet, so it can't regress anything currently passing.

---

### Step 2 — Backend: dict-of-buckets scanning + progress-counter fix (ships as one commit/deploy unit — see why below)

> **Why this is one unit, not two independently-committable steps (feasibility review, critical, confirmed by direct read of the live code):** the draft's Step 2 renames `file_total`→`file_totals` at the `stream_directory` unpack (disko.py:589) but explicitly told the implementer to leave the counter line (disko.py:596, which reads the *old* name) untouched "until Step 3." After the rename, `file_total` no longer exists in that scope at all — the live code confirms these are the *only* two references to that name in `stream_directory` — so that line raises `NameError` on every non-cached call, not "transiently wrong data" as the draft claimed. This falsifies the draft's premise that each commit passes the full suite alone. **Resolution:** the dict-of-buckets diff must itself leave `stream_directory` in a working (if not yet fully *correct*) state, and the counter-correctness fix lands as a second, stacked commit that is only ever squash-merged together with the first — trunk must never see the intermediate NameError-prone state. The two are shown separately below purely for reviewability.

#### 2a. `_list_entries` / `_iter_children` signature change (dict-of-buckets)

**Where:** `_list_entries` (disko.py:335-342) and `_iter_children` (disko.py:345-383), plus both call sites: `scan_to_list` (disko.py:392-396) and `stream_directory`'s live-scan branch (disko.py:589, 596, 608).

**After** (`_list_entries`):
```python
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
```

`_iter_children(path, dirs, file_totals, stop=None, background=None)` — replace the tail (disko.py:377-383) **and its docstring** (feasibility review, nit — the old docstring's `"(loose files)" entry` line goes stale otherwise):
```python
    """... yields dir children, then one entry per non-empty type bucket
    (e.g. '(images)', '(documents)') instead of a single '(loose files)' entry,
    each carrying fileType so the frontend can color/group/filter by type."""
    BUCKET_LABELS = {'image': 'images', 'video': 'video', 'audio': 'audio',
                      'archive': 'archives', 'document': 'documents', 'code': 'code', 'other': 'other'}
    for bucket, total in sorted(file_totals.items()):
        if total <= 0:
            continue
        yield {'name': f'({BUCKET_LABELS.get(bucket, bucket)})', 'path': path,
               'size': total, 'isDir': False, 'fileType': bucket}
```

**`stream_directory` — the fix that avoids the `NameError` (this is the delta from the draft):**
```python
    dirs, file_totals = _list_entries(path)   # disko.py:589, renamed
    ...
    # Counted before the mount split. NOTE: this undercounts by one slot for every
    # non-empty bucket beyond the first (e.g. 3 buckets => counted as 1, not 3) --
    # that's the known, deliberately-deferred bug this commit's stacked partner (2b)
    # fixes; what matters HERE is that this line references a name that exists.
    total_dirs = len(dirs) + (1 if any(v > 0 for v in file_totals.values()) else 0)
```
Call sites: `scan_to_list` — rename `file_total`→`file_totals` at both the unpack (disko.py:392) and the `_iter_children` call (disko.py:396). `stream_directory` — same rename at the `_iter_children` call at disko.py:608.

**Tests to add/rewrite** (in `tests/test_disko.py`):

- New `_make_type_tree(root)` fixture helper (modeled on `_make_tree` at :48), **defined as a fully standalone fixture, independent of `_make_tree`**, so bucket-count expectations are unambiguous (test-rigor review, minor). It creates its own `sub/` dir (to keep dir-related assertions comparable) plus these loose files, with the exact resulting bucket membership spelled out:
  - `photo.jpg`, `IMG.JPG` → **image**
  - `clip.mp4` → **video**
  - `notes.zip`, `backup.tar.gz` → **archive**
  - `report.pdf` → **document**
  - `script.py` → **code**
  - `README` (no extension), `.bashrc` (dotfile, no real extension) → **other**
  - → 6 non-empty buckets total (image, video, archive, document, code, other), no audio bucket present in this fixture.
- New `TestScanToListTypeBuckets(TempTreeMixin, unittest.TestCase)`:
  - `test_multiple_buckets_appear_with_correct_sizes` — scan the type-tree, assert one child dict per non-empty bucket, `fileType` set, and `size` equal to the sum of that bucket's files' `st_blocks*512`.
  - `test_single_type_folder_yields_one_bucket` — a folder with only `.txt` files still yields exactly one bucket child, no phantom `(other: 0 B)` entries (§6.4).
  - **`test_mixed_case_and_compound_extension_classified_correctly` — rewritten for concrete arithmetic** (test-rigor review, major: the draft's version only checked a bucket "exists", which wouldn't catch e.g. `IMG.JPG` silently landing in `other` while `photo.jpg` still lands correctly in `image`): assert `by_bucket['image']['size'] == alloc(photo.jpg) + alloc(IMG.JPG)` and `by_bucket['archive']['size'] == alloc(notes.zip) + alloc(backup.tar.gz)`, i.e. the mixed-case/compound file's own size must be present in the expected sum, not just an existence check.
  - `test_dotfile_with_no_extension_lands_in_other` — `.bashrc` contributes to the `other` bucket total (not invisible), asserted with the same precise-sum pattern.
- **Rewrite the now-broken assertions that hardcode the single `(loose files)` shape** (unchanged from draft, still correct): `test_scan_tree` (test_disko.py:137-159) → `.txt` files land in `document` (not `other`), rewrite `set(by_name) == {'big','small','empty','(documents)'}`; `test_sparse_file_counts_allocated_blocks` (:161-168) → filter by `c.get('fileType') is not None` instead of the literal name; `test_scan_normalizes_path` (:205-207) → same set-literal fix; `TestHTTPSmoke.test_stream_emits_events_then_done` (:667-679) → name-set becomes `{'big','small','empty','(documents)'}` (its `total_dirs==4` assertion is only numerically correct because this fixture still produces exactly one bucket — Step 2b's regression test below is what actually exercises the multi-bucket counter path). Grep `'(loose files)'` across the test file before landing, to confirm no other spot was missed.

**Manual verification:** N/A — fully covered by the Python test suite.

**Risk:** Medium-high (raised from the draft's "Medium," reflecting the confirmed NameError): this piece alone is not executable, and must not be merged to trunk without 2b stacked on top of it in the same squash-merge.

#### 2b. `stream_directory`'s `total_dirs` counter — correctness fix

**Where:** disko.py:594-596 (as renumbered by 2a), inside `stream_directory`'s live-scan branch. Stacked directly on top of 2a; never merged separately.

**After:**
```python
    # Counted before the mount split (mounts are emitted as children too), plus one slot
    # per non-empty type bucket that _iter_children will yield after the dirs, so progress
    # never exceeds 100% even when loose files span multiple buckets.
    nonempty_buckets = sum(1 for v in file_totals.values() if v > 0)
    total_dirs = len(dirs) + nonempty_buckets
```

**Tests to add/rewrite:**

- New test in `TestHTTPSmoke`, using `_make_type_tree` (6 non-empty buckets + the fixture's dirs): `test_stream_total_dirs_correct_with_multiple_buckets`.
  - **Fixed to give an absolute, independently-derived expected value, not a self-referential one** (test-rigor review, major — the draft's proposed assertion `total_dirs == sum(child events)` only proves the two counts derived from the same `file_totals` dict agree with each other via parallel filtering logic; it never proves either number matches ground truth, and never demonstrates the check would actually fail under the pre-fix formula):
    ```python
    def test_stream_total_dirs_correct_with_multiple_buckets(self):
        events = self._stream()  # against a type-tree root
        expected = len(KNOWN_DIRS) + len(KNOWN_NONEMPTY_BUCKETS)  # from the fixture spec, not from the code under test
        self.assertEqual(events[0]['total_dirs'], expected)
        # Extends the existing invariant already covered for the single-bucket
        # fixture at test_disko.py:677 to the multi-bucket case, which is exactly
        # where the pre-fix formula (len(dirs) + (1 if any bucket nonempty else 0))
        # undercounts: for this fixture it would have produced len(dirs)+1, strictly
        # less than `expected`, which is how this test would have caught the bug.
        self.assertEqual(events[0]['total_dirs'], sum(1 for e in events if e['type'] == 'child'))
    ```
- Zero-loose-files edge: reusing an `empty`-style fixture, assert `nonempty_buckets == 0` and `total_dirs == len(dirs)`.

**Manual verification:** Force a slow scan on a large real folder with mixed file types; confirm the progress bar/percentage never exceeds 100% and the final count matches exactly.

**Risk:** Low in isolation; see the header note above for why it cannot land without 2a.

#### 2c. Cache versioning (per-entry `version` field)

**Where:** New constant near the other module-level constants (disko.py:28-37, alongside `CACHE_FILE`), plus `cache_get` (disko.py:98-100) and `cache_set` (disko.py:103-122). Stacked on top of 2a/2b in the same commit/deploy unit (see the deploy-ordering hazard below for why).

**After:**
```python
CACHE_VERSION = 2  # bump when the cached child-dict shape changes incompatibly
                    # (v2: loose files became per-type-bucket entries with 'fileType')
...
def cache_get(path: str):
    with _cache_lock:
        entry = _cache.get(_cache_key(path))
        if entry is None:
            return None
        # entry.get('version') alone would raise AttributeError if some non-dict value
        # ever ends up in _cache (cache_load only validates the TOP-LEVEL JSON object is
        # a dict, disko.py:48-63 -- it never validates each per-path value), so guard
        # the shape explicitly rather than assuming it (backward-compat review, minor).
        if not isinstance(entry, dict) or entry.get('version') != CACHE_VERSION:
            return None  # old/foreign/malformed shape: treat as a miss, not a crash or stale render
        return entry
...
        _cache[key] = {'children': children, 'scanned_at': scanned_at, 'version': CACHE_VERSION}
```

No change to `cache_load()` (still accepts any top-level dict wholesale) or the on-disk file shape — the version gate lives in `cache_get`, per the PRD's adopted design (§6.2).

**Audit of every raw `_cache[...]`/`_cache.get(...)` access, not just `cache_get`/`cache_set`'s public bodies** (backward-compat review, critical — the draft's claim that "the version gate lives entirely in cache_get" is not quite true of the real code, which has two other raw access sites):

1. **`cache_set`'s own staleness check** (disko.py:115): `existing = _cache.get(key)` then `existing.get('scanned_at', 0) > scanned_at` — this bypasses `cache_get`'s new version gate entirely, reading the raw dict. It stays correct under mixed old/new-format data only because `scanned_at` is present and shape-agnostic in both formats, and a pre-upgrade entry is always chronologically older than any post-upgrade scan — an invariant that holds today but is worth stating explicitly rather than leaving implicit.
2. **`_propagate_size`'s ancestor read** (disko.py:133): `entry = _cache.get(parent)` — also raw. It only ever matches ancestor `children` entries where `isDir` is true and the path differs from the parent's own path; neither the old `(loose files)` shape nor the new per-bucket entries are ever selected by this matcher (both are `isDir: False` with `path` equal to the parent itself), and every field it reads/writes (`scanned_at`, `size`, `status`, `mount`) is common to both shapes. **Deliberately not stamping `version` here**, even though doing so would look like a tidy fix: an ancestor entry `_propagate_size` touches may still hold *its own* old-shaped, no-`fileType` loose-file bucket in its `children` list (from before the ancestor itself was last scanned) — stamping `version: CACHE_VERSION` onto it just because a *descendant's* size propagated in would silently mislabel that stale shape as current and defeat the whole migration guard. This is a deliberate no-op, not an oversight; see its own test below.
3. **`cache_delete`** (disko.py:157): only pops the entry, never reads its shape. Safe, no change needed.

**Deploy-ordering hazard, and why 2c cannot be its own commit either** (backward-compat review, critical): `cache_set` stamps `version: CACHE_VERSION` onto whatever `children` list it's handed, with zero relationship to whether that list actually came from 2a/2b's new bucketed `_iter_children`. If the `CACHE_VERSION` bump ever became live (e.g. via a partial rollout, a hotfix reverting only 2a/2b, or a merge-order slip) **before** 2a/2b's shape change was live, every directory scanned in that window would get cache_set-stamped `version: 2` while still holding the *old* `(loose files)` shape — a mislabeling that can't be fixed by bumping `CACHE_VERSION` again, since the next boundary has the identical problem. **Resolution:** 2a, 2b and 2c ship as a single atomic commit/deploy unit (see "Commit/PR granularity"); this is now a hard requirement, not the draft's "recommend landing together" framing.

**Tests to add:**

- `test_old_format_entry_is_treated_as_miss` — write `{self.root: {'children': [], 'scanned_at': time.time()}}` (no `'version'` key) directly to `disko.CACHE_FILE` as JSON, call `disko.cache_load()`, assert `disko.cache_get(self.root) is None`.
- `test_non_dict_cache_entry_treated_as_miss` (new, minor per backward-compat review) — `disko._cache[disko._cache_key(self.root)] = None`; assert `disko.cache_get(self.root) is None` rather than raising.
- Extend `test_set_save_load_get` with an assertion that a freshly-`cache_set` entry carries `entry.get('version') == disko.CACHE_VERSION`.
- **`test_stream_rescans_once_after_version_bump` — extended to prove the design's actual selling point, not just its first half** (test-rigor review, major: the draft only checked that the first post-upgrade access is a clean live scan; it never checked that this happens *exactly once*, which is precisely what distinguishes a per-entry version field from PRD §6.2's rejected whole-envelope design):
  ```python
  def test_stream_rescans_once_after_version_bump(self):
      # seed an old-format (no-version) entry for self.root, then:
      events_1 = self._stream()
      self.assertFalse(events_1[0]['from_cache'])          # first access: live scan, not a crash
      events_2 = self._stream()
      self.assertTrue(events_2[0]['from_cache'])           # second access: now a cache HIT
      entry = disko.cache_get(self.root)
      self.assertEqual(entry['version'], disko.CACHE_VERSION)
  ```
- **`test_ancestor_touched_by_propagation_still_treated_as_miss` (new, minor per test-rigor review)** — seed a no-version ancestor entry, trigger `cache_set` on a descendant path so `_propagate_size` touches the ancestor's `children`/size in place, then assert `disko.cache_get(ancestor)` is still `None` — proving the deliberate non-stamping behavior documented above, end-to-end, not just in prose.
- Regression check (no new test, confirm still green): `TestCacheStaleness` (test_disko.py:549-604) rerun as-is to confirm the added `version` key doesn't break ancestor-propagation assertions.

**Manual verification / operational note (performance review, major — new in this revision):** the version bump interacts with `schedule_prefetch` (disko.py:453-465), which treats `cache_get(p)` falsy as "needs a background rescan." Because every pre-upgrade entry now reads as a miss, the first visit to any previously-warm directory after the version bump also triggers up to `PREFETCH_TOP_N=10` extra background `du` scans of its children — bounded to one level down by `PREFETCH_MAX_DEPTH=1` (`_do_prefetch`'s recursive `schedule_prefetch(children, depth)` call receives `depth-1=0` and returns immediately, confirmed by reading the code — this does **not** cascade further), and further bounded by `SCAN_WORKERS=12`/`PREFETCH_WORKERS=4`. This is real, bounded, one-time cost, compounded by `cache_save()`'s non-incremental full-`_cache` JSON rewrite (disko.py:66-91) happening on every one of those extra `cache_set` calls during the same burst — **judgement call:** rather than softening the version gate to reduce this cost (which would reopen the exact "serve stale shape silently" failure mode the design exists to prevent), accept it as a bounded, one-time migration cost and document it in the rollout note: *expect a burst of extra background scans and disk I/O for a few minutes after this deploy on a large, previously-cached tree; schedule the deploy outside peak hours if the disk being scanned is large.* Recommended (not blocking) additional test: seed a cache with several pre-upgrade entries under one root, run a scan, and assert the number of background scans submitted stays within the `PREFETCH_TOP_N` bound per visited directory.

**Risk:** Medium (raised from the draft's "Low-medium" to reflect the deploy-ordering hazard and prefetch/write-amplification cost, both now documented and bounded rather than left as gaps).

---

### Step 3 — Frontend: shared color-resolution helper, `TYPE_COLORS`, `capChildren`/sidebar bucket exemption

**Where:** `PALETTE`/`MAX_LEAVES`/`OTHER_COLOR` constants (disko.py:901-909), `capChildren` (disko.py:961-972), `renderTreemap`'s `colorOf` (disko.py:1379-1382), `renderSidebar`'s per-row color and `MAX_SIDEBAR` slice (disko.py:1447-1451).

#### 3a. Color-blind/contrast check — **blocking gate, before this step is mergeable** (frontend/UX review, major)

The draft shipped `TYPE_COLORS` with hexes explicitly labeled "placeholder... pending the color-blind/contrast check" but deferred the check itself to the very last item of the *final manual QA checklist*, run only after Steps 3-4 (colors, capChildren, legend, filtering) were already built on top of those placeholder values — contradicting PRD §6.3's "must be run through a color-blindness simulator... before it's finalized." **Fix:** run the color-blind/contrast pass (protanopia/deuteranopia/tritanopia simulation, e.g. browser devtools vision-deficiency emulation or Coblis) on the candidate `TYPE_COLORS` hexes as the *first* sub-step of this commit, before writing the JS below. Finalize the hex values (or explicitly document residual same-hue ambiguity per §6.3's caveat, and confirm `TYPE_COLORS.other` stays distinct from `OTHER_COLOR` under each simulated deficiency) and record the outcome in the PR description. Steps 4-5 (legend, filtering) build on these same finalized values.

#### 3b. Shared color helper + constants

```js
const TYPE_COLORS = {
  image: '#38bdf8', video: '#f472b6', audio: '#fbbf24', archive: '#a78bfa',
  document: '#34d399', code: '#fb923c', other: '#94a3b8',
};  // finalized per 3a's color-blind pass before merge
const TYPE_LABELS = {
  image: 'Images', video: 'Video', audio: 'Audio', archive: 'Archives',
  document: 'Documents', code: 'Code', other: 'Other',
};
function colorForNode(data, siblingIdx) {
  if (data.fileType) return TYPE_COLORS[data.fileType] || TYPE_COLORS.other;
  return PALETTE[siblingIdx % PALETTE.length];
}
```
(`TYPE_COLORS.other` = `#94a3b8`, visibly distinct in hue/lightness from `OTHER_COLOR` = `#475569`.)

**`colorOf` after (disko.py:1378-1382):**
```js
(root.children||[]).forEach((n,i) => { n.colorIdx = i; });
const colorOf = d => {
  let n=d; while(n.depth>1) n=n.parent;
  return n.data.aggregate ? OTHER_COLOR : colorForNode(n.data, n.colorIdx);
};
```

**`renderSidebar` color, after (disko.py:1451):** `const color = colorForNode(item, i);`

#### 3c. Bucket exemption — **now shared between the treemap and the sidebar**

The draft applied the `>500`-children bucket exemption only to `capChildren` (used solely by `renderTreemap`, one call site confirmed at disko.py:1368); `renderSidebar`'s independent `MAX_SIDEBAR=500` truncation (`all.slice(0, MAX_SIDEBAR)`, confirmed at disko.py:1447) had no equivalent logic — this was flagged, independently and identically, by both the feasibility review (major) and the frontend/UX review (critical). In a folder with >500 children where several ordinary subfolders outrank a bucket by size, the bucket would still get its own treemap cell but could be silently absent from the sidebar and its "N smaller items" note — a direct violation of the cross-view color-consistency guarantee (G1) this whole feature exists to provide. **Fix — factor the partition logic out and reuse it in both places:**

```js
function selectVisible(children, maxSlots) {
  // Shared bucket-exemption partition: type-bucket nodes are always kept whole;
  // only ordinary subfolders/mounts are subject to the size-ranked cap. Used by
  // both the treemap's capChildren (which folds the rest into an aggregate row)
  // and the sidebar's MAX_SIDEBAR truncation (which just stops rendering more).
  const buckets = children.filter(c => c.fileType);
  const others  = children.filter(c => !c.fileType);
  const budget = Math.max(0, maxSlots - buckets.length);
  const sorted = others.slice().sort((a,b) => (b.size||0)-(a.size||0));
  return { buckets, kept: sorted.slice(0, budget), rest: sorted.slice(budget) };
}

function capChildren(children, max, parentPath) {
  const { buckets, kept, rest } = selectVisible(children, max - 1); // -1 reserves the aggregate row
  if (buckets.length + kept.length + rest.length <= max) return children;
  const size = rest.reduce((s,c) => s+(c.size||0), 0);
  const agg = { name: `${rest.length} smaller items`, path: parentPath||'',
                size, isDir: false, aggregate: true };
  if (rest.some(c => c.status)) agg.status = 'partial';
  return rest.length ? buckets.concat(kept, [agg]) : buckets.concat(kept);
}
```

**`renderSidebar`'s `MAX_SIDEBAR` slice, after:**
```js
const all = (data.children||[]).filter(c=>hasSizeInfo(c)||c.mount);
if (!all.length) return;
const items = all.length > MAX_SIDEBAR
  ? (() => { const { buckets, kept } = selectVisible(all, MAX_SIDEBAR);
              return buckets.concat(kept).sort((a,b) => (b.size||0)-(a.size||0)); })()
  : all;
const maxSz = items[0]?.size || 1;
```
(Re-sorting the combined `buckets.concat(kept)` by size, rather than leaving buckets first, keeps the sidebar's largest-first visual ordering and its `maxSz` bar-width scaling correct regardless of where a bucket ranks.)

**Accepted, documented limitation (frontend/UX review, nit — resolved by leaving as-is rather than over-engineering):** the treemap's sibling-index color fallback (`colorForNode`'s non-bucket branch) derives `colorIdx` from the pre-cap `root.children` order, while the sidebar's fallback index `i` is derived from its own (possibly re-sorted, post-cap) `items` array; past the 500-child fold threshold these can theoretically diverge for the same *non-bucket* subfolder appearing in both views. This does not affect `TYPE_COLORS` bucket coloring (the actual G1 guarantee), only the pre-existing sibling-index scheme for ordinary subfolders in the rare >500-children case, so it's left as accepted pre-existing behavior rather than reworked here.

**Tests:** No JS test tooling exists (confirmed: no `package.json`, no jsdom/Jest/Playwright). Nothing automated to add; the backend suite is unaffected.

**Manual verification (extends PRD §7 step 2, now covering both views):**
- Open a real folder with ≥3 loose file types; confirm treemap cell colors and sidebar dots for the same bucket are pixel-identical (devtools computed-style, not eyeballing).
- Confirm subfolder/mount cells are unaffected — still sibling-index colored.
- Construct a folder with >500 total children including several bucket nodes; confirm every bucket cell **and every corresponding sidebar row** still render, never folded into the aggregate/omitted from the list, while ordinary subfolders past the cap still fold/truncate as before in both views.
- Resize through the 760px breakpoint with a bucketed folder open; confirm no color/DOM regression.

**Risk:** Medium. Main residual risk (unchanged from draft): `selectVisible`'s filter predicate misclassifying a node, or the `rest.length ? ... : ...` guard failing to prevent a cosmetic "0 smaller items" row.

---

### Step 4 — Frontend: legend panel (view-scoped, per-type totals)

**Where:** New markup inside `#sidebar`, between `#sidebar-header` and `#sidebar-list` (disko.py:882-893); new `renderLegend(data)` function near `renderSidebar` (disko.py:1440+); new call sites in `render()` and `renderData()`.

**New HTML** (after `#sidebar-header`, before `#sidebar-list`):
```html
<div id="legend" role="group" aria-label="File types in this folder"></div>
<div id="legend-status" class="sr-only" aria-live="polite"></div>
```

**New JS function:**
```js
function renderLegend(data) {
  const box = document.getElementById('legend');
  box.textContent = '';
  const totals = {};
  (data.children || []).forEach(c => { if (c.fileType) totals[c.fileType] = (totals[c.fileType]||0) + (c.size||0); });
  const present = Object.keys(totals).filter(k => totals[k] > 0);
  if (!present.length) { box.style.display = 'none'; return; }
  box.style.display = '';
  const caption = el('div', 'legend-caption', 'Sizes shown are for loose files in this folder only');
  box.appendChild(caption);
  present.sort((a,b) => totals[b]-totals[a]).forEach(bucket => {
    const item = el('div', 'legend-item');
    item.setAttribute('role', 'button');
    item.setAttribute('tabindex', '0');
    item.setAttribute('aria-pressed', String(activeFilterType === bucket));
    const swatch = el('span', 'legend-swatch'); swatch.style.background = TYPE_COLORS[bucket] || TYPE_COLORS.other;
    item.append(swatch, el('span', 'legend-label', TYPE_LABELS[bucket] || bucket),
                el('span', 'legend-size', fmt(totals[bucket])));
    box.appendChild(item);
  });
}
```

**Call sites (frontend/UX review — "missing," now given as concrete diffs instead of prose):**
```js
function render(data) {
  renderTreemap(data);
  renderSidebar(data);
  renderLegend(data);      // new
}
function renderData(data) {
  currentData = data;
  renderTreemap(data);
  renderSidebar(data);
  renderLegend(data);      // new
}
```

Wire click/keyboard handling in Step 5 (kept separate so this step is reviewable purely as "legend renders correctly, no interaction yet").

**CSS:** `.legend`, `.legend-caption` (small, muted, matching `.tt-hint`), `.legend-item` (flex row, swatch + label + size, hover/focus-visible matching `.hdr-btn`/`.crumb`), `.legend-swatch` (small rounded square/circle, matching `.sitem-dot`'s sizing).

**Tests:** None automated (same JS-tooling gap).

**Manual verification:**
- One file type present → legend shows exactly one entry (no empty "(other: 0 B)" phantom rows).
- Zero loose files → legend hidden entirely, not an empty box.
- Caption legible in both wide and ≤760px stacked layouts.
- Legend totals match the sum of that bucket's sidebar rows' sizes.

**Risk:** Low — additive DOM, no interaction yet.

---

### Step 5 — Frontend: click-to-filter/highlight, lifecycle, accessibility, tooltip type label

**Where:** New global `activeFilterType` state (near `navGen`/`lastPath`); extend `renderLegend`'s items with click/keydown + `aria-pressed` update; `renderTreemap` and `renderSidebar` (per-cell/row dim class); `goTo`/`goToPath`/`goBack` (clear filter on navigation); the global `keydown` listener (add Escape); `showTooltip` (add type label).

**State + toggle:**
```js
let activeFilterType = null;  // single bucket key, or null (NG5: no multi-select in v1)

function toggleFilter(bucket) {
  activeFilterType = (activeFilterType === bucket) ? null : bucket;
  announceFilter();
  if (currentData) { renderTreemap(currentData); renderSidebar(currentData); renderLegend(currentData); }
}
function announceFilter() {
  document.getElementById('legend-status').textContent = activeFilterType
    ? `Showing ${TYPE_LABELS[activeFilterType] || activeFilterType} files only`
    : 'Filter cleared';
}
```

**Legend item wiring — given as concrete code, including the keyboard handler** (frontend/UX review, major: the draft only described this in prose, and the app's own convention for non-native buttons, e.g. the treemap cell handler at disko.py:1430-1435, calls `preventDefault()` specifically because Space would otherwise also scroll the page — an easy regression to introduce by leaving this unspecified in the plan):
```js
item.addEventListener('click', () => toggleFilter(bucket));
item.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter' || ev.key === ' ') {
    ev.preventDefault();  // matches the existing cell keydown convention; without this,
                           // Space would also scroll the sidebar/page.
    toggleFilter(bucket);
  }
});
```

**Lifecycle — clear on navigation, persist on refresh:** add `activeFilterType = null;` at the top of `goTo(path, force)`, inside `goToPath()`'s `onCommit`, and at the top of `goBack()` — all three are "a different folder is now in view." **Do not** add it to `refreshCurrent()` or the `onRefresh`/revalidation path — those redraw the *same* folder, so the filter must survive.

**A fifth call path, named explicitly** (frontend/UX review, minor — the draft's own risk note worried about a missed fifth path and there is one): `refreshPath()` (the sidebar row's ↺ single-folder refresh) mutates `currentData.children` in place and calls `renderData(currentData)` directly, bypassing `goTo`/`goToPath`/`goBack` entirely. It happens to behave correctly today (it never touches `activeFilterType`, so the filter correctly survives it, since it redraws the same folder) — but it must be named here as an audited "same folder, must persist" path, not left undiscovered, and a QA line added below.

**Dim/highlight in `renderTreemap`:**
```js
.attr('class', d => {
  let cls = 'cell' + (d.data.isDir===false?' file':'') + (isUnknown(d.data)?' unknown':(d.data.status?' partial':''));
  if (activeFilterType) {
    if (d.data.fileType && d.data.fileType !== activeFilterType) cls += ' dim-other';
    else if (!d.data.fileType) cls += ' dim-neutral';
  }
  return cls;
})
```
**Dim/highlight in `renderSidebar`** — same three-way branch added to the row's class string.

**CSS additions — with the hover-specificity conflict resolved deterministically, not "eyeballed in QA"** (frontend/UX review, minor: the draft's own risk note flagged that `.cell.dim-other rect`/`.cell.dim-neutral rect` and the existing `.cell:hover rect { opacity: .8 }` rule have identical specificity, so which wins on hover was left to source-order accident):
```css
.cell.dim-other rect  { opacity: .25; }
.cell.dim-neutral rect{ opacity: .55; }
.cell.dim-other:hover rect  { opacity: .25; }  /* dimmed cells stay dimmed on hover, deterministically */
.cell.dim-neutral:hover rect{ opacity: .55; }
.sitem.dim-other  .sitem-dot, .sitem.dim-other  .sitem-name { opacity: .25; }
.sitem.dim-neutral .sitem-dot, .sitem.dim-neutral .sitem-name { opacity: .55; }
```

**Keyboard (Escape) — extend the existing global listener:**
```js
if (e.key==='Backspace'||e.key==='ArrowLeft') { e.preventDefault(); goBack(); }
if (e.key==='Escape' && activeFilterType) { e.preventDefault(); toggleFilter(activeFilterType); }
```

**Tooltip type label** — in `showTooltip`, after the existing `tt-pct` line:
```js
if (d.data.fileType) tooltip.appendChild(el('div', 'tt-type', TYPE_LABELS[d.data.fileType] || d.data.fileType));
```
CSS: `.tt-type { font-size: 11px; color: #94a3b8; margin-top: 2px; }`.

**Tests:** None automated (JS-tooling gap).

**Manual verification (extends PRD §6.3/§7, folded into the final QA checklist too):**
- Click a legend entry → matching-type cells/rows stay bright; other typed cells dim strongly; subfolders/mounts dim to a visibly-intermediate level (three distinct visual states).
- Click again → clears; `aria-live` announces "Filter cleared".
- Click a different entry while one is active → replaces, never combines (NG5).
- Refresh the current folder (refresh button or stale-cache revalidation) → filter survives.
- **Use the per-row ↺ refresh (`refreshPath`) while a filter is active → filter survives (new QA line, covering the fifth path named above).**
- Navigate into a subfolder → filter clears silently.
- `goBack()` (Back button or Backspace) → filter clears.
- Full keyboard pass: Tab reaches every legend entry; Enter/Space toggles it and flips `aria-pressed` (confirm Space does not also scroll the page); Escape clears from anywhere on the page; `aria-live` announces both directions — verify with a screen reader or the browser's accessibility tree inspector.
- Confirm existing cell click/keydown-to-open handlers still work on a dimmed subfolder cell (dimming is visual only).

**Risk:** Medium-high — this remains the step most likely to have a subtle interaction bug, though the fifth path and the keyboard handler are now both specified rather than left to be discovered during implementation.

---

### Step 6 — Docs: README "Features" + screenshot

**Where:** `README.md` — the `## Features` bullet list (README.md:15-24) and the screenshot reference (README.md:11, `docs/screenshot.png`).

**Before/after:** Add one new bullet after "Zoomable D3.js treemap":
```
- **File-type color coding** — loose files in the current folder are colored and grouped by type (video, images, documents, ...); a legend shows per-type totals and lets you click a type to highlight matching cells and dim the rest
```
Regenerate `docs/screenshot.png` from a real run of the updated app so it shows the legend and typed colors (not the old sibling-colored treemap).

**Tests:** None (docs-only change).

**Manual verification:** Render `README.md` locally; confirm the bullet reads correctly and the screenshot reflects the shipped UI.

**Risk:** Very low.

---

## Commit/PR granularity

**One PR** for the whole v1 scope, containing:

1. **Commit 1** (Step 1, `classify_file`): independently committable and deployable on its own — purely additive, no call sites, cannot regress anything.
2. **Commits 2a/2b/2c (Step 2 — dict-of-buckets, counter fix, cache versioning): reviewed as three stacked diffs, but squash-merged and deployed as a single atomic unit — this is now a hard requirement, not a recommendation.** Two independent, confirmed failure modes prove this: (a) 2a alone raises `NameError` on every live scan (a hard code-level dependency, not just a data-correctness one), and (b) 2c's version bump landing or running even briefly before 2a/2b's shape change is live permanently mislabels stale-shaped cache entries as current, in a way a further version bump cannot undo. Cherry-picking, reverting, or independently deploying any one of 2a/2b/2c is unsafe.
3. **Commits 3-5 (Steps 3-5 — frontend colors/exemption, legend, filtering):** safe to ship as a second deploy unit after 1-2c, since each is additive and reuses the previous step's manual QA. **Exception:** Step 3's color-blind/contrast pass (3a) is a blocking gate *within* commit 3, not a trailing checklist item — Steps 4-5 must not be built or reviewed against un-finalized placeholder hex values.
4. **Commit 6** (docs): any time after.

If the team prefers strict one-commit-per-PR: the same grouping applies to *merge order*, with 2a/2b/2c specifically required to merge as one unit (squash), never as three separate merges to trunk.

## Rollback plan

- **Backend (Commits 1-2c):** Because 2a/2b/2c are guaranteed to be one atomic deploy unit (see above), the reverse-deploy-ordering hazard (version bump live without the shape change, or vice versa) cannot occur in practice — reverting means reverting all three together. Rolling back this unit after it's run live: any cache entries written by the new code carry `version: 2`; the pre-upgrade `cache_get` (no version check at all) will happily read them, so a rolled-back server would render each bucket as a same-index-colored, oddly-named leaf (`(images)`, `(video)`, etc.) — ugly, not broken (no crash, no data loss). Recommended: delete or rename `~/.disko_cache.json` (or `$DISKO_CACHE`) once when rolling back live, to force a clean re-scan under the old code instead of serving mixed-shape entries. Separately, note that `_propagate_size` deliberately never stamps `version` on ancestor entries it only touches via propagation (see Step 2c) — this means an ancestor directory gets exactly one extra full re-scan the first time it's *directly* viewed post-upgrade, even if its size was kept numerically fresh by propagation in the meantime. This is intentional conservatism (the alternative — stamping version early — would silently mislabel a still-old-shaped entry as current) and applies whether or not a rollback happens.
- **Frontend (Commits 3-5):** stateless — reverting any of these is a plain revert with no data-migration concern. Reverting the filtering commit (5) alone while keeping colors + legend display (3-4) is a safe partial rollback if only the click-to-filter interaction turns out buggy.
- **Docs (Commit 6):** trivial revert.

## Final end-to-end manual QA checklist (full feature, matching PRD §6.3/§7)

1. **Cross-view color consistency (G1):** for each bucket in a ≥3-type folder, compare the treemap cell's rendered fill against the sidebar dot's rendered color (devtools computed style) — must be pixel-identical.
2. **Bucket exemption, both views:** open (or synthesize) a folder with >500 total children including several type buckets; confirm every bucket cell renders in the treemap **and every corresponding row still appears in the sidebar** (not silently dropped by `MAX_SIDEBAR`), while ordinary subfolder/mount overflow still folds/truncates correctly in both.
3. **Progress-counter regression:** force a slow scan on a folder with multiple loose file types; confirm the progress bar/`n / total` text never exceeds the total, and the final count exactly matches the number of streamed `child` events.
4. **Filter lifecycle across navigation/refresh:**
   - a. Click a legend entry → highlight/dim in both treemap and sidebar.
   - b. Refresh the current folder (refresh button, or stale-cache revalidation) → filter survives.
   - c. Use a row's per-item ↺ refresh (`refreshPath`) while a filter is active → filter survives.
   - d. Navigate into a subfolder → filter clears.
   - e. Go back (Back button / Backspace) → filter clears.
   - f. Click a second legend entry while one is active → replaces, never combines (NG5).
   - g. Click the active entry again → clears.
5. **Keyboard accessibility:** Tab reaches every legend entry in order; Enter/Space toggles a filter, flips `aria-pressed`, and does **not** also scroll the page; Escape clears the active filter from anywhere on the page; the `aria-live="polite"` region announces both "Showing … files only" and "Filter cleared" — verify with a screen reader or the browser's accessibility inspector.
6. **Color-blind palette:** confirmed as a blocking gate in Step 3a before this feature was built on top of the palette; re-run the simulator pass here as a final sanity check under protanopia/deuteranopia/tritanopia, confirming `TYPE_COLORS.other` stays distinct from `OTHER_COLOR` under each.
7. **Edge cases from §6.4:** one file type → exactly one legend/bucket entry (no phantom empty buckets); a dotfile with no real extension visibly contributes to `other` (non-zero, counted); a file that fails `stat()` stays invisible (unchanged from today).
8. **Responsive layout:** resize through the 760px breakpoint with the legend visible and a filter active; confirm legend, caption, and dim/highlight states remain legible and functional in the stacked layout.
9. **Tooltip:** hover/focus a bucketed cell; confirm the tooltip shows the resolved type label alongside name/path/size/percentage.
10. **README/screenshot:** confirm the shipped screenshot and Features bullet match what steps 1-9 actually show.
11. **Post-deploy operational check (new, per Step 2c's performance note):** after the backend deploy unit (1-2c) goes live on a large, previously-cached tree, confirm the transient burst of extra background scans/disk I/O described in Step 2c's risk section is within expectation (bounded by `PREFETCH_TOP_N`/`SCAN_WORKERS`) and subsides — not a sustained, escalating load.
