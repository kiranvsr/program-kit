import http.client
import json
import threading
import unittest

import tiny_service
from tiny_service import MAX_BODY, handle


class HandleTests(unittest.TestCase):
    def test_health_is_ok(self):
        self.assertEqual(handle("GET", "/health", b""), (200, {"status": "ok"}))

    def test_health_refuses_other_methods(self):
        self.assertEqual(handle("POST", "/health", b"")[0], 405)

    def test_echo_returns_the_json_it_was_sent(self):
        self.assertEqual(handle("POST", "/echo", b'{"a": [1, 2]}'), (200, {"echo": {"a": [1, 2]}}))

    def test_echo_accepts_any_json_value(self):
        self.assertEqual(handle("POST", "/echo", b"3"), (200, {"echo": 3}))

    def test_echo_refuses_a_body_that_is_not_json(self):
        status, payload = handle("POST", "/echo", b"not json")
        self.assertEqual((status, payload), (400, {"error": "body is not valid JSON"}))

    def test_echo_refuses_a_body_that_is_not_utf8(self):
        self.assertEqual(handle("POST", "/echo", b"\xff\xfe")[0], 400)

    def test_echo_refuses_an_empty_body(self):
        self.assertEqual(handle("POST", "/echo", b"")[0], 400)

    def test_echo_refuses_other_methods(self):
        self.assertEqual(handle("GET", "/echo", b"")[0], 405)

    def test_echo_takes_a_body_of_exactly_the_limit(self):
        body = b'"' + b"a" * (MAX_BODY - 2) + b'"'
        self.assertEqual(len(body), MAX_BODY)
        self.assertEqual(handle("POST", "/echo", body)[0], 200)

    def test_echo_refuses_a_body_over_the_limit(self):
        body = b'"' + b"a" * (MAX_BODY - 1) + b'"'
        self.assertEqual(len(body), MAX_BODY + 1)
        self.assertEqual(handle("POST", "/echo", body), (413, {"error": "body too large"}))

    def test_unknown_path_is_not_found(self):
        self.assertEqual(handle("GET", "/nope", b""), (404, {"error": "not found"}))

    def test_service_listens_on_the_loopback_address_by_default(self):
        self.assertEqual(tiny_service.HOST, "127.0.0.1")


class ServerTests(unittest.TestCase):
    """The same behaviour through a real socket, on a free port."""

    def setUp(self):
        self.server = tiny_service.make_server("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def call(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type"), json.loads(response.read())
        finally:
            connection.close()

    def test_health_over_http(self):
        self.assertEqual(self.call("GET", "/health"), (200, "application/json", {"status": "ok"}))

    def test_echo_over_http(self):
        status, kind, payload = self.call("POST", "/echo", b'{"hello": "world"}')
        self.assertEqual((status, kind, payload), (200, "application/json", {"echo": {"hello": "world"}}))

    def test_oversized_body_over_http(self):
        status, _, payload = self.call("POST", "/echo", b"x" * (MAX_BODY + 500))
        self.assertEqual((status, payload), (413, {"error": "body too large"}))

    def test_bad_content_length_over_http(self):
        status, _, _ = self.call("POST", "/echo", headers={"Content-Length": "abc"})
        self.assertEqual(status, 400)

    def test_other_methods_get_a_json_answer(self):
        self.assertEqual(self.call("PUT", "/health")[0], 405)
        self.assertEqual(self.call("PATCH", "/echo", b"{}"), (405, "application/json", {"error": "method not allowed"}))

    def test_head_gets_405_with_headers_only(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            connection.request("HEAD", "/health")
            response = connection.getresponse()
            self.assertEqual((response.status, response.read()), (405, b""))
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
