import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import hashlib
import http.client
import http.server
import io
import json
import os
import tempfile
import threading
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

    def pin(self, url="http://127.0.0.1:1/x", data=PAYLOAD, name=NAME, **over):
        return {"url": url, "sha256": sha(data), "size": len(data), "filename": name, **over}

    def digest_dir(self, data=PAYLOAD):
        return self.cache / sha(data)

    def leftovers(self, data=PAYLOAD):
        directory = self.digest_dir(data)
        return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


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
        self.assertEqual(err, f"error: {NAME} is not in the cache; run: pk fetch --pins {pins}\n")

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

    def test_temp_file_is_created_in_digest_directory(self):
        seen = []
        real = tempfile.mkstemp

        def spy(*args, **kwargs):
            seen.append((args, kwargs))
            return real(*args, **kwargs)

        server, base = self.serve({"/a": PAYLOAD})
        with mock.patch.object(fetch.tempfile, "mkstemp", spy):
            fetch.fetch(self.cache, [self.pin(base + "/a")])
        self.assertEqual(len(seen), 1)
        self.assertEqual(Path(seen[0][1]["dir"]), self.digest_dir())

    def test_one_refusal_does_not_stop_the_other_pins(self):
        server, base = self.serve({"/a": PAYLOAD})
        other = b"second"
        rows = fetch.fetch(self.cache, [self.pin(base + "/nope", data=other, name="b.bin"),
                                        self.pin(base + "/a")])
        self.assertEqual([r["state"] for r in rows], ["refused", "fetched"])


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
        self.assertEqual(out.split()[:2], ["refused", NAME])
        self.assertNotIn("Traceback", out + err)

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
