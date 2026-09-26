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


class TempTreeMixin(object):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='disko-tree-')
        # realpath: on macOS /var is a symlink to /private/var.
        self.root = os.path.realpath(self.tmp)
        _make_tree(self.root)
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
        # Join background refresh threads (e.g. after a cache hit) so they
        # cannot write into the next test's state, then drain prefetches.
        for t in set(threading.enumerate()) - self._threads_before:
            if not t.name.startswith('prefetch'):
                t.join(5)
        _wait_for_prefetch()
        with disko._cache_lock:
            disko._cache.clear()
        disko.CACHE_FILE = self._orig_cache_file
        disko._persist_cache = self._orig_persist
        shutil.rmtree(self.cache_dir, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)


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
        self.assertEqual(set(by_name), {'big', 'small', 'empty', '(loose files)'})

        for name in ('big', 'small', 'empty'):
            c = by_name[name]
            self.assertTrue(c['isDir'])
            self.assertEqual(c['path'], os.path.join(self.root, name))

        loose = by_name['(loose files)']
        self.assertFalse(loose['isDir'])
        self.assertEqual(loose['path'], self.root)
        self.assertEqual(loose['size'], 3000)

        # Sorted largest first, and 'big' is the largest entry.
        sizes = [c['size'] for c in children]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(children[0]['name'], 'big')

    def test_scan_normalizes_path(self):
        children = disko.scan_to_list(self.root + os.sep + '.' + os.sep)
        self.assertEqual({c['name'] for c in children}, {'big', 'small', 'empty', '(loose files)'})

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

    def _cache_hit_refreshes(self, age):
        disko.cache_set(self.root, [])
        with disko._cache_lock:
            disko._cache[self.root]['scanned_at'] = time.time() - age
        calls = []
        orig = disko.submit_scan
        disko.submit_scan = lambda path, depth=0: calls.append((path, depth)) or True
        try:
            events = []
            disko.stream_directory(self.root, events.append)
        finally:
            disko.submit_scan = orig
        self.assertTrue(events[0]['from_cache'])
        self.assertEqual(events[-1]['type'], 'done')
        return calls

    def test_fresh_cache_hit_does_not_refresh(self):
        self.assertEqual(self._cache_hit_refreshes(0), [])

    def test_stale_cache_hit_refreshes_once(self):
        calls = self._cache_hit_refreshes(disko.REFRESH_MIN_AGE + 10)
        self.assertEqual(calls, [(self.root, disko.PREFETCH_MAX_DEPTH)])

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
        disko.du_single = lambda path: (None, 'timeout')
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

    def _get(self, path, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            conn.request('GET', path, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.getheader('Content-Type', ''), resp.read()
        finally:
            conn.close()

    def _stream(self, extra=''):
        from urllib.parse import quote
        status, ctype, body = self._get('/stream?path=' + quote(self.root) + extra)
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

    def test_stream_emits_events_then_done(self):
        events = self._stream()
        types = [e['type'] for e in events]
        self.assertEqual(types[0], 'start')
        self.assertEqual(types[-1], 'done')
        self.assertFalse(events[0]['from_cache'])
        self.assertEqual(events[0]['path'], self.root)
        self.assertEqual(events[0]['total_dirs'], 3)
        names = {e['name'] for e in events if e['type'] == 'child'}
        self.assertEqual(names, {'big', 'small', 'empty', '(loose files)'})

        # Second request is served from the cache populated by the first.
        events = self._stream()
        self.assertEqual(events[0]['type'], 'start')
        self.assertTrue(events[0]['from_cache'])
        self.assertEqual(events[-1]['type'], 'done')

    def test_stream_missing_path_errors(self):
        from urllib.parse import quote
        status, _, body = self._get('/stream?path=' + quote(os.path.join(self.root, 'nope')))
        self.assertEqual(status, 200)
        self.assertIn(b'"type": "error"', body)

    def test_unknown_path_404(self):
        status, _, _ = self._get('/nope')
        self.assertEqual(status, 404)

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


if __name__ == '__main__':
    unittest.main()
