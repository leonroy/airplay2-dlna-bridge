"""Real HTTP admission/header and encoder-permit recovery regressions."""
import socket
import subprocess
import threading
import time
from unittest.mock import Mock

import pytest


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "resource did not return to baseline"
        time.sleep(0.005)


def response_headers(peer):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = peer.recv(4096)
        assert chunk, "response ended before headers"
        data += chunk
    return data


@pytest.fixture
def http_server(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "log", Mock())
    monkeypatch.setattr(bridge, "HTTP_HEADER_TIMEOUT", 2)
    monkeypatch.setattr(bridge, "HTTP_HEADER_DEADLINE", 3)
    servers, sockets = [], []

    class Server(bridge.BoundedHTTPServer):
        def __init__(self, *args, **kwargs):
            self.count_lock = threading.Lock()
            self.started = self.active = 0
            super().__init__(*args, **kwargs)

        def process_request_thread(self, *args):
            with self.count_lock:
                self.started += 1
                self.active += 1
            try:
                super().process_request_thread(*args)
            finally:
                with self.count_lock:
                    self.active -= 1

    def start(max_connections=2):
        server = Server(("127.0.0.1", 0), bridge.StreamHandler,
                        max_connections=max_connections)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        servers.append((server, thread))

        def connect(request=None):
            peer = socket.create_connection(server.server_address, timeout=3)
            peer.settimeout(3)
            sockets.append(peer)
            if request is not None:
                peer.sendall(request)
            return peer

        server.connect = connect
        return server

    yield start
    bridge.cancel_all_clients("test cleanup")
    for peer in sockets:
        peer.close()
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()
        wait_until(lambda: server.active == 0)


def test_connection_limit_rejects_before_thread_start_and_recovers(http_server):
    server = http_server()
    first, second = server.connect(), server.connect()
    wait_until(lambda: server.active == 2)
    excess = server.connect()
    assert response_headers(excess).startswith(b"HTTP/1.0 503")
    assert server.started == 2
    first.close()
    second.close()
    wait_until(lambda: server.active == 0)
    healthy = server.connect(b"GET /missing HTTP/1.0\r\n\r\n")
    assert response_headers(healthy).startswith(b"HTTP/1.0 404")
    wait_until(lambda: server.active == 0)
    assert server.started == 3


def test_connection_permit_recovers_if_handler_thread_cannot_start(
        monkeypatch, http_server):
    start = threading.Thread.start
    attempted = threading.Event()

    def fail_once(self):
        if getattr(self._target, "__name__", "") == "process_request_thread" and not attempted.is_set():
            attempted.set()
            raise RuntimeError("handler thread start failed")
        return start(self)

    monkeypatch.setattr(threading.Thread, "start", fail_once)
    server = http_server(max_connections=1)
    failed = server.connect(b"GET /missing HTTP/1.0\r\n\r\n")
    try:
        assert failed.recv(4096) == b""
    except ConnectionResetError:
        pass
    assert attempted.is_set()
    assert server.started == 0
    healthy = server.connect(b"GET /missing HTTP/1.0\r\n\r\n")
    assert response_headers(healthy).startswith(b"HTTP/1.0 404")
    wait_until(lambda: server.active == 0)


def test_header_inactivity_releases_connection_budget(bridge, monkeypatch, http_server):
    monkeypatch.setattr(bridge, "HTTP_HEADER_TIMEOUT", 0.08)
    server = http_server(max_connections=1)
    server.connect(b"GET /")
    wait_until(lambda: server.started == 1)
    wait_until(lambda: server.active == 0, timeout=0.5)
    healthy = server.connect(b"GET /missing HTTP/1.0\r\n\r\n")
    assert response_headers(healthy).startswith(b"HTTP/1.0 404")


@pytest.mark.parametrize("partial", [b"GET /", b"GET /stream.flac HTTP/1.0\r\nX: "])
def test_total_header_deadline_defeats_continuous_trickle(
        bridge, monkeypatch, http_server, partial):
    monkeypatch.setattr(bridge, "HTTP_HEADER_TIMEOUT", 0.12)
    monkeypatch.setattr(bridge, "HTTP_HEADER_DEADLINE", 0.24)
    bridge.AUDIO.begin()
    bridge.AUDIO.describe("44100/S16_LE/2")
    popen = Mock()
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    server = http_server(max_connections=1)
    peer = server.connect(partial)
    wait_until(lambda: server.active == 1)
    started = time.monotonic()
    for _ in range(10):
        time.sleep(0.03)
        try:
            peer.sendall(b"a")
        except OSError:
            break
    wait_until(lambda: server.active == 0, timeout=0.4)
    assert time.monotonic() - started < 0.7
    try:
        assert peer.recv(4096) == b""
    except ConnectionResetError:
        pass
    popen.assert_not_called()
    healthy = server.connect(b"GET /missing HTTP/1.0\r\n\r\n")
    assert response_headers(healthy).startswith(b"HTTP/1.0 404")


@pytest.mark.parametrize("path", ["/stream.flac", "/stream.wav"])
def test_header_deadline_is_disarmed_for_paused_half_closed_streams(
        bridge, monkeypatch, http_server, path):
    monkeypatch.setattr(bridge, "HTTP_HEADER_DEADLINE", 0.1)
    bridge.AUDIO.begin()
    bridge.AUDIO.describe("44100/S16_LE/2")
    server = http_server()
    peer = server.connect(f"GET {path} HTTP/1.0\r\n\r\n".encode())
    peer.shutdown(socket.SHUT_WR)
    assert response_headers(peer).startswith(b"HTTP/1.0 200")
    time.sleep(0.25)
    assert server.active == 1
    assert bridge.has_active_clients(bridge.AUDIO.snapshot().session_id)
    bridge.cancel_all_clients("test complete")
    wait_until(lambda: server.active == 0)


def test_encoder_exhaustion_rejects_without_spawning_and_cancellation_recovers(
        bridge, monkeypatch, http_server):
    monkeypatch.setattr(bridge, "ENCODER_SLOTS", threading.BoundedSemaphore(1))
    popen = subprocess.Popen
    launches = []

    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        launches.append(process)
        return process

    monkeypatch.setattr(bridge.subprocess, "Popen", launch)
    bridge.AUDIO.begin()
    bridge.AUDIO.describe("44100/S16_LE/2")
    server = http_server(max_connections=3)
    first = server.connect(b"GET /stream.flac HTTP/1.0\r\n\r\n")
    assert response_headers(first).startswith(b"HTTP/1.0 200")
    excess = server.connect(b"GET /stream.flac HTTP/1.0\r\n\r\n")
    assert response_headers(excess).startswith(b"HTTP/1.0 503")
    assert len(launches) == 1
    assert len(bridge.CLIENTS) == 1
    # WAV diagnostic requests do not consume the encoder budget.
    wav = server.connect(b"GET /stream.wav HTTP/1.0\r\n\r\n")
    assert response_headers(wav).startswith(b"HTTP/1.0 200")
    bridge.cancel_all_clients("permit recovery")
    wait_until(lambda: not bridge.CLIENTS and server.active == 0)
    assert launches[0].poll() is not None
    assert launches[0].stdin.closed and launches[0].stdout.closed
    retry = server.connect(b"GET /stream.flac HTTP/1.0\r\n\r\n")
    assert response_headers(retry).startswith(b"HTTP/1.0 200")
    assert len(launches) == 2


@pytest.mark.parametrize("failure", ["spawn", "early_exit", "headers", "feeder"])
def test_encoder_permit_recovers_after_startup_failures(
        bridge, monkeypatch, http_server, failure):
    monkeypatch.setattr(bridge, "ENCODER_SLOTS", threading.BoundedSemaphore(1))
    bridge.AUDIO.begin()
    bridge.AUDIO.describe("44100/S16_LE/2")
    if failure == "spawn":
        launch = subprocess.Popen
        attempted = threading.Event()

        def fail_once(*args, **kwargs):
            if not attempted.is_set():
                attempted.set()
                raise OSError("spawn failed")
            return launch(*args, **kwargs)

        monkeypatch.setattr(bridge.subprocess, "Popen", fail_once)
    elif failure == "early_exit":
        monkeypatch.setattr(bridge.PCMFormat, "encoder_command",
                            lambda self: ["ffmpeg", "-hide_banner", "-version"])
    elif failure == "headers":
        end_headers = bridge.StreamHandler.end_headers
        attempted = threading.Event()

        def fail_once(self):
            if not attempted.is_set():
                attempted.set()
                raise BrokenPipeError("header write failed")
            return end_headers(self)

        monkeypatch.setattr(bridge.StreamHandler, "end_headers", fail_once)
    else:
        start = threading.Thread.start
        attempted = threading.Event()

        def fail_feeder_once(self):
            if getattr(self._target, "__name__", "") == "feeder" and not attempted.is_set():
                attempted.set()
                raise RuntimeError("thread start failed")
            return start(self)

        monkeypatch.setattr(threading.Thread, "start", fail_feeder_once)
    server = http_server()
    first = server.connect(b"GET /stream.flac HTTP/1.0\r\n\r\n")
    if failure in ("early_exit", "feeder"):
        assert response_headers(first).startswith(b"HTTP/1.0 200")
    wait_until(lambda: server.started == 1 and server.active == 0)
    assert not bridge.CLIENTS
    # Capacity must be available immediately after all process/feeder cleanup.
    assert bridge.ENCODER_SLOTS.acquire(blocking=False)
    bridge.ENCODER_SLOTS.release()
    retry = server.connect(b"GET /stream.flac HTTP/1.0\r\n\r\n")
    assert response_headers(retry).startswith(b"HTTP/1.0 200")
