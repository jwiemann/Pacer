"""http_util: error mapping (status, timeout, connection failure, bad JSON) and retry policy, against a local server thread."""
import json, socket, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import http_util


class LocalServer:
    """Answers each path with its queued responses in order (the last one repeats). Records every request it gets."""

    def __init__(self):
        self.routes = {}   # path -> list of (status, body bytes, delay seconds)
        self.hits = []     # (method, path, body text)
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self._answer()

            def do_POST(self):
                self._answer()

            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8")
                srv.hits.append((self.command, self.path, body))
                queue = srv.routes.get(self.path, [(404, b"no route", 0)])
                status, payload, delay = queue.pop(0) if len(queue) > 1 else queue[0]
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:  # the client gave up (timeout test)
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def respond(self, path, *responses):
        self.routes[path] = [(r[0], r[1], r[2] if len(r) > 2 else 0) for r in responses]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def server():
    srv = LocalServer()
    yield srv
    srv.close()


@pytest.fixture
def sleeps(monkeypatch):
    """Records the retry pauses instead of waiting for them."""
    waited = []
    monkeypatch.setattr(http_util, "time", SimpleNamespace(sleep=waited.append))
    return waited


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_success_returns_the_body_text_and_sends_the_body_as_utf8(server):
    server.respond("/echo", (200, "grüße".encode("utf-8")))
    assert http_util.request_text(server.url + "/echo", method="POST", body="grüße") == "grüße"
    assert server.hits == [("POST", "/echo", "grüße")]


def test_json_body_is_serialised_and_the_reply_parsed(server):
    server.respond("/api/x", (200, b'{"ok": true, "n": 3}'))
    assert http_util.request_json(server.url + "/api/x", method="POST", body={"a": 1}) == {"ok": True, "n": 3}
    method, path, sent = server.hits[0]
    assert (method, path) == ("POST", "/api/x")
    assert json.loads(sent) == {"a": 1}


def test_empty_json_reply_is_none(server):
    server.respond("/empty", (200, b""))
    assert http_util.request_json(server.url + "/empty") is None


def test_http_error_carries_status_and_body_and_is_a_runtime_error(server):
    server.respond("/missing", (404, b"no such agent"))
    with pytest.raises(http_util.HttpError) as info:
        http_util.request_text(server.url + "/missing")
    err = info.value
    assert isinstance(err, RuntimeError)
    assert err.status == 404 and err.body == "no such agent"
    assert "HTTP 404 no such agent" in str(err)


def test_error_body_is_cut_to_a_snippet(server):
    server.respond("/big", (500, b"x" * 1000))
    with pytest.raises(http_util.HttpError) as info:
        http_util.request_text(server.url + "/big")
    assert len(info.value.body) == http_util.BODY_SNIPPET
    assert len(str(info.value)) <= 400


def test_server_errors_are_retried_then_raised(server, sleeps):
    server.respond("/flaky", (503, b"busy"))
    with pytest.raises(http_util.HttpError) as info:
        http_util.request_text(server.url + "/flaky", retries=2)
    assert info.value.status == 503
    assert len(server.hits) == 3
    assert sleeps == [1, 1]


def test_retry_succeeds_once_the_server_recovers(server, sleeps):
    server.respond("/recovering", (503, b"busy"), (200, b"up"))
    assert http_util.request_text(server.url + "/recovering", retries=1) == "up"
    assert len(server.hits) == 2


def test_client_errors_are_not_retried(server, sleeps):
    server.respond("/nope", (404, b"gone"))
    with pytest.raises(http_util.HttpError):
        http_util.request_text(server.url + "/nope", retries=2)
    assert len(server.hits) == 1 and sleeps == []


def test_connection_refused_is_an_error_without_status(sleeps):
    url = f"http://127.0.0.1:{free_port()}/x"
    with pytest.raises(http_util.HttpError) as info:
        http_util.request_text(url, retries=1)
    assert info.value.status is None
    assert sleeps == [1]


def test_slow_reply_times_out_as_an_error_without_status(server):
    server.respond("/slow", (200, b"late", 2))
    with pytest.raises(http_util.HttpError) as info:
        http_util.request_text(server.url + "/slow", timeout=0.2)
    assert info.value.status is None
    assert "timed out" in str(info.value)


def test_reply_that_is_not_json_raises_with_the_reason(server):
    server.respond("/html", (200, b"<html>login</html>"))
    assert http_util.request_text(server.url + "/html") == "<html>login</html>"
    with pytest.raises(http_util.HttpError) as info:
        http_util.request_json(server.url + "/html")
    assert info.value.status is None
    assert "not JSON" in str(info.value)


def test_malformed_url_is_an_error_without_status():
    with pytest.raises(http_util.HttpError) as info:
        http_util.request_text("not a url")
    assert info.value.status is None
