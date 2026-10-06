import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import hashlib
import http.client
import http.server
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import fetch
import pk
from common import KitError

PAYLOAD = b"archive bytes " * 1000
NAME = "tool-1.0.tar.gz"


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server.routes.get(self.path)
        self.server.hits.append(self.path)
        if body is None:
            self.send_error(404)
            return
        if isinstance(body, tuple) and body[0] == "drip":  # ("drip", total bytes, seconds between bytes)
            self.send_response(200)
            self.send_header("Content-Length", str(body[1]))
            self.end_headers()
            try:
                for _ in range(body[1]):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(body[2])
            except OSError:  # the client gave up: stop writing
                pass
            return
        if isinstance(body, tuple) and body[0] == "held":  # ("held", bytes, barrier, seconds to wait after it)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body[1])))
            self.end_headers()
            half = len(body[1]) // 2
            self.wfile.write(body[1][:half])
            self.wfile.flush()
            body[2].wait(timeout=20)  # every download of the test is now part-way through
            time.sleep(body[3])
            self.wfile.write(body[1][half:])
            return
        if isinstance(body, tuple):  # ("redirect", location)
            self.send_response(302)
            self.send_header("Location", body[1])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FakeResponse:
    """Yields `chunks`, then raises `error` (if any) instead of reporting end of data."""

    def __init__(self, chunks, error=None):
        self.chunks, self.error, self.closed = list(chunks), error, False

    def read(self, size=-1):
        if self.chunks:
            return self.chunks.pop(0)
        if self.error is not None:
            raise self.error
        return b""

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.requests = response, error, []

    def open(self, request, timeout=None):
        self.requests.append((request.full_url, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name).resolve() / "cache"
        env = mock.patch.dict(os.environ, {"PROGRAM_KIT_CACHE": str(self.cache), "PROGRAM_KIT_FETCH_ALLOW_HTTP": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.work = Path(tmp.name).resolve()

    def serve(self, routes):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.routes, server.hits = routes, []
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(stop)
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    def parts(self, expected):
        """The names of the download files in the cache root, once `expected` of them are there."""
        deadline = time.monotonic() + 20
        while True:  # the server reaches its barrier before a client has opened its file
            found = sorted(p.name for p in self.cache.glob(".*.part")) if self.cache.is_dir() else []
            if len(found) >= expected or time.monotonic() > deadline:
                return found
            time.sleep(0.01)

    def pin(self, url="http://127.0.0.1:1/x", data=PAYLOAD, name=NAME, **over):
        return {"url": url, "sha256": sha(data), "size": len(data), "filename": name, **over}

    def digest_dir(self, data=PAYLOAD):
        return self.cache / sha(data)

    def leftovers(self, data=PAYLOAD):
        """What a fetch left behind: the entries of the digest directory and any file in the cache root."""
        directory = self.digest_dir(data)
        inside = sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []
        return inside + (sorted(p.name for p in self.cache.iterdir() if not p.is_dir()) if self.cache.is_dir() else [])


class Names(Base):
    def test_archive_name_rejects_traversal_dot_and_empty(self):
        for bad in ("", ".", "..", "../x", "a/b", "a\\b", ".hidden", "-x", "a b", "a\nb", "a\x00b", "é", None, 5):
            with self.subTest(bad=bad), self.assertRaises(KitError):
                fetch.archive_name(bad)
        for good in ("a", "tool-1.0.tar.gz", "A_b-c.1"):
            self.assertEqual(fetch.archive_name(good), good)

    def test_cache_path_validates_digest_and_name(self):
        good = "a" * 64
        self.assertEqual(fetch.cache_path(self.cache, good, NAME), self.cache / good / NAME)
        for bad in ("A" * 64, "a" * 63, "a" * 65, "../" + "a" * 61, ""):
            with self.subTest(bad=bad), self.assertRaises(KitError):
                fetch.cache_path(self.cache, bad, NAME)
        with self.assertRaises(KitError):
            fetch.cache_path(self.cache, good, "../x")

    def test_cache_root_honours_environment_then_default(self):
        self.assertEqual(fetch.cache_root(), self.cache)
        with mock.patch.dict(os.environ):
            os.environ.pop("PROGRAM_KIT_CACHE")
            self.assertEqual(fetch.cache_root(), Path.home() / ".cache" / "program-kit" / "fetch")

    def test_malformed_pins_are_kit_errors_before_any_network_call(self):
        opener = FakeOpener(error=AssertionError("network touched"))
        good = self.pin()
        bad_pins = [
            {k: v for k, v in good.items() if k != "url"},
            {k: v for k, v in good.items() if k != "size"},
            {**good, "size": "5"}, {**good, "size": True}, {**good, "size": -1},
            {**good, "sha256": "ABC"}, {**good, "sha256": "a" * 63}, {**good, "filename": "../x"},
            {**good, "url": 5}, "not a pin",
        ]
        for bad in bad_pins:
            with self.subTest(bad=bad), self.assertRaises(KitError):
                fetch.fetch(self.cache, [good, bad], opener=opener)
            with self.subTest(verify=bad), self.assertRaises(KitError):
                fetch.verify(self.cache, [good, bad])
        self.assertEqual(opener.requests, [])
        self.assertFalse(self.cache.exists())


class Verify(Base):
    def test_verify_never_constructs_opener_and_names_fetch_on_miss(self):
        def boom(*a, **k):
            raise AssertionError("network path touched")

        with mock.patch.object(urllib.request, "build_opener", boom), \
                mock.patch.object(urllib.request, "urlopen", boom):
            rows = fetch.verify(self.cache, [self.pin()])
        self.assertEqual([(r["filename"], r["state"]) for r in rows], [(NAME, "missing")])
        # the CLI names the fetch command
        pins = self.work / "pins.json"
        pins.write_text(json.dumps([self.pin()]))
        code, out, err = run_cli("fetch", "verify", "--pins", str(pins))
        self.assertEqual(code, 1)
        self.assertEqual(err, f"error: {NAME} not in the cache; run: pk fetch --pins {pins}\n")
        self.assertEqual(out, "")

    def test_verify_treats_symlinked_entry_as_miss(self):
        outside = self.work / "outside.bin"
        outside.write_bytes(PAYLOAD)
        directory = self.digest_dir()
        directory.mkdir(parents=True)
        (directory / NAME).symlink_to(outside)
        self.assertEqual(fetch.verify(self.cache, [self.pin()])[0]["state"], "missing")
        self.assertIsNone(fetch.cached(self.cache, sha(PAYLOAD), NAME))

    def test_verify_treats_symlinked_digest_directory_as_miss(self):
        real = self.work / "real"
        real.mkdir()
        (real / NAME).write_bytes(PAYLOAD)
        self.cache.mkdir()
        self.digest_dir().symlink_to(real, target_is_directory=True)
        self.assertEqual(fetch.verify(self.cache, [self.pin()])[0]["state"], "missing")

    def test_cached_misses_non_file_and_mismatch_and_hits_good_entry(self):
        directory = self.digest_dir()
        (directory / NAME).mkdir(parents=True)  # a directory is not a cache entry
        self.assertIsNone(fetch.cached(self.cache, sha(PAYLOAD), NAME))
        (directory / NAME).rmdir()
        (directory / NAME).write_bytes(b"other")
        self.assertIsNone(fetch.cached(self.cache, sha(PAYLOAD), NAME))
        self.assertTrue((directory / NAME).exists())  # cached() never deletes
        (directory / NAME).write_bytes(PAYLOAD)
        self.assertEqual(fetch.cached(self.cache, sha(PAYLOAD), NAME), directory / NAME)
        self.assertEqual(fetch.verify(self.cache, [self.pin()])[0]["state"], "cached")


class Fetch(Base):
    def test_fetch_downloads_and_caches(self):
        server, base = self.serve({"/a": PAYLOAD})
        rows = fetch.fetch(self.cache, [self.pin(base + "/a")])
        self.assertEqual([(r["filename"], r["state"]) for r in rows], [(NAME, "fetched")])
        self.assertEqual((self.digest_dir() / NAME).read_bytes(), PAYLOAD)
        self.assertEqual(self.leftovers(), [NAME])

    def test_fetch_refuses_digest_mismatch_and_leaves_no_file(self):
        wrong = b"x" * len(PAYLOAD)
        server, base = self.serve({"/a": wrong})
        rows = fetch.fetch(self.cache, [self.pin(base + "/a")])
        self.assertEqual(rows[0]["state"], "refused")
        self.assertIn("digest mismatch", rows[0]["reason"])
        self.assertEqual(self.leftovers(), [])

    def test_fetch_refuses_size_mismatch(self):
        server, base = self.serve({"/short": PAYLOAD[:-1], "/long": PAYLOAD + b"!"})
        short = fetch.fetch(self.cache, [self.pin(base + "/short")])[0]
        long = fetch.fetch(self.cache, [self.pin(base + "/long")])[0]
        self.assertEqual((short["state"], long["state"]), ("refused", "refused"))
        self.assertIn("size", short["reason"])
        self.assertIn("size", long["reason"])
        self.assertEqual(self.leftovers(), [])

    def test_oversize_stream_stops_as_soon_as_the_pin_is_exceeded(self):
        chunks = [b"x" * 10, b"x" * 10, b"x" * 10]
        response = FakeResponse(chunks)
        data = b"y" * 15
        row = fetch.fetch(self.cache, [self.pin(data=data)], opener=FakeOpener(response))[0]
        self.assertEqual(row["state"], "refused")
        self.assertEqual(len(response.chunks), 1)  # the third chunk was never read
        self.assertTrue(response.closed)
        self.assertEqual(self.leftovers(data), [])

    def test_fetch_is_idempotent_for_cached_entry(self):
        server, base = self.serve({"/a": PAYLOAD})
        pin = self.pin(base + "/a")
        fetch.fetch(self.cache, [pin])
        before = (self.digest_dir() / NAME).stat().st_ino
        opener = FakeOpener(error=AssertionError("network touched"))
        rows = fetch.fetch(self.cache, [pin], opener=opener)
        self.assertEqual(rows[0]["state"], "cached")
        self.assertEqual(len(server.hits), 1)
        self.assertEqual((self.digest_dir() / NAME).stat().st_ino, before)

    def test_fetch_replaces_mismatching_regular_entry(self):
        directory = self.digest_dir()
        directory.mkdir(parents=True)
        (directory / NAME).write_bytes(b"corrupt")
        server, base = self.serve({"/a": PAYLOAD})
        self.assertEqual(fetch.fetch(self.cache, [self.pin(base + "/a")])[0]["state"], "fetched")
        self.assertEqual((directory / NAME).read_bytes(), PAYLOAD)

    def test_fetch_cleans_temp_on_mid_download_error_without_traceback(self):
        for error in (ConnectionResetError("reset by peer"), http.client.IncompleteRead(b"x", 5)):
            with self.subTest(error=type(error).__name__):
                response = FakeResponse([PAYLOAD[:100]], error=error)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rows = fetch.fetch(self.cache, [self.pin()], opener=FakeOpener(response))
                self.assertEqual(rows[0]["state"], "refused")
                self.assertEqual(len(rows[0]["reason"].splitlines()), 1)
                self.assertNotIn("Traceback", rows[0]["reason"] + err.getvalue())
                self.assertEqual(self.leftovers(), [])
                self.assertTrue(response.closed)

    def test_transport_errors_on_open_become_refusals(self):
        for error in (urllib.error.URLError("no route"), OSError("boom"), http.client.BadStatusLine("x"),
                      TimeoutError("timed out")):
            with self.subTest(error=type(error).__name__):
                rows = fetch.fetch(self.cache, [self.pin()], opener=FakeOpener(error=error))
                self.assertEqual(rows[0]["state"], "refused")
                self.assertEqual(len(rows[0]["reason"].splitlines()), 1)
        self.assertEqual(self.leftovers(), [])

    def test_http_error_status_is_refused(self):
        server, base = self.serve({})
        rows = fetch.fetch(self.cache, [self.pin(base + "/missing")])
        self.assertEqual(rows[0]["state"], "refused")
        self.assertIn("404", rows[0]["reason"])
        self.assertEqual(self.leftovers(), [])

    def test_timeout_and_url_reach_the_opener(self):
        opener = FakeOpener(FakeResponse([PAYLOAD]))
        fetch.fetch(self.cache, [self.pin("https://example.test/x")], opener=opener, timeout=42)
        self.assertEqual(opener.requests, [("https://example.test/x", 42)])
        self.assertEqual(self.leftovers(), [NAME])

    def test_scheme_rules(self):
        opener = FakeOpener(error=AssertionError("network touched"))
        for url in ("file:///etc/passwd", "ftp://example.test/x", "gopher://x/y", "data:text/plain,hi", "/just/a/path"):
            with self.subTest(url=url):
                row = fetch.fetch(self.cache, [self.pin(url)], opener=opener)[0]
                self.assertEqual((row["state"], row["reason"]), ("refused", "scheme not allowed"))
        with mock.patch.dict(os.environ):  # http needs the explicit opt-in; https never does
            os.environ.pop("PROGRAM_KIT_FETCH_ALLOW_HTTP")
            row = fetch.fetch(self.cache, [self.pin("http://example.test/x")], opener=opener)[0]
            self.assertEqual((row["state"], row["reason"]), ("refused", "scheme not allowed"))
            os.environ["PROGRAM_KIT_FETCH_ALLOW_HTTP"] = "yes"
            row = fetch.fetch(self.cache, [self.pin("http://example.test/x")], opener=opener)[0]
            self.assertEqual(row["reason"], "scheme not allowed")
            ok = fetch.fetch(self.cache, [self.pin("https://example.test/x")],
                             opener=FakeOpener(FakeResponse([PAYLOAD])))[0]
            self.assertEqual(ok["state"], "fetched")
        self.assertEqual(self.leftovers(), [NAME])

    def test_redirect_to_a_disallowed_scheme_is_refused(self):
        with mock.patch.dict(os.environ):
            server, base = self.serve({"/r": ("redirect", "file:///etc/passwd")})
            rows = fetch.fetch(self.cache, [self.pin(base + "/r")])
        self.assertEqual(rows[0]["state"], "refused")
        self.assertEqual(self.leftovers(), [])

    def test_fetch_replaces_symlinked_entry_without_following(self):
        target = self.work / "target.bin"
        target.write_bytes(b"precious")
        directory = self.digest_dir()
        directory.mkdir(parents=True)
        (directory / NAME).symlink_to(target)
        server, base = self.serve({"/a": PAYLOAD})
        rows = fetch.fetch(self.cache, [self.pin(base + "/a")])
        self.assertEqual(rows[0]["state"], "fetched")
        self.assertEqual(target.read_bytes(), b"precious")
        self.assertFalse((directory / NAME).is_symlink())
        self.assertEqual((directory / NAME).read_bytes(), PAYLOAD)

    def test_fetch_replaces_symlinked_digest_directory_without_following(self):
        real = self.work / "real"
        real.mkdir()
        (real / "keep.txt").write_text("keep")
        self.cache.mkdir()
        self.digest_dir().symlink_to(real, target_is_directory=True)
        server, base = self.serve({"/a": PAYLOAD})
        self.assertEqual(fetch.fetch(self.cache, [self.pin(base + "/a")])[0]["state"], "fetched")
        self.assertEqual(sorted(p.name for p in real.iterdir()), ["keep.txt"])
        self.assertFalse(self.digest_dir().is_symlink())
        self.assertEqual((self.digest_dir() / NAME).read_bytes(), PAYLOAD)

    def test_a_download_in_flight_is_a_file_of_its_own_in_the_cache_root(self):
        seen = {}

        def look():
            seen["parts"] = self.parts(1)
            seen["root"] = sorted(p.name for p in self.cache.iterdir())
            seen["modes"] = [p.stat().st_mode & 0o777 for p in self.cache.iterdir()]
            seen["digest directory"] = self.digest_dir().exists()

        old = os.umask(0o027)
        self.addCleanup(os.umask, old)
        server, base = self.serve({"/a": ("held", PAYLOAD, threading.Barrier(1, action=look), 0)})
        self.assertEqual(fetch.fetch(self.cache, [self.pin(base + "/a")])[0]["state"], "fetched")
        self.assertEqual(seen["root"], seen["parts"], seen)
        self.assertEqual(len(seen["root"]), 1, seen)
        self.assertRegex(seen["root"][0], rf"\A\.{re.escape(NAME)}\.\d+\.[0-9a-f]+\.part\Z")
        self.assertFalse(seen["digest directory"])  # nothing carries the final name until it is complete
        if hasattr(os, "fchmod"):
            self.assertEqual(seen["modes"], [0o640])  # created with its mode: 0644 narrowed by the umask
        self.assertEqual(self.leftovers(), [NAME])

    def test_the_umask_is_never_read_or_changed_by_a_fetch(self):
        server, base = self.serve({"/a": PAYLOAD, "/wrong": b"x" * len(PAYLOAD)})
        with mock.patch.object(os, "umask", side_effect=AssertionError("umask touched")) as umask:
            rows = fetch.fetch(self.cache, [self.pin(base + "/wrong", name="w.bin"), self.pin(base + "/a")])
        self.assertEqual([row["state"] for row in rows], ["refused", "fetched"], rows)
        self.assertEqual(umask.call_count, 0)

    def test_malformed_url_is_refused_and_later_pins_still_run(self):
        server, base = self.serve({"/a": PAYLOAD})
        pins = self.work / "pins.json"
        pins.write_text(json.dumps([self.pin("https://[bad", data=b"zz", name="b.bin"), self.pin(base + "/a")]))
        for url in ("https://[bad", "http://[::1"):
            row = fetch.fetch(self.cache, [self.pin(url, data=b"zz", name="b.bin")])[0]
            self.assertEqual((row["state"], row["reason"]), ("refused", "malformed url"))
        code, out, err = run_cli("fetch", "--pins", str(pins))
        self.assertEqual(code, 1)
        self.assertEqual(err, "error: b.bin: malformed url\n")
        self.assertEqual(out.split()[:2], ["fetched", NAME])

    def test_whole_download_deadline_refuses_a_slow_drip(self):
        clock = [1000.0]

        class Drip(FakeResponse):
            def read(self, size=-1):
                clock[0] += 400  # each chunk is within any per-operation timeout, the total is not
                return b"x"

        response = Drip([])
        with mock.patch.object(fetch.time, "monotonic", lambda: clock[0]):
            row = fetch.fetch(self.cache, [self.pin(data=b"x" * 100)], opener=FakeOpener(response), timeout=600)[0]
        self.assertEqual((row["state"], row["reason"]), ("refused", "timed out"))
        self.assertTrue(response.closed)
        self.assertEqual(self.leftovers(b"x" * 100), [])

    def test_real_slow_drip_server_is_cut_off_at_the_deadline(self):
        data = b"x" * 40
        server, base = self.serve({"/drip": ("drip", 40, 0.05)})
        started = time.monotonic()
        row = fetch.fetch(self.cache, [self.pin(base + "/drip", data=data)], timeout=0.3)[0]
        elapsed = time.monotonic() - started
        self.assertEqual((row["state"], row["reason"]), ("refused", "timed out"))
        self.assertLess(elapsed, 1.5)
        self.assertEqual(self.leftovers(data), [])
        self.assertFalse(self.digest_dir(data).exists())

    @unittest.skipUnless(hasattr(os, "fchmod"), "POSIX only")
    def test_entry_mode_follows_umask_and_is_world_readable(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        server, base = self.serve({"/a": PAYLOAD})
        fetch.fetch(self.cache, [self.pin(base + "/a")])
        self.assertEqual((self.digest_dir() / NAME).stat().st_mode & 0o777, 0o644)
        self.assertEqual(os.umask(0o022), 0o022)  # the helper left the umask as it found it

    def test_failed_fetch_creates_no_digest_directory_and_removes_none(self):
        server, base = self.serve({"/wrong": b"x" * len(PAYLOAD)})
        self.assertEqual(fetch.fetch(self.cache, [self.pin(base + "/wrong")])[0]["state"], "refused")
        self.assertFalse(self.digest_dir().exists())  # a failed attempt never creates the directory
        self.assertTrue(self.cache.exists())
        directory = self.digest_dir()
        directory.mkdir()  # as another fetch of this digest leaves it just before it publishes
        with mock.patch.object(os, "rmdir", side_effect=AssertionError("rmdir called")) as rmdir:
            fetch.fetch(self.cache, [self.pin(base + "/wrong")])
        self.assertEqual(rmdir.call_count, 0)
        self.assertTrue(directory.is_dir())
        (directory / "other").write_bytes(b"keep")
        (directory / NAME).write_bytes(b"corrupt")
        fetch.fetch(self.cache, [self.pin(base + "/wrong")])
        self.assertEqual(sorted(p.name for p in directory.iterdir()), ["other", NAME])
        self.assertEqual((directory / NAME).read_bytes(), b"corrupt")  # a mismatching entry is left in place

    def test_dangling_and_looping_symlink_entries_are_misses_and_replaced(self):
        directory = self.digest_dir()
        directory.mkdir(parents=True)
        entry = directory / NAME
        server, base = self.serve({"/a": PAYLOAD})
        for kind in ("dangling", "loop"):
            with self.subTest(kind=kind):
                if entry.is_symlink() or entry.exists():
                    entry.unlink()
                if kind == "dangling":
                    entry.symlink_to(self.work / "does-not-exist")
                else:
                    entry.symlink_to(entry.name)  # points at itself
                self.assertIsNone(fetch.cached(self.cache, sha(PAYLOAD), NAME))
                self.assertEqual(fetch.verify(self.cache, [self.pin()])[0]["state"], "missing")
                self.assertEqual(fetch.fetch(self.cache, [self.pin(base + "/a")])[0]["state"], "fetched")
                self.assertFalse(entry.is_symlink())
                self.assertEqual(entry.read_bytes(), PAYLOAD)
        self.assertFalse((self.work / "does-not-exist").exists())

    def test_one_refusal_does_not_stop_the_other_pins(self):
        server, base = self.serve({"/a": PAYLOAD})
        other = b"second"
        rows = fetch.fetch(self.cache, [self.pin(base + "/nope", data=other, name="b.bin"),
                                        self.pin(base + "/a")])
        self.assertEqual([r["state"] for r in rows], ["refused", "fetched"])


class Concurrent(Base):
    """Two fetches of one digest at the same time: both succeed, or one fails cleanly."""

    def in_threads(self, *pins):
        rows = [None] * len(pins)

        def work(index, pin):
            rows[index] = fetch.fetch(self.cache, [pin])[0]

        threads = [threading.Thread(target=work, args=item) for item in enumerate(pins)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        return rows

    def test_two_fetches_of_one_digest_at_once_both_succeed(self):
        seen = {}

        def look():  # runs once, while both downloads are half-way
            seen["parts"] = self.parts(2)
            seen["final name"] = (self.digest_dir() / NAME).exists()
            seen["digest directory"] = self.digest_dir().exists()

        both = threading.Barrier(2, action=look)
        server, base = self.serve({"/a": ("held", PAYLOAD, both, 0)})
        with mock.patch.object(os, "rmdir", side_effect=AssertionError("rmdir called")) as rmdir, \
                mock.patch.object(os, "umask", side_effect=AssertionError("umask touched")) as umask:
            rows = self.in_threads(self.pin(base + "/a"), self.pin(base + "/a"))
        self.assertEqual([row["state"] for row in rows], ["fetched", "fetched"], rows)
        self.assertEqual(server.hits, ["/a", "/a"])
        self.assertEqual((rmdir.call_count, umask.call_count), (0, 0))
        self.assertFalse(seen["final name"])  # no partial file is ever visible under the final name
        self.assertEqual(len(set(seen["parts"])), 2, seen)  # each download has a file of its own
        self.assertFalse(seen["digest directory"])
        self.assertEqual((self.digest_dir() / NAME).read_bytes(), PAYLOAD)
        self.assertEqual(self.leftovers(), [NAME])
        self.assertEqual(fetch.verify(self.cache, [self.pin()])[0]["state"], "cached")

    def test_a_fetch_that_fails_beside_one_that_succeeds_takes_nothing_from_it(self):
        wrong = b"x" * len(PAYLOAD)
        for first_done in ("the failing one", "the good one"):
            with self.subTest(first_done=first_done):
                if self.cache.exists():
                    (self.digest_dir() / NAME).unlink()
                    self.digest_dir().rmdir()
                both = threading.Barrier(2)
                slow = 0.3
                server, base = self.serve({
                    "/good": ("held", PAYLOAD, both, slow if first_done == "the failing one" else 0),
                    "/bad": ("held", wrong, both, slow if first_done == "the good one" else 0)})
                with mock.patch.object(os, "rmdir", side_effect=AssertionError("rmdir called")) as rmdir:
                    good, bad = self.in_threads(self.pin(base + "/good"), self.pin(base + "/bad"))
                self.assertEqual(rmdir.call_count, 0)  # never a directory another fetch is using
                self.assertEqual(good["state"], "fetched", good)
                self.assertEqual(bad["state"], "refused", bad)
                self.assertIn("digest mismatch", bad["reason"])
                self.assertEqual(len(bad["reason"].splitlines()), 1)
                self.assertEqual((self.digest_dir() / NAME).read_bytes(), PAYLOAD)
                self.assertEqual(self.leftovers(), [NAME])

    def test_two_processes_fetching_one_digest_at_once_both_exit_zero(self):
        both = threading.Barrier(2)
        server, base = self.serve({"/a": ("held", PAYLOAD, both, 0)})
        pins = self.work / "pins.json"
        pins.write_text(json.dumps([self.pin(base + "/a")]))
        script = Path(fetch.__file__).with_name("pk.py")
        children = [subprocess.Popen([sys.executable, str(script), "fetch", "--pins", str(pins)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
                    for _ in range(2)]
        results = [(child.wait(timeout=60), *child.communicate()) for child in children]
        entry = self.digest_dir() / NAME
        self.assertEqual(results, [(0, f"fetched {NAME} {entry}\n", "")] * 2)
        self.assertEqual(server.hits, ["/a", "/a"])
        self.assertEqual(entry.read_bytes(), PAYLOAD)
        self.assertEqual(self.leftovers(), [NAME])


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = pk.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Cli(Base):
    def pins_file(self, pins):
        path = self.work / "pins.json"
        path.write_text(json.dumps(pins))
        return path

    def test_fetch_then_verify(self):
        server, base = self.serve({"/a": PAYLOAD})
        pins = self.pins_file([self.pin(base + "/a")])
        code, out, err = run_cli("fetch", "--pins", str(pins))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.split()[:2], ["fetched", NAME])
        code, out, err = run_cli("fetch", "verify", "--pins", str(pins))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.split()[:2], ["cached", NAME])
        code, out, err = run_cli("fetch", "--pins", str(pins), "verify")
        self.assertEqual(code, 0)
        self.assertEqual(len(server.hits), 1)

    def test_fetch_exits_one_if_any_refused(self):
        server, base = self.serve({})
        pins = self.pins_file([self.pin(base + "/nope")])
        code, out, err = run_cli("fetch", "--pins", str(pins))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith(f"error: {NAME}: "), err)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertNotIn("Traceback", out + err)

    def test_refusal_goes_to_stderr_and_other_pins_stay_on_stdout(self):
        server, base = self.serve({"/a": PAYLOAD})
        pins = self.pins_file([self.pin(base + "/nope", data=b"zz", name="b.bin"), self.pin(base + "/a")])
        code, out, err = run_cli("fetch", "--pins", str(pins))
        self.assertEqual(code, 1)
        self.assertEqual(out.split()[:2], ["fetched", NAME])
        self.assertTrue(err.startswith("error: b.bin: "), err)

    def test_verify_with_a_miss_prints_real_reasons_and_nothing_on_stdout(self):
        server, base = self.serve({"/a": PAYLOAD})
        good = self.pin(base + "/a")
        bad = self.pin(data=b"other", name="bad.bin")
        absent = self.pin(data=b"absent", name="absent.bin")
        fetch.fetch(self.cache, [good])
        d = self.digest_dir(b"other")
        d.mkdir(parents=True)
        (d / "bad.bin").write_bytes(b"corrupt")
        pins = self.pins_file([good, bad, absent])
        code, out, err = run_cli("fetch", "verify", "--pins", str(pins))
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.splitlines(), [f"error: bad.bin digest mismatch; run: pk fetch --pins {pins}",
                                            f"error: absent.bin not in the cache; run: pk fetch --pins {pins}"])

    def test_missing_pins_and_bad_pins_file(self):
        self.assertEqual(run_cli("fetch")[0], 1)
        self.assertEqual(run_cli("fetch", "--pins", str(self.work / "absent.json"))[0], 1)
        for content in ("{not json", json.dumps({"pins": []}), json.dumps([])):
            path = self.work / "bad.json"
            path.write_text(content)
            code, out, err = run_cli("fetch", "verify", "--pins", str(path))
            self.assertEqual((code, out), (1, ""))
            self.assertTrue(err.startswith("error: "), err)


if __name__ == "__main__":
    unittest.main()
