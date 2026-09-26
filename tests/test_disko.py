"""Stdlib unittest suite for disko.

Run from the repo root with:  python -m unittest discover -s tests -v
"""

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

# Point the cache at a throwaway file *before* importing disko, so the real
# ~/.disko_cache.json is never read or written by the test run.
_MODULE_TMP = tempfile.mkdtemp(prefix='disko-test-')
os.environ['DISKO_CACHE'] = os.path.join(_MODULE_TMP, 'cache.json')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import disko  # noqa: E402


def tearDownModule():
    _wait_for_prefetch()
    shutil.rmtree(_MODULE_TMP, ignore_errors=True)


def _wait_for_prefetch(timeout=5.0):
    """Wait for disko's background prefetch work to drain."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with disko._prefetch_lock:
            if not disko._prefetching:
                return
        time.sleep(0.05)


def _join_tolerating_prestart_race(t, join_timeout=5, poll_interval=0.01, start_deadline=2.0):
    """t.join(join_timeout), tolerating the narrow window where a just-created thread
    appears in threading.enumerate() (via CPython's internal _limbo bookkeeping) before
    its own start() call -- running concurrently on another thread -- has finished
    marking it started (join() raises RuntimeError in that window even though start()
    was already invoked). A single fixed-delay retry can still lose this race under a
    sufficiently slow/loaded scheduler, so poll every `poll_interval` until the thread
    becomes joinable or `start_deadline` elapses, then propagate any further failure."""
    deadline = time.monotonic() + start_deadline
    while True:
        try:
            t.join(join_timeout)
            return
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(poll_interval)


def _write_file(path, size):
    with open(path, 'wb') as f:
        # Random bytes so filesystems with transparent compression still
        # allocate real blocks.
        f.write(os.urandom(size))


def _make_tree(root):
    """Create a small tree; returns expected sizes for sanity checks.

    root/
      big/      a.bin (64 KiB), nested/b.bin (32 KiB)
      small/    c.bin (4 KiB)
      empty/
      loose1.txt (1000 B), loose2.txt (2000 B)
    """
    os.makedirs(os.path.join(root, 'big', 'nested'))
    os.makedirs(os.path.join(root, 'small'))
    os.makedirs(os.path.join(root, 'empty'))
    _write_file(os.path.join(root, 'big', 'a.bin'), 64 * 1024)
    _write_file(os.path.join(root, 'big', 'nested', 'b.bin'), 32 * 1024)
    _write_file(os.path.join(root, 'small', 'c.bin'), 4 * 1024)
    _write_file(os.path.join(root, 'loose1.txt'), 1000)
    _write_file(os.path.join(root, 'loose2.txt'), 2000)


# Exact bucket membership (standalone fixture, independent of _make_tree so bucket-count
# expectations are unambiguous): image, video, archive, document, code, other -- 6 non-empty
# buckets, no audio bucket present.
TYPE_TREE_DIRS = {'sub'}
TYPE_TREE_BUCKET_FILES = {
    'image': ['photo.jpg', 'IMG.JPG'],
    'video': ['clip.mp4'],
    'archive': ['notes.zip', 'backup.tar.gz'],
    'document': ['report.pdf'],
    'code': ['script.py'],
    'other': ['README', '.bashrc'],
}


def _make_type_tree(root):
    """Create a tree with loose files spanning multiple file-type buckets; returns
    {bucket: [filenames]} for the fixture's own bookkeeping (sizes vary per file, see
    _write_file below -- callers that need exact expected sizes read them from disk)."""
    os.makedirs(os.path.join(root, 'sub'))
    sizes = {
        'photo.jpg': 500, 'IMG.JPG': 700, 'clip.mp4': 900,
        'notes.zip': 300, 'backup.tar.gz': 400, 'report.pdf': 600,
        'script.py': 200, 'README': 100, '.bashrc': 150,
    }
    for name, size in sizes.items():
        _write_file(os.path.join(root, name), size)
    return TYPE_TREE_BUCKET_FILES


class TempTreeMixin(object):
    #: overridable hook so a subclass can build a different fixture tree
    _populate_tree = staticmethod(_make_tree)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='disko-tree-')
        # realpath: on macOS /var is a symlink to /private/var.
        self.root = os.path.realpath(self.tmp)
        self._populate_tree(self.root)
        # Cache lives outside the scanned tree so it is not counted as a file.
        self.cache_dir = tempfile.mkdtemp(prefix='disko-cache-')
        self._orig_cache_file = disko.CACHE_FILE
        disko.CACHE_FILE = os.path.join(self.cache_dir, 'cache.json')
        # Persist even if the suite happens to run as root (disko skips saving as euid 0).
        self._orig_persist = disko._persist_cache
        disko._persist_cache = True
        self._threads_before = set(threading.enumerate())
        with disko._cache_lock:
            disko._cache.clear()

    def tearDown(self):
        try:
            # Join background refresh threads (e.g. after a cache hit) so they
            # cannot write into the next test's state, then drain prefetches.
            # See _join_tolerating_prestart_race for why a plain t.join(5) isn't safe.
            for t in set(threading.enumerate()) - self._threads_before:
                if t.name.startswith('prefetch'):
                    continue
                _join_tolerating_prestart_race(t)
            _wait_for_prefetch()
            with disko._cache_lock:
                disko._cache.clear()
        finally:
            # Always restore, even if the joins/prefetch-drain above raised --
            # otherwise a leaked CACHE_FILE/_persist_cache override corrupts
            # every test that runs afterward (they read disko's globals, not
            # per-test instance state), turning one flaky test into a cascade.
            disko.CACHE_FILE = self._orig_cache_file
            disko._persist_cache = self._orig_persist
            shutil.rmtree(self.cache_dir, ignore_errors=True)
            shutil.rmtree(self.tmp, ignore_errors=True)


class TempTypeTreeMixin(TempTreeMixin):
    """Like TempTreeMixin, but self.root holds _make_type_tree's multi-bucket fixture
    instead of _make_tree's (a single 'sub' dir plus loose files spanning 6 buckets)."""
    _populate_tree = staticmethod(_make_type_tree)


class _FlakyJoinThread:
    """Fake stand-in for a real Thread: .join() raises RuntimeError('cannot join thread
    before it is started') for the first `fail_times` calls, then succeeds -- lets the
    retry/deadline logic in _join_tolerating_prestart_race be tested deterministically,
    without depending on real OS thread scheduling to reproduce the actual race."""
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def join(self, timeout=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError('cannot join thread before it is started')


class TestJoinTolerance(unittest.TestCase):
    def test_retries_past_a_single_fixed_delay_attempt(self):
        # A single fixed-delay retry (the earlier, weaker fix) would give up after one
        # extra attempt; this thread only becomes joinable on the 6th call, longer than
        # that would tolerate.
        fake = _FlakyJoinThread(fail_times=5)
        _join_tolerating_prestart_race(fake, poll_interval=0.001, start_deadline=1.0)
        self.assertEqual(fake.calls, 6)

    def test_gives_up_after_the_deadline_elapses(self):
        fake = _FlakyJoinThread(fail_times=10 ** 6)  # never becomes joinable
        with self.assertRaises(RuntimeError):
            _join_tolerating_prestart_race(fake, poll_interval=0.001, start_deadline=0.05)


class TestCacheEnvOverride(unittest.TestCase):
    def test_disko_cache_env_is_honored(self):
        self.assertEqual(disko.CACHE_FILE, os.environ['DISKO_CACHE'])
        self.assertNotEqual(disko.CACHE_FILE, os.path.expanduser('~/.disko_cache.json'))


class TestDuSingle(TempTreeMixin, unittest.TestCase):
    def test_sizes_are_sane(self):
        big, big_status = disko.du_single(os.path.join(self.root, 'big'))
        small, small_status = disko.du_single(os.path.join(self.root, 'small'))
        empty, empty_status = disko.du_single(os.path.join(self.root, 'empty'))
        self.assertEqual((big_status, small_status, empty_status), (None, None, None))
        self.assertIsInstance(big, int)
        # du reports allocated blocks, so allow slack but require the data.
        self.assertGreaterEqual(big, 96 * 1024)
        self.assertLess(big, 10 * 1024 * 1024)
        self.assertGreaterEqual(small, 4 * 1024)
        self.assertGreater(big, small)
        self.assertGreaterEqual(empty, 0)
        self.assertLess(empty, big)
        # Results are whole KiB (du -sk * 1024).
        self.assertEqual(big % 1024, 0)

    def test_missing_path_reports_unknown_with_error(self):
        # A du failure must surface as an unknown size, never as 0.
        size, status = disko.du_single(os.path.join(self.root, 'does-not-exist'))
        self.assertIsNone(size)
        self.assertIsInstance(status, str)
        self.assertTrue(status)

    def test_dash_named_dir_is_not_an_option(self):
        os.mkdir(os.path.join(self.root, '-rf'))
        size, status = disko.du_single(os.path.join(self.root, '-rf'))
        self.assertIsNone(status)
        self.assertIsInstance(size, int)


class TestScanToList(TempTreeMixin, unittest.TestCase):
    def test_scan_tree(self):
        children = disko.scan_to_list(self.root)
        by_name = {c['name']: c for c in children}
        # loose1.txt/loose2.txt both classify as 'document', so they land in one bucket.
        self.assertEqual(set(by_name), {'big', 'small', 'empty', '(documents)'})

        for name in ('big', 'small', 'empty'):
            c = by_name[name]
            self.assertTrue(c['isDir'])
            self.assertEqual(c['path'], os.path.join(self.root, name))

        loose = by_name['(documents)']
        self.assertFalse(loose['isDir'])
        self.assertEqual(loose['path'], self.root)
        self.assertEqual(loose['fileType'], 'document')
        # Loose files are measured by allocated blocks, like du does for dirs.
        expected = sum(os.lstat(os.path.join(self.root, n)).st_blocks * 512
                       for n in ('loose1.txt', 'loose2.txt'))
        self.assertEqual(loose['size'], expected)
        self.assertGreaterEqual(loose['size'], 3000)

        # Sorted largest first, and 'big' is the largest entry.
        sizes = [c['size'] for c in children]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(children[0]['name'], 'big')

    def test_sparse_file_counts_allocated_blocks(self):
        sub = os.path.join(self.root, 'small')
        with open(os.path.join(sub, 'sparse.img'), 'wb') as f:
            f.truncate(1024 * 1024 * 1024)  # 1 GiB apparent, ~0 allocated
        children = disko.scan_to_list(sub)
        # Filter by fileType (any bucket), not by a literal name: 'sparse.img' has no
        # recognized extension and lands in 'other', not '(loose files)'/'(documents)'.
        loose = [c for c in children if c.get('fileType') is not None]
        self.assertTrue(loose)
        total = sum(c['size'] for c in loose)
        self.assertLess(total, 64 * 1024 * 1024)

    def test_mount_points_are_not_dued(self):
        real_split = disko._split_mounts

        def fake_split(path, dirs):
            local, mounts = real_split(path, dirs)
            return ([e for e in local if e.name != 'big'],
                    mounts + [e for e in local if e.name == 'big'])

        calls = []
        real_du = disko.du_single
        disko._split_mounts = fake_split
        disko.du_single = lambda p, *a: (calls.append(p), real_du(p, *a))[1]
        try:
            children = disko.scan_to_list(self.root)
        finally:
            disko._split_mounts = real_split
            disko.du_single = real_du
        big = [c for c in children if c['name'] == 'big'][0]
        self.assertEqual(big, {'name': 'big', 'path': os.path.join(self.root, 'big'),
                               'size': 0, 'isDir': True, 'mount': True})
        self.assertNotIn(os.path.join(self.root, 'big'), calls)
        self.assertTrue(disko._cacheable(children))

    def test_resolve_scan_path_macos_root(self):
        real = disko.platform.system
        try:
            disko.platform.system = lambda: 'Darwin'
            self.assertEqual(disko._resolve_scan_path('/'), '/System/Volumes/Data')
            self.assertEqual(disko._resolve_scan_path('//'), '/System/Volumes/Data')
            self.assertEqual(disko._resolve_scan_path('/usr/'), '/usr')
            disko.platform.system = lambda: 'Linux'
            self.assertEqual(disko._resolve_scan_path('/'), '/')
        finally:
            disko.platform.system = real

    def test_scan_normalizes_path(self):
        children = disko.scan_to_list(self.root + os.sep + '.' + os.sep)
        self.assertEqual({c['name'] for c in children}, {'big', 'small', 'empty', '(documents)'})

    def test_scan_unreadable_returns_none(self):
        # None (not []) so callers skip caching an unreadable/missing directory.
        self.assertIsNone(disko.scan_to_list(os.path.join(self.root, 'loose1.txt')))
        self.assertIsNone(disko.scan_to_list(os.path.join(self.root, 'nope')))

    def test_cacheable_rejects_unknown_sizes(self):
        self.assertFalse(disko._cacheable(None))
        self.assertTrue(disko._cacheable([]))
        self.assertTrue(disko._cacheable([{'size': 0, 'status': 'partial'}]))
        self.assertFalse(disko._cacheable([{'size': 1}, {'size': None, 'status': 'timeout'}]))

    def test_scan_empty_dir(self):
        self.assertEqual(disko.scan_to_list(os.path.join(self.root, 'empty')), [])


class TestClassifyFile(unittest.TestCase):
    def test_common_extensions_map_to_expected_bucket(self):
        cases = {
            'photo.jpg': 'image', 'movie.mp4': 'video', 'song.mp3': 'audio',
            'archive.zip': 'archive', 'report.pdf': 'document', 'script.py': 'code',
        }
        for name, expected in cases.items():
            self.assertEqual(disko.classify_file(name), expected, name)

    def test_case_insensitive(self):
        self.assertEqual(disko.classify_file('IMG.JPG'), 'image')
        self.assertEqual(disko.classify_file('Movie.MP4'), 'video')

    def test_unmatched_and_extensionless_fall_to_other(self):
        self.assertEqual(disko.classify_file('data.xyz123'), 'other')
        self.assertEqual(disko.classify_file('noextension'), 'other')
        self.assertEqual(disko.classify_file('.bashrc'), 'other')  # dotfile, no real extension

    def test_compound_archive_suffixes(self):
        # NOTE: every suffix here (gz/bz2/xz/zst/lz4) is ALSO independently mapped to
        # 'archive' in FILE_TYPE_EXTENSIONS, so this loop passes via the single-suffix
        # fallback alone for every case -- it is an end-user-correctness check, not proof
        # the compound branch itself executed. See test_compound_suffix_branch_is_actually_exercised.
        for suffix in disko.COMPOUND_ARCHIVE_SUFFIXES:
            self.assertEqual(disko.classify_file(f'x.{suffix}'), 'archive', suffix)

    def test_compound_suffix_branch_is_actually_exercised(self):
        # Remove 'gz's own standalone mapping so a pass can only come from the
        # COMPOUND_ARCHIVE_SUFFIXES branch -- this is the actual regression guard.
        with mock.patch.dict(disko._EXT_TO_TYPE):
            del disko._EXT_TO_TYPE['gz']
            self.assertEqual(disko.classify_file('x.tar.gz'), 'archive')

    def test_case_insensitive_compound_suffix(self):
        self.assertEqual(disko.classify_file('ARCHIVE.TAR.GZ'), 'archive')

    def test_no_extension_listed_in_multiple_buckets(self):
        seen = {}
        for bucket, exts in disko.FILE_TYPE_EXTENSIONS.items():
            for ext in exts:
                self.assertNotIn(ext, seen, f'{ext!r} listed in both {seen.get(ext)!r} and {bucket!r}')
                seen[ext] = bucket


class TestScanToListTypeBuckets(TempTypeTreeMixin, unittest.TestCase):
    def _alloc(self, name):
        return os.lstat(os.path.join(self.root, name)).st_blocks * 512

    def test_multiple_buckets_appear_with_correct_sizes(self):
        children = disko.scan_to_list(self.root)
        by_type = {c['fileType']: c for c in children if c.get('fileType')}
        self.assertEqual(set(by_type), set(TYPE_TREE_BUCKET_FILES))
        for bucket, names in TYPE_TREE_BUCKET_FILES.items():
            expected = sum(self._alloc(n) for n in names)
            self.assertEqual(by_type[bucket]['size'], expected, bucket)
            self.assertFalse(by_type[bucket]['isDir'])
            self.assertEqual(by_type[bucket]['path'], self.root)

    def test_single_type_folder_yields_one_bucket(self):
        # A folder with only .txt files (all 'document') yields exactly one bucket
        # child -- no phantom empty buckets for the types that aren't present.
        only_txt = os.path.join(self.root, 'only_txt')
        os.mkdir(only_txt)
        _write_file(os.path.join(only_txt, 'a.txt'), 100)
        _write_file(os.path.join(only_txt, 'b.txt'), 200)
        children = disko.scan_to_list(only_txt)
        buckets = [c for c in children if c.get('fileType')]
        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0]['fileType'], 'document')

    def test_mixed_case_and_compound_extension_classified_correctly(self):
        children = disko.scan_to_list(self.root)
        by_type = {c['fileType']: c for c in children if c.get('fileType')}
        # photo.jpg + IMG.JPG (mixed case) must BOTH land in 'image', not just the lowercase one.
        self.assertEqual(by_type['image']['size'], self._alloc('photo.jpg') + self._alloc('IMG.JPG'))
        # notes.zip + backup.tar.gz (compound suffix) must BOTH land in 'archive'.
        self.assertEqual(by_type['archive']['size'], self._alloc('notes.zip') + self._alloc('backup.tar.gz'))

    def test_dotfile_with_no_extension_lands_in_other(self):
        children = disko.scan_to_list(self.root)
        by_type = {c['fileType']: c for c in children if c.get('fileType')}
        # README (no extension) + .bashrc (dotfile, no real extension) both land in 'other',
        # visibly contributing a nonzero size -- not invisible.
        expected = self._alloc('README') + self._alloc('.bashrc')
        self.assertEqual(by_type['other']['size'], expected)
        self.assertGreater(by_type['other']['size'], 0)


class TestCacheRoundTrip(TempTreeMixin, unittest.TestCase):
    def test_set_save_load_get(self):
        children = disko.scan_to_list(self.root)
        disko.cache_set(self.root, children)
        self.assertTrue(os.path.isfile(disko.CACHE_FILE))
        with open(disko.CACHE_FILE) as f:
            on_disk = json.load(f)
        self.assertIn(self.root, on_disk)

        # Drop in-memory state and reload from disk.
        with disko._cache_lock:
            disko._cache.clear()
        self.assertIsNone(disko.cache_get(self.root))
        disko.cache_load()
        entry = disko.cache_get(self.root)
        self.assertIsNotNone(entry)
        self.assertEqual(entry['children'], children)
        self.assertIsInstance(entry['scanned_at'], float)
        self.assertEqual(entry.get('version'), disko.CACHE_VERSION)

    def test_old_format_entry_is_treated_as_miss(self):
        # No 'version' key at all (the pre-v2 on-disk shape): cache_get must treat it as
        # a miss (triggering exactly one clean re-scan), not crash or render stale data.
        with open(disko.CACHE_FILE, 'w') as f:
            json.dump({self.root: {'children': [], 'scanned_at': time.time()}}, f)
        disko.cache_load()
        self.assertIsNone(disko.cache_get(self.root))

    def test_dict_entry_missing_children_key_treated_as_miss(self):
        # A dict entry that carries the current version but is missing 'children'
        # (e.g. a partially hand-edited cache file) must not crash stream_directory's
        # len(cached['children'])/iteration -- cache_get must reject it as a miss too,
        # not just check isinstance(dict) + version.
        with disko._cache_lock:
            disko._cache[disko._cache_key(self.root)] = {
                'version': disko.CACHE_VERSION, 'scanned_at': time.time(),
            }
        self.assertIsNone(disko.cache_get(self.root))

    def test_wrong_typed_present_keys_treated_as_miss(self):
        # A dict entry that has the right keys AND the current version, but wrong-typed
        # values, must still be rejected -- not just a missing-key/non-dict-entry check.
        # A null scanned_at would otherwise crash cache_set's `existing.get('scanned_at',
        # 0) > scanned_at` comparison (None > float raises TypeError in Python 3).
        key = disko._cache_key(self.root)
        cases = [
            {'children': 'not-a-list', 'scanned_at': time.time(), 'version': disko.CACHE_VERSION},
            {'children': [1, 2, 3], 'scanned_at': time.time(), 'version': disko.CACHE_VERSION},
            {'children': [], 'scanned_at': None, 'version': disko.CACHE_VERSION},
            {'children': [], 'scanned_at': float('nan'), 'version': disko.CACHE_VERSION},
            {'children': [], 'scanned_at': 'not-a-number', 'version': disko.CACHE_VERSION},
            # A dict child missing fields _propagate_size assumes are always present
            # (name/path/size) -- structurally incomplete, not just non-dict.
            {'children': [{'isDir': True}], 'scanned_at': time.time(), 'version': disko.CACHE_VERSION},
        ]
        for entry in cases:
            with disko._cache_lock:
                disko._cache[key] = entry
            self.assertIsNone(disko.cache_get(self.root), entry)
            # cache_set must not crash reading this entry's raw staleness check either.
            self.assertTrue(disko.cache_set(self.root, []), entry)

    def test_propagation_into_structurally_incomplete_child_does_not_crash(self):
        # Reproduces the exact reviewer-reported crash: a current-version ancestor entry
        # whose child dict is missing 'path' must not crash _propagate_size's c['path']
        # subscript when a descendant is cached (which triggers propagation into it).
        big = os.path.join(self.root, 'big')
        with disko._cache_lock:
            disko._cache[disko._cache_key(self.root)] = {
                'children': [{'isDir': True}], 'scanned_at': 1.0, 'version': disko.CACHE_VERSION,
            }
        self.assertTrue(disko.cache_set(big, [{'name': 'a', 'path': os.path.join(big, 'a'),
                                               'size': 1, 'isDir': True}], scanned_at=200.0))
        self.assertIsNone(disko.cache_get(self.root))  # still malformed: a miss, not stale data

    def test_propagation_with_non_numeric_sibling_size_does_not_crash(self):
        # A sibling entry with a non-numeric 'size' (structurally complete otherwise)
        # must not crash _size_key's sort (-(size)) or the size-summing reduce when a
        # matching child elsewhere in the same children list is propagated into.
        big = os.path.join(self.root, 'big')
        with disko._cache_lock:
            disko._cache[disko._cache_key(self.root)] = {
                'children': [
                    {'name': 'big', 'path': big, 'size': 5, 'isDir': True},
                    {'name': 'sibling', 'path': os.path.join(self.root, 'sibling'),
                     'size': 'bad', 'isDir': True},
                ],
                'scanned_at': 1.0, 'version': disko.CACHE_VERSION,
            }
        self.assertTrue(disko.cache_set(big, [{'name': 'a', 'path': os.path.join(big, 'a'),
                                               'size': 1, 'isDir': True}], scanned_at=200.0))
        self.assertIsNone(disko.cache_get(self.root))  # still malformed: a miss, not stale data

    def test_propagation_into_child_with_null_path_does_not_crash(self):
        # A child with the right keys but a null 'path' (structurally complete, wrong
        # type) must not crash _propagate_size's _cache_key(c['path']) ->
        # os.path.expanduser call, which requires a str (TypeError on None).
        big = os.path.join(self.root, 'big')
        with disko._cache_lock:
            disko._cache[disko._cache_key(self.root)] = {
                'children': [{'name': 'big', 'path': None, 'size': 10, 'isDir': True}],
                'scanned_at': 1.0, 'version': disko.CACHE_VERSION,
            }
        self.assertTrue(disko.cache_set(big, [{'name': 'a', 'path': os.path.join(big, 'a'),
                                               'size': 1, 'isDir': True}], scanned_at=200.0))
        self.assertIsNone(disko.cache_get(self.root))  # still malformed: a miss, not stale data

    def test_non_finite_child_size_treated_as_miss(self):
        # A structurally/type-complete child whose 'size' is inf/NaN wouldn't crash
        # _size_key's sort or the size-summing reduce, but would silently poison the
        # ancestor's total (NaN/inf propagating upward) -- reject it outright instead.
        for bad_size in (float('inf'), float('-inf'), float('nan')):
            self.assertFalse(disko._sound_child(
                {'name': 'x', 'path': '/x', 'size': bad_size, 'isDir': True}), bad_size)

    def test_negative_child_size_rejected(self):
        # No allocated size the app itself ever produces is negative; a negative value
        # would otherwise reduce a propagated ancestor total and vanish from the UI via
        # hasSizeInfo's `size > 0` check, with no visible sign anything is wrong.
        self.assertFalse(disko._sound_child({'name': 'x', 'path': '/x', 'size': -1, 'isDir': True}))

    def test_non_string_file_type_rejected(self):
        # 'fileType' is always a plain str bucket key in the real app (see
        # FILE_TYPE_EXTENSIONS); a non-finite float here would otherwise only be caught,
        # if at all, by the SSE writer's allow_nan=False JSON guard -- reject it at the
        # cache-validation boundary instead, where every other field is already checked.
        for bad_file_type in (float('nan'), float('inf'), 123, True):
            self.assertFalse(disko._sound_child(
                {'name': 'x', 'path': '/x', 'size': 1, 'isDir': False, 'fileType': bad_file_type}),
                bad_file_type)
        # A real bucket key (or no fileType at all) must still be accepted.
        self.assertTrue(disko._sound_child(
            {'name': 'x', 'path': '/x', 'size': 1, 'isDir': False, 'fileType': 'image'}))
        self.assertTrue(disko._sound_child({'name': 'x', 'path': '/x', 'size': 1, 'isDir': True}))

    def test_bogus_file_type_string_rejected(self):
        # A string that isn't a real bucket key (not in FILE_TYPE_EXTENSIONS, not
        # 'other') must be rejected too, not just non-string values.
        self.assertFalse(disko._sound_child(
            {'name': 'x', 'path': '/x', 'size': 1, 'isDir': False, 'fileType': 'bogus'}))
        self.assertTrue(disko._sound_child(
            {'name': 'x', 'path': '/x', 'size': 1, 'isDir': False, 'fileType': 'other'}))

    def test_non_finite_status_rejected(self):
        # A cached child with status: NaN previously passed _sound_child unchanged,
        # so _current_entry kept accepting the entry on every open -- and the SSE
        # writer's json.dumps(..., allow_nan=False) then raised ValueError on every
        # one of those opens, repeating the same partial-start/error sequence forever.
        self.assertFalse(disko._sound_child(
            {'name': 'x', 'path': '/x', 'size': 1, 'isDir': True, 'status': float('nan')}))
        self.assertFalse(disko._sound_child(
            {'name': 'x', 'path': '/x', 'size': 1, 'isDir': True, 'status': 'bogus'}))
        self.assertTrue(disko._sound_child(
            {'name': 'x', 'path': '/x', 'size': 1, 'isDir': True, 'status': 'partial'}))
        self.assertTrue(disko._sound_child({'name': 'x', 'path': '/x', 'size': 1, 'isDir': True}))

    def test_non_bool_mount_rejected(self):
        # 'mount' is always a plain bool in the real app (see _mount_child); a
        # wrong-typed value here isn't known to crash anything today, but is exactly
        # the kind of hand-edited/corrupted field this validator exists to catch.
        self.assertFalse(disko._sound_child(
            {'name': 'x', 'path': '/x', 'size': None, 'isDir': True, 'mount': 'yes'}))
        self.assertTrue(disko._sound_child(
            {'name': 'x', 'path': '/x', 'size': None, 'isDir': True, 'mount': True}))

    def test_unhashable_file_type_and_status_rejected_not_crashed(self):
        # 'fileType' and 'status' are checked against known values with 'in' on a
        # dict/frozenset, which raises TypeError for an unhashable value (a JSON array
        # or object) rather than returning False -- cache_load() accepts these
        # JSON-native shapes, so a malformed cache entry with e.g. fileType: [] must
        # be rejected cleanly, not crash the validator meant to turn it into a miss.
        self.assertFalse(disko._sound_child(
            {'name': 'x', 'path': '/x', 'size': 1, 'isDir': False, 'fileType': []}))
        self.assertFalse(disko._sound_child(
            {'name': 'x', 'path': '/x', 'size': 1, 'isDir': False, 'fileType': {}}))
        self.assertFalse(disko._sound_child(
            {'name': 'x', 'path': '/x', 'size': 1, 'isDir': True, 'status': []}))
        self.assertFalse(disko._sound_child(
            {'name': 'x', 'path': '/x', 'size': 1, 'isDir': True, 'status': {}}))

    def test_non_finite_value_under_unknown_key_rejected(self):
        # _json_safe is a general backstop: a non-finite float under a field this
        # validator's author never anticipated (not just size/status/mount) must still
        # be rejected, since it would otherwise crash the same allow_nan=False writer.
        self.assertFalse(disko._sound_child(
            {'name': 'x', 'path': '/x', 'size': 1, 'isDir': True, 'weird': float('inf')}))
        self.assertFalse(disko._sound_child(
            {'name': 'x', 'path': '/x', 'size': 1, 'isDir': True, 'nested': {'a': float('nan')}}))
        self.assertFalse(disko._sound_child(
            {'name': 'x', 'path': '/x', 'size': 1, 'isDir': True, 'listed': [1, float('nan')]}))

    def test_oversized_integer_treated_as_miss_not_crash(self):
        # math.isfinite() itself raises OverflowError on an arbitrarily large Python int
        # (json.load decodes JSON integers with unlimited precision, so a hand-edited
        # cache file can hand one to scanned_at or a child's size) -- the validator that
        # exists specifically to turn malformed data into a clean miss must not itself
        # crash on this input.
        huge = 10 ** 400
        key = disko._cache_key(self.root)
        with disko._cache_lock:
            disko._cache[key] = {'children': [], 'scanned_at': huge, 'version': disko.CACHE_VERSION}
        self.assertIsNone(disko.cache_get(self.root))
        with disko._cache_lock:
            disko._cache[key] = {
                'children': [{'name': 'x', 'path': '/x', 'size': huge, 'isDir': True}],
                'scanned_at': 1.0, 'version': disko.CACHE_VERSION,
            }
        self.assertIsNone(disko.cache_get(self.root))

    def test_non_dict_cache_entry_treated_as_miss(self):
        # A genuinely non-dict, non-None, truthy value: None alone would already be
        # caught by cache_get's earlier "not a sound entry" check without ever
        # exercising the isinstance(dict) guard this test is meant to prove.
        with disko._cache_lock:
            disko._cache[disko._cache_key(self.root)] = 'oops-a-string'
        self.assertIsNone(disko.cache_get(self.root))
        # cache_set must not crash on this malformed entry either (it reads the same
        # raw _cache dict via its own staleness check).
        self.assertTrue(disko.cache_set(self.root, []))

    def test_delete_persists(self):
        disko.cache_set(self.root, [])
        disko.cache_delete(self.root)
        self.assertIsNone(disko.cache_get(self.root))
        disko.cache_load()
        self.assertIsNone(disko.cache_get(self.root))

    def test_load_missing_or_corrupt_file(self):
        disko.cache_load()  # file does not exist yet
        self.assertEqual(disko._cache, {})
        with open(disko.CACHE_FILE, 'w') as f:
            f.write('{not json')
        disko.cache_load()
        self.assertEqual(disko._cache, {})

    def test_load_non_object_json_is_ignored(self):
        with open(disko.CACHE_FILE, 'w') as f:
            json.dump([1, 2], f)
        disko.cache_load()
        self.assertEqual(disko._cache, {})

    def test_save_is_private_atomic_and_leaves_no_temp_files(self):
        disko.cache_set(self.root, [])
        if os.name == 'posix':
            self.assertEqual(os.stat(disko.CACHE_FILE).st_mode & 0o777, 0o600)
        self.assertEqual(os.listdir(self.cache_dir), ['cache.json'])

    @unittest.skipUnless(hasattr(os, 'symlink') and os.name == 'posix', 'needs symlinks')
    def test_save_does_not_write_through_symlink(self):
        victim = os.path.join(self.cache_dir, 'victim.txt')
        with open(victim, 'w') as f:
            f.write('V')
        os.symlink(victim, disko.CACHE_FILE)
        disko.cache_set(self.root, [])
        with open(victim) as f:
            self.assertEqual(f.read(), 'V')
        self.assertFalse(os.path.islink(disko.CACHE_FILE))

    def test_failed_save_keeps_old_cache(self):
        disko.cache_set(self.root, [])
        with open(disko.CACHE_FILE) as f:
            before = f.read()
        orig_replace = disko.os.replace

        def boom(*a, **k):
            raise OSError('simulated failure')
        disko.os.replace = boom
        try:
            disko.cache_set(os.path.join(self.root, 'big'), [])
        finally:
            disko.os.replace = orig_replace
        with open(disko.CACHE_FILE) as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(os.listdir(self.cache_dir), ['cache.json'])

    def test_no_save_when_persistence_disabled(self):
        disko._persist_cache = False
        disko.cache_set(self.root, [])
        self.assertFalse(os.path.exists(disko.CACHE_FILE))


class TestBoundedScanning(TempTreeMixin, unittest.TestCase):
    def test_server_is_threaded(self):
        self.assertTrue(issubclass(disko.Server, disko.ThreadingHTTPServer))
        self.assertTrue(disko.Server.daemon_threads)

    def test_prefetch_depth_is_bounded(self):
        # big/ has a nested/ child: with PREFETCH_MAX_DEPTH == 1 only big/ (and
        # small/) get prefetched, never big/nested/.
        self.assertEqual(disko.PREFETCH_MAX_DEPTH, 1)
        events = []
        disko.stream_directory(self.root, events.append)
        _wait_for_prefetch()
        self.assertIsNotNone(disko.cache_get(self.root))
        self.assertIsNotNone(disko.cache_get(os.path.join(self.root, 'big')))
        self.assertIsNone(disko.cache_get(os.path.join(self.root, 'big', 'nested')))

    def test_schedule_prefetch_depth_zero_is_noop(self):
        disko.schedule_prefetch([{'path': os.path.join(self.root, 'big'), 'isDir': True, 'size': 1}], depth=0)
        with disko._prefetch_lock:
            self.assertEqual(disko._prefetching, set())

    def test_submit_scan_dedupes_in_flight(self):
        p = os.path.join(self.root, 'big')
        with disko._prefetch_lock:
            disko._prefetching.add(p)
        try:
            self.assertFalse(disko.submit_scan(p))
        finally:
            with disko._prefetch_lock:
                disko._prefetching.discard(p)

    def _cache_hit_events(self, age, stop=None):
        disko.cache_set(self.root, [], scanned_at=time.time() - age)
        events = []

        def write_event(data):
            if data is not None:  # skip heartbeats
                events.append(data)
        disko.stream_directory(self.root, write_event, stop=stop)
        self.assertTrue(events[0]['from_cache'])
        return events

    def test_fresh_cache_hit_does_not_refresh(self):
        events = self._cache_hit_events(0)
        self.assertEqual([e['type'] for e in events], ['start', 'done'])

    def test_cached_child_cannot_override_event_type(self):
        # write_event({'type': 'child', **child}) would let a cached child's own
        # 'type' key (from a corrupted/hand-edited cache file -- _sound_child allows
        # unknown extra keys) win over the dict literal's 'type': 'child', turning
        # the SSE event into a bogus 'done'/'error' and truncating the listing (the
        # browser's EventSource closes on an unexpected 'done'/'error'). The server-
        # controlled discriminator must always win regardless of what's cached.
        poisoned = {'name': 'x', 'path': os.path.join(self.root, 'x'), 'size': 1,
                    'isDir': False, 'type': 'done'}
        disko.cache_set(self.root, [poisoned], scanned_at=time.time())
        events = []
        disko.stream_directory(self.root, lambda d: d is not None and events.append(d))
        self.assertEqual([e['type'] for e in events], ['start', 'child', 'done'])
        self.assertEqual(events[1]['name'], 'x')

    def test_stream_rescans_once_after_version_bump(self):
        # Simulate a pre-upgrade cache entry (no 'version' key) already in memory --
        # cache_get must treat it as a miss exactly once, then a normal cache hit
        # after the live scan re-populates it in the current shape.
        with disko._cache_lock:
            disko._cache[disko._cache_key(self.root)] = {'children': [], 'scanned_at': time.time()}
        events_1 = []
        disko.stream_directory(self.root, lambda d: d is not None and events_1.append(d))
        self.assertFalse(events_1[0]['from_cache'])
        events_2 = []
        disko.stream_directory(self.root, lambda d: d is not None and events_2.append(d))
        self.assertTrue(events_2[0]['from_cache'])
        entry = disko.cache_get(self.root)
        self.assertEqual(entry['version'], disko.CACHE_VERSION)

    def test_stale_cache_hit_revalidates_in_stream(self):
        stale_at = time.time() - disko.CACHE_TTL - 10
        events = self._cache_hit_events(disko.CACHE_TTL + 10)
        types = [e['type'] for e in events]
        self.assertEqual(types[:2], ['start', 'revalidating'])
        self.assertEqual(types[2], 'refresh')
        self.assertEqual(types[-1], 'done')
        fresh = {e['name']: e for e in events[3:-1]}
        self.assertGreater(fresh['big']['size'], 0)
        entry = disko.cache_get(self.root)
        self.assertGreater(entry['scanned_at'], stale_at)
        self.assertEqual(events[2]['scanned_at'], entry['scanned_at'])
        self.assertIn('big', [c['name'] for c in entry['children']])

    def test_stale_cached_child_type_key_cannot_override_event(self):
        # Same discriminator-override risk as the fresh-cache-hit case, but for the
        # cached children emitted before revalidation starts on a stale entry.
        poisoned = {'name': 'x', 'path': os.path.join(self.root, 'x'), 'size': 1,
                    'isDir': False, 'type': 'error'}
        disko.cache_set(self.root, [poisoned], scanned_at=time.time() - disko.CACHE_TTL - 10)
        events = []
        disko.stream_directory(self.root, lambda d: d is not None and events.append(d))
        child_events = [e for e in events if e.get('name') == 'x']
        self.assertTrue(child_events)
        for e in child_events:
            self.assertEqual(e['type'], 'child')

    def test_stale_revalidate_finishes_after_disconnect(self):
        stop = threading.Event()
        stop.set()  # client is already gone once the cached listing is sent
        events = self._cache_hit_events(disko.CACHE_TTL + 10, stop=stop)
        self.assertNotIn('refresh', [e['type'] for e in events])
        deadline = time.time() + 10
        while time.time() < deadline and not disko.cache_get(self.root)['children']:
            time.sleep(0.05)
        self.assertIn('big', [c['name'] for c in disko.cache_get(self.root)['children']])

    def test_stale_revalidation_is_deduped_per_path(self):
        # Many tabs/reloads on the same stale folder share one rescan.
        disko.cache_set(self.root, [], scanned_at=time.time() - disko.CACHE_TTL - 10)
        calls = []
        real_scan, real_hb = disko.scan_to_list, disko.HEARTBEAT_SECS

        def slow_scan(path, **kw):
            calls.append(path)
            time.sleep(0.5)
            return real_scan(path, **kw)

        disko.scan_to_list, disko.HEARTBEAT_SECS = slow_scan, 0.05
        results = []
        try:
            def client():
                events = []
                disko.stream_directory(self.root, lambda d: d is not None and events.append(d))
                results.append(events)
            threads = [threading.Thread(target=client) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        finally:
            disko.scan_to_list, disko.HEARTBEAT_SECS = real_scan, real_hb
        self.assertEqual(calls.count(self.root), 1)  # (child prefetches are separate paths)
        self.assertEqual(len(results), 5)
        for events in results:
            types = [e['type'] for e in events]
            self.assertIn('refresh', types)
            self.assertEqual(types[-1], 'done')
        self.assertEqual(disko._revalidations, {})

    def test_macos_root_reports_requested_path(self):
        # '/' is scanned as the Data volume on macOS but still reported as '/'.
        real = disko._resolve_scan_path
        disko._resolve_scan_path = lambda p: self.root if disko.norm_path(p) == '/' else real(p)
        try:
            events = []
            disko.stream_directory('/', events.append)
        finally:
            disko._resolve_scan_path = real
        self.assertEqual(events[0]['type'], 'start')
        self.assertEqual(events[0]['path'], '/')
        self.assertEqual(events[0]['name'], '/')
        self.assertIn('big', [e['name'] for e in events if e['type'] == 'child'])

    def test_disconnect_does_not_cache_partial_results(self):
        stop = threading.Event()
        events = []

        def write_event(data):
            events.append(data)
            stop.set()  # client "disconnects" right after the start event

        disko.stream_directory(self.root, write_event, stop=stop)
        self.assertEqual(events[0]['type'], 'start')
        self.assertNotIn({'type': 'done'}, events)
        self.assertIsNone(disko.cache_get(self.root))

    def test_du_bounded_skips_when_stopped(self):
        stop = threading.Event()
        stop.set()
        self.assertEqual(disko.du_bounded(os.path.join(self.root, 'big'), stop), (None, 'cancelled'))
        size, status = disko.du_bounded(os.path.join(self.root, 'big'))
        self.assertIsNone(status)
        self.assertGreater(size, 0)

    def test_unknown_size_scan_is_streamed_but_not_cached(self):
        orig = disko.du_single
        disko.du_single = lambda path, *a: (None, 'timeout')
        try:
            events = []
            disko.stream_directory(self.root, events.append)
        finally:
            disko.du_single = orig
        kids = [e for e in events if e['type'] == 'child' and e['isDir']]
        self.assertTrue(kids)
        self.assertTrue(all(k['size'] is None and k['status'] == 'timeout' for k in kids))
        self.assertEqual(events[-1]['type'], 'done')
        self.assertIsNone(disko.cache_get(self.root))


class TestScanPriority(TempTreeMixin, unittest.TestCase):
    """A running du is killed when its scan is abandoned, and live scans come first."""

    def setUp(self):
        super(TestScanPriority, self).setUp()
        self.real_popen = disko.subprocess.Popen

    def tearDown(self):
        disko.subprocess.Popen = self.real_popen
        super(TestScanPriority, self).tearDown()

    def _slow_du(self, procs):
        """Make every du a 30s sleep, recording the processes."""
        real = self.real_popen

        def popen(cmd, **kw):
            proc = real([sys.executable, '-c', 'import time; time.sleep(30)'], **kw)
            procs.append(proc)
            return proc
        disko.subprocess.Popen = popen

    def test_cancel_kills_running_du(self):
        procs, flag = [], threading.Event()
        self._slow_du(procs)
        threading.Timer(0.3, flag.set).start()
        t0 = time.time()
        self.assertEqual(disko.du_single(self.root, flag.is_set), (None, 'cancelled'))
        self.assertLess(time.time() - t0, 5)
        self.assertIsNotNone(procs[0].poll())  # process is gone, not orphaned

    def test_timeout_kills_running_du(self):
        procs = []
        self._slow_du(procs)
        orig = disko.DU_TIMEOUT
        disko.DU_TIMEOUT = 0.3
        try:
            self.assertEqual(disko.du_single(self.root), (None, 'timeout'))
        finally:
            disko.DU_TIMEOUT = orig
        self.assertIsNotNone(procs[0].poll())

    def test_disconnect_kills_live_scan_du(self):
        procs, stop, events = [], threading.Event(), []
        self._slow_du(procs)
        threading.Timer(0.3, stop.set).start()
        t0 = time.time()
        disko.stream_directory(self.root, events.append, stop=stop)
        deadline = time.time() + 5
        while any(p.poll() is None for p in procs) and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(procs)
        self.assertTrue(all(p.poll() is not None for p in procs))
        self.assertLess(time.time() - t0, 5)

    def test_live_scan_preempts_prefetch_du(self):
        procs = []
        self._slow_du(procs)
        result = []
        t = threading.Thread(target=lambda: result.append(disko.du_bounded(self.root, background='prefetch')))
        t.start()
        deadline = time.time() + 5
        while not procs and time.time() < deadline:
            time.sleep(0.02)
        with disko.live_scan():
            t.join(5)
        self.assertEqual(result, [(None, 'cancelled')])
        self.assertIsNotNone(procs[0].poll())

    def test_background_du_waits_for_live_scan(self):
        result = []
        with disko.live_scan():
            t = threading.Thread(target=lambda: result.append(
                disko.du_bounded(os.path.join(self.root, 'big'), background='revalidate')))
            t.start()
            time.sleep(0.3)
            self.assertEqual(result, [])  # still waiting
        t.join(10)
        self.assertIsNone(result[0][1])
        self.assertGreater(result[0][0], 0)

    def test_preempted_prefetch_is_not_cached(self):
        orig = disko.du_single
        disko.du_single = lambda path, *a: (None, 'cancelled')
        try:
            disko._prefetching.add(self.root)
            disko._do_prefetch(self.root, 1)
        finally:
            disko.du_single = orig
        self.assertIsNone(disko.cache_get(self.root))
        self.assertNotIn(self.root, disko._prefetching)


class TestCacheStaleness(TempTreeMixin, unittest.TestCase):
    def test_older_scan_does_not_overwrite_newer(self):
        self.assertTrue(disko.cache_set(self.root, [], scanned_at=200.0))
        self.assertFalse(disko.cache_set(self.root, [{'name': 'x', 'path': '/x', 'size': 1, 'isDir': True}],
                                         scanned_at=100.0))
        entry = disko.cache_get(self.root)
        self.assertEqual(entry['children'], [])
        self.assertEqual(entry['scanned_at'], 200.0)

    def test_keys_are_normalized(self):
        disko.cache_set(self.root + '/big/', [], scanned_at=1.0)
        self.assertIsNotNone(disko.cache_get(os.path.join(self.root, 'big')))
        disko.cache_delete(os.path.join(self.root, 'big', 'nested', '..') + '/')
        self.assertIsNone(disko.cache_get(os.path.join(self.root, 'big')))

    def test_rescan_size_propagates_to_cached_ancestors(self):
        big = os.path.join(self.root, 'big')
        nested = os.path.join(big, 'nested')
        root_children = [
            {'name': 'big', 'path': big, 'size': 10, 'isDir': True},
            {'name': 'small', 'path': os.path.join(self.root, 'small'), 'size': 50, 'isDir': True},
        ]
        disko.cache_set(self.root, root_children, scanned_at=100.0)
        disko.cache_set(big, [{'name': 'nested', 'path': nested, 'size': 5, 'isDir': True},
                              {'name': '(loose files)', 'path': big, 'size': 5, 'isDir': False}],
                        scanned_at=100.0)
        held = disko.cache_get(self.root)['children']  # a stream may be iterating this list
        disko.cache_set(nested, [{'name': 'deep', 'path': os.path.join(nested, 'deep'), 'size': 95,
                                  'isDir': True, 'status': 'partial'}], scanned_at=200.0)

        big_entry = disko.cache_get(big)['children']
        self.assertEqual(big_entry[0]['size'], 95)
        self.assertEqual(big_entry[0]['status'], 'partial')
        root_entry = disko.cache_get(self.root)['children']
        self.assertEqual([c['name'] for c in root_entry], ['big', 'small'])  # re-sorted
        self.assertEqual(root_entry[0]['size'], 100)
        self.assertEqual(root_entry[0]['status'], 'partial')
        self.assertEqual(held[0]['size'], 10)  # old list untouched

    def test_propagation_skips_mount_points(self):
        # Drilling into a mount point must not pull the other filesystem into the parent's totals.
        mnt = os.path.join(self.root, 'mnt')
        disko.cache_set(self.root, [{'name': 'big', 'path': os.path.join(self.root, 'big'), 'size': 100, 'isDir': True},
                                    {'name': 'mnt', 'path': mnt, 'size': 0, 'isDir': True, 'mount': True}],
                        scanned_at=100.0)
        disko.cache_set(mnt, [{'name': 'huge', 'path': os.path.join(mnt, 'huge'), 'size': 10 ** 12, 'isDir': True}],
                        scanned_at=200.0)
        sizes = {c['name']: c['size'] for c in disko.cache_get(self.root)['children']}
        self.assertEqual(sizes, {'big': 100, 'mnt': 0})

    def test_propagation_skips_newer_ancestor(self):
        big = os.path.join(self.root, 'big')
        disko.cache_set(self.root, [{'name': 'big', 'path': big, 'size': 10, 'isDir': True}], scanned_at=300.0)
        disko.cache_set(big, [{'name': 'a', 'path': os.path.join(big, 'a'), 'size': 99, 'isDir': True}],
                        scanned_at=200.0)
        self.assertEqual(disko.cache_get(self.root)['children'][0]['size'], 10)

    def test_ancestor_skipped_by_propagation_still_treated_as_miss(self):
        # Seed an old-format (no 'version' key) ancestor entry, then trigger cache_set on
        # a descendant. _propagate_size now stops at the first non-current-version
        # ancestor (_current_entry) rather than updating its raw data in place: an entry
        # that's already a guaranteed cache_get miss regardless is about to be discarded
        # on its own next real scan, so writing into it gains nothing (and, per the
        # review that prompted this, could otherwise let a stale/future scanned_at on
        # such an entry silently block a fresh scan's cache_set from ever replacing it --
        # see test_future_dated_old_version_entry_is_replaced_by_next_scan). The old
        # entry's own size must therefore stay exactly as seeded, untouched.
        big = os.path.join(self.root, 'big')
        with disko._cache_lock:
            disko._cache[disko._cache_key(self.root)] = {
                'children': [{'name': 'big', 'path': big, 'size': 10, 'isDir': True}],
                'scanned_at': 100.0,
            }
        disko.cache_set(big, [{'name': 'a', 'path': os.path.join(big, 'a'), 'size': 99, 'isDir': True}],
                        scanned_at=200.0)
        with disko._cache_lock:
            raw = disko._cache[disko._cache_key(self.root)]
        self.assertEqual(raw['children'][0]['size'], 10)  # left untouched, not propagated into
        self.assertIsNone(disko.cache_get(self.root))  # still unversioned: a miss

    def test_future_dated_old_version_entry_is_replaced_by_next_scan(self):
        # An old-version (or unversioned) entry with a scanned_at that happens to be
        # LATER than a fresh scan's start time (e.g. after a clock rollback, or simply a
        # hand-edited cache) must not block that fresh scan from replacing it -- only a
        # CURRENT-version entry's timestamp should ever win a "newer scan" race, since an
        # old-version entry is already being migrated away, not legitimately competing.
        with disko._cache_lock:
            disko._cache[disko._cache_key(self.root)] = {
                'children': [], 'scanned_at': time.time() + 3600,  # "from the future"
            }
        self.assertTrue(disko.cache_set(self.root, [], scanned_at=time.time()))
        entry = disko.cache_get(self.root)
        self.assertIsNotNone(entry)  # replaced and now a normal, current-version hit
        self.assertEqual(entry['version'], disko.CACHE_VERSION)

    def test_propagation_skips_corrupted_ancestor(self):
        # A malformed ancestor entry (e.g. a hand-edited or foreign cache file --
        # cache_load only validates the top-level JSON is a dict, never each per-path
        # value) must not crash cache_set for a descendant being scanned.
        big = os.path.join(self.root, 'big')
        with disko._cache_lock:
            disko._cache[disko._cache_key(self.root)] = 'not-a-dict'
        self.assertTrue(disko.cache_set(big, [{'name': 'a', 'path': os.path.join(big, 'a'),
                                               'size': 99, 'isDir': True}], scanned_at=200.0))
        self.assertIsNone(disko.cache_get(self.root))  # still corrupted/unrecognized: a miss


class TestHTTPSmoke(TempTreeMixin, unittest.TestCase):
    def setUp(self):
        super(TestHTTPSmoke, self).setUp()
        self._orig_default = disko._default_path
        disko._default_path = self.root
        self.server = disko.Server(('127.0.0.1', 0), disko.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        disko._default_path = self._orig_default
        super(TestHTTPSmoke, self).tearDown()

    def _get(self, path, headers=None, method='GET'):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            conn.request(method, path, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.getheader('Content-Type', ''), resp.read()
        finally:
            conn.close()

    def _stream(self, extra='', path=None):
        from urllib.parse import quote
        status, ctype, body = self._get('/stream?path=' + quote(path or self.root) + extra)
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith('text/event-stream'), ctype)
        events = []
        for block in body.decode('utf-8').split('\n\n'):
            block = block.strip()
            if block.startswith(':'):  # SSE comment (keepalive heartbeat)
                continue
            if block:
                self.assertTrue(block.startswith('data: '), block)
                events.append(json.loads(block[len('data: '):]))
        return events

    def test_index_returns_html(self):
        status, ctype, body = self._get('/')
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith('text/html'), ctype)
        text = body.decode('utf-8')
        self.assertIn('<!DOCTYPE html>', text)
        self.assertNotIn('%%DEFAULT_PATH%%', text)
        self.assertIn(self.root, text)

    def test_default_path_cannot_break_out_of_script(self):
        disko._default_path = '/tmp/</script><script>x<!--'
        status, _, body = self._get('/')
        self.assertEqual(status, 200)
        text = body.decode('utf-8')
        self.assertNotIn('</script><script>x', text)
        self.assertNotIn('x<!--', text)
        self.assertIn('\\u003c/script>\\u003cscript>x', text)
        self.assertIn('</script>', text)

    def test_stream_emits_events_then_done(self):
        events = self._stream()
        types = [e['type'] for e in events]
        self.assertEqual(types[0], 'start')
        self.assertEqual(types[-1], 'done')
        self.assertFalse(events[0]['from_cache'])
        self.assertEqual(events[0]['path'], self.root)
        # total_dirs counts every child event, including the "(documents)" bucket entry,
        # so the client's progress bar reaches exactly 100%.
        self.assertEqual(events[0]['total_dirs'], 4)
        self.assertEqual(events[0]['total_dirs'], sum(1 for e in events if e['type'] == 'child'))
        names = {e['name'] for e in events if e['type'] == 'child'}
        self.assertEqual(names, {'big', 'small', 'empty', '(documents)'})

        # Second request is served from the cache populated by the first.
        events = self._stream()
        self.assertEqual(events[0]['type'], 'start')
        self.assertTrue(events[0]['from_cache'])
        self.assertEqual(events[-1]['type'], 'done')

    def test_stream_total_dirs_correct_with_multiple_buckets(self):
        # This fixture's own dir ('sub') is counted as its total_dirs still uses the
        # unqualified dir count from _list_entries, +1 -- this is exactly the case where
        # the pre-fix formula (len(dirs) + (1 if any bucket nonempty else 0)) undercounts:
        # for this fixture it would have produced 1+1=2, strictly less than the 7 buckets
        # actually stream, which is how this test would have caught the regression.
        type_root = os.path.join(self.root, 'types')
        os.mkdir(type_root)
        bucket_files = _make_type_tree(type_root)
        events = self._stream(path=type_root)
        expected = len(TYPE_TREE_DIRS) + len(bucket_files)  # fixture's own dirs + non-empty type buckets
        self.assertEqual(events[0]['total_dirs'], expected)
        self.assertEqual(events[0]['total_dirs'], sum(1 for e in events if e['type'] == 'child'))
        names = {e['name'] for e in events if e['type'] == 'child'}
        self.assertEqual(names, {'sub', '(images)', '(video)', '(archives)', '(documents)', '(code)', '(other)'})

    def test_stream_missing_path_errors(self):
        from urllib.parse import quote
        status, _, body = self._get('/stream?path=' + quote(os.path.join(self.root, 'nope')))
        self.assertEqual(status, 200)
        self.assertIn(b'"type": "error"', body)

    def test_unknown_path_404(self):
        status, _, _ = self._get('/nope')
        self.assertEqual(status, 404)

    def test_invalidate_is_post_only(self):
        from urllib.parse import quote
        disko.cache_set(self.root, [], scanned_at=time.time())
        url = '/invalidate?path=' + quote(self.root)
        self.assertEqual(self._get(url)[0], 405)
        self.assertIsNotNone(disko.cache_get(self.root))
        self.assertEqual(self._get(url, {'Origin': 'https://evil.example'}, method='POST')[0], 403)
        self.assertIsNotNone(disko.cache_get(self.root))
        self.assertEqual(self._get(url, method='POST')[0], 200)
        self.assertIsNone(disko.cache_get(self.root))

    def test_foreign_host_rejected(self):
        # DNS-rebinding protection: only loopback Host headers are served.
        status, _, _ = self._get('/', headers={'Host': 'evil.example:%d' % self.port})
        self.assertEqual(status, 403)
        status, _, _ = self._get('/', headers={'Host': 'localhost:%d' % self.port})
        self.assertEqual(status, 200)

    def test_cross_origin_rejected(self):
        status, _, body = self._get('/stream?path=/', headers={'Origin': 'http://evil.example'})
        self.assertEqual(status, 403)
        self.assertNotIn(b'data: ', body)
        status, _, _ = self._get('/', headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(status, 403)
        status, _, _ = self._get('/', headers={'Origin': 'http://127.0.0.1:%d' % self.port})
        self.assertEqual(status, 200)

    def test_no_wildcard_cors(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            conn.request('GET', '/')
            resp = conn.getresponse()
            resp.read()
            self.assertIsNone(resp.getheader('Access-Control-Allow-Origin'))
        finally:
            conn.close()


class TestCLI(unittest.TestCase):
    def setUp(self):
        self._orig = (sys.argv, disko.DU_TIMEOUT, disko._verbose, disko._default_path)

    def tearDown(self):
        sys.argv, disko.DU_TIMEOUT, disko._verbose, disko._default_path = self._orig

    def _run_main_expecting_exit(self, *args):
        sys.argv = ['disko'] + list(args)
        devnull = open(os.devnull, 'w')
        real_stderr = sys.stderr
        sys.stderr = devnull
        try:
            with self.assertRaises(SystemExit) as cm:
                disko.main()
        finally:
            sys.stderr = real_stderr
            devnull.close()
        return cm.exception.code

    def test_du_timeout_must_be_positive(self):
        self.assertEqual(self._run_main_expecting_exit('--du-timeout', '0'), 2)
        self.assertEqual(self._run_main_expecting_exit('--du-timeout', '-5'), 2)

    def test_flags_are_applied(self):
        # Occupy a port so main() applies the flags, then exits on EADDRINUSE before serving.
        busy = disko.Server(('localhost', 0), disko.Handler)
        try:
            port = busy.server_address[1]
            code = self._run_main_expecting_exit('--du-timeout', '7.5', '--verbose', '--no-browser',
                                                 '--port', str(port), '--path', '/tmp/../tmp')
        finally:
            busy.server_close()
        self.assertEqual(code, 1)
        self.assertEqual(disko.DU_TIMEOUT, 7.5)
        self.assertTrue(disko._verbose)
        self.assertEqual(disko._default_path, '/tmp')


if __name__ == '__main__':
    unittest.main()
