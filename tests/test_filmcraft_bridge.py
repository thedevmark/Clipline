from contextlib import contextmanager
import json
import socket
import socketserver
import threading
import time

import pytest

from native.services.filmcraft_bridge import (
    EditorUnavailable, FilmcraftBridge, NativeCommandError, OutcomeUnknown,
)


@contextmanager
def native_server(answer):
    seen = []
    failures = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                request = json.loads(self.rfile.readline(4 * 1024 * 1024 + 2))
                seen.append(request)
                answer(self, request, len(seen))
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # The deadline tests intentionally close their client connection.
            except Exception as exc:
                failures.append(exc)

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    with Server(("127.0.0.1", 0), Handler) as server:
        owner = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        owner.start()
        try:
            yield server.server_address[1], seen
        finally:
            server.shutdown()
            owner.join(2)
    assert not failures, failures


def reply(handler, request, result):
    handler.wfile.write(json.dumps({"id": request["id"], "ok": True, "result": result}).encode() + b"\n")


def test_native_command_dispatch_preserves_ids_unicode_and_nested_parameters():
    with native_server(lambda h, q, n: reply(h, q, {"revision": 7})) as (port, seen):
        result = FilmcraftBridge(port).execute("metadata.set", {"field": "title", "value": "Grüße 🎮"})
    assert result == {"revision": 7}
    assert len(seen) == 1
    assert seen[0]["method"] == "engine.execute"
    assert seen[0]["params"] == {"command": "metadata.set", "params": {"field": "title", "value": "Grüße 🎮"}}
    assert isinstance(seen[0]["id"], str)


def test_command_applied_then_reply_lost_is_unknown_and_is_never_repeated():
    applied = []

    def apply_and_disconnect(handler, request, count):
        applied.append(request["params"])

    with native_server(apply_and_disconnect) as (port, seen):
        with pytest.raises(OutcomeUnknown) as failure:
            FilmcraftBridge(port).execute("timeline.razor", {"seconds": 2})
    assert len(applied) == len(seen) == 1
    assert failure.value.request_id == seen[0]["id"]
    assert failure.value.method == "engine.execute"


def test_native_error_retains_identity_and_does_not_claim_no_side_effects():
    def fail(handler, request, count):
        handler.wfile.write(json.dumps({"id": request["id"], "ok": False, "error": "stale linked revision"}).encode() + b"\n")

    with native_server(fail) as (port, seen):
        with pytest.raises(NativeCommandError) as failure:
            FilmcraftBridge(port).execute("timeline.trim", {"deltaFrames": 2})
    assert failure.value.reason == "stale linked revision"
    assert failure.value.request_id == seen[0]["id"]
    assert len(seen) == 1


def test_server_ui_wait_timeout_is_an_unknown_outcome():
    def timeout(handler, request, count):
        handler.wfile.write(json.dumps({"id": request["id"], "ok": False, "error": "timeout"}).encode() + b"\n")

    with native_server(timeout) as (port, seen):
        with pytest.raises(OutcomeUnknown):
            FilmcraftBridge(port).execute("timeline.razor", {"seconds": 1})
    assert len(seen) == 1


@pytest.mark.parametrize("bad", [
    lambda q: b"not-json\n",
    lambda q: json.dumps({"id": "another request", "ok": True, "result": 1}).encode() + b"\n",
    lambda q: json.dumps({"id": q["id"], "ok": 1, "result": 1}).encode() + b"\n",
    lambda q: json.dumps({"id": q["id"], "ok": True}).encode() + b"\n",
    lambda q: json.dumps({"id": q["id"], "ok": False, "error": {}}).encode() + b"\n",
    lambda q: ('{"id":"' + q["id"] + '","ok":true,"result":NaN}\n').encode(),
    lambda q: ('{"id":"' + q["id"] + '","ok":true,"result":1e309}\n').encode(),
])
def test_malformed_or_wrong_reply_is_unknown_without_retry(bad):
    with native_server(lambda h, q, n: h.wfile.write(bad(q))) as (port, seen):
        with pytest.raises(OutcomeUnknown):
            FilmcraftBridge(port).call("engine.inspect")
    assert len(seen) == 1


def test_reply_is_bounded_even_without_a_newline():
    with native_server(lambda h, q, n: h.wfile.write(b"x" * 1000)) as (port, seen):
        with pytest.raises(OutcomeUnknown, match="size limit"):
            FilmcraftBridge(port, max_reply=128).call("engine.inspect")
    assert len(seen) == 1


def test_reply_exactly_at_limit_can_finish_with_a_newline():
    def answer(handler, request, count):
        line = json.dumps({"id": request["id"], "ok": True, "result": None}).encode()
        line += b" " * (256 - len(line))
        handler.wfile.write(line + b"\n")

    with native_server(answer) as (port, _):
        assert FilmcraftBridge(port, max_reply=256).call("engine.inspect") is None


def test_slow_stream_has_one_total_deadline(monkeypatch):
    class SlowConnection:
        def __init__(self):
            self.deadlines = []
            self.sent = []
            self.reads = 0

        def __enter__(self): return self
        def __exit__(self, *_): pass
        def settimeout(self, value): self.deadlines.append(value)
        def sendall(self, data): self.sent.append(data)
        def recv(self, size):
            self.reads += 1
            return b" "

    connection = SlowConnection()
    clock = iter([0, 0.1, 0.4, 0.8, 1.1])
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: connection)
    monkeypatch.setattr("native.services.filmcraft_bridge.time.monotonic", lambda: next(clock))
    with pytest.raises(OutcomeUnknown, match="deadline expired"):
        FilmcraftBridge(9876, timeout=1).call("engine.inspect")
    assert connection.deadlines == pytest.approx([0.9, 0.6, 0.2])
    assert connection.reads == 2
    assert len(connection.sent) == 1


def test_late_reply_cannot_be_consumed_by_a_later_call():
    release_first = threading.Event()
    first_finished = threading.Event()

    def answer(handler, request, count):
        if count == 1:
            assert release_first.wait(10)
        try:
            reply(handler, request, count)
        finally:
            if count == 1:
                first_finished.set()

    with native_server(answer) as (port, seen):
        try:
            with pytest.raises(OutcomeUnknown):
                FilmcraftBridge(port, timeout=0.5).execute("timeline.razor", {"seconds": 1})
            assert FilmcraftBridge(port, timeout=5).call("engine.inspect") == 2
        finally:
            release_first.set()
            assert first_finished.wait(5)
    assert len(seen) == 2
    assert seen[0]["id"] != seen[1]["id"]


def test_invalid_input_never_connects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid requests must not reach the native editor")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    bridge = FilmcraftBridge(9876)
    for params in ([1], {1: "bad key"}, {"x": float("nan")}, {"x": object()}, {"x": "\ud800"}, {"x": "a" * (4 * 1024 * 1024)}):
        with pytest.raises(ValueError):
            bridge.call("engine.inspect", params)
    circular = {}; circular["self"] = circular
    with pytest.raises(ValueError):
        bridge.call("engine.inspect", circular)
    with pytest.raises(ValueError, match="wait=false"):
        bridge.execute("file.exportMedia", {"wait": True})


@pytest.mark.parametrize("method", ["engine.execute", "ui.menu.invoke"])
@pytest.mark.parametrize("key", ["command", "id"])
def test_direct_control_aliases_cannot_start_a_blocking_export(monkeypatch, method, key):
    def forbidden(*args, **kwargs):
        pytest.fail("Blocking exports must not reach the UI thread")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    with pytest.raises(ValueError, match="wait=false"):
        FilmcraftBridge(9876).call(method, {key: "file.exportMedia", "params": {"path": "clip.mp4", "wait": True}})


def test_background_export_preserves_native_job_result():
    with native_server(lambda h, q, n: reply(h, q, {"job": 4, "path": "clip.mp4"})) as (port, seen):
        assert FilmcraftBridge(port).execute("file.exportMedia", {"wait": False}) == {"job": 4, "path": "clip.mp4"}
    assert seen[0]["params"]["params"] == {"wait": False}


def test_connection_failure_means_nothing_was_sent_and_has_no_retry(monkeypatch):
    attempts = []

    def unavailable(*args, **kwargs):
        attempts.append(args)
        raise ConnectionRefusedError("not listening")

    monkeypatch.setattr(socket, "create_connection", unavailable)
    with pytest.raises(EditorUnavailable):
        FilmcraftBridge(9876).execute("timeline.razor", {"seconds": 2})
    assert len(attempts) == 1
    assert attempts[0][0] == ("127.0.0.1", 9876)


@pytest.mark.parametrize("kwargs", [
    {"port": True}, {"port": 0}, {"port": 65536}, {"port": "9876"},
    {"timeout": True}, {"timeout": float("nan")}, {"timeout": float("inf")},
    {"timeout": 0}, {"timeout": 61}, {"max_reply": True}, {"max_reply": 0},
    {"max_reply": 33 * 1024 * 1024},
])
def test_configuration_is_bounded(kwargs):
    with pytest.raises(ValueError):
        FilmcraftBridge(**({"port": 9876} | kwargs))
