"""Real pipe/socket lifecycle regressions; no HTTP listeners or disk artifacts."""
import socket
import subprocess
import struct
import sys
import threading
from unittest.mock import Mock

import pytest


def tcp_pair():
    """Use TCP for real RST semantics; a Unix socketpair cannot reproduce them."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        peer = socket.create_connection(listener.getsockname(), timeout=3)
        try:
            connection, _ = listener.accept()
        except BaseException:
            peer.close()
            raise
        return peer, connection


class StreamProbe:
    def __init__(self, bridge, method, records, tcp=False):
        self.bridge = bridge
        self.records = records
        self.peer, self.connection = tcp_pair() if tcp else socket.socketpair()
        self.peer.settimeout(3)
        self.headers_sent = threading.Event()
        self.errors = []
        self.handler = object.__new__(bridge.StreamHandler)
        self.handler.connection = self.connection
        self.handler.headers = {}
        self.handler.wfile = self.connection.makefile("wb", buffering=0)
        self.handler.send_response = Mock()
        self.handler.send_header = Mock()
        self.handler.end_headers = self.headers_sent.set
        self.handler.address_string = lambda: "socketpair"

        def run():
            try:
                getattr(self.handler, method)()
            except BaseException as error:
                self.errors.append(error)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def connected(self):
        assert self.headers_sent.wait(3)
        self.client = self.records[self.connection]
        return self.client

    def finished(self):
        self.thread.join(3)
        assert not self.thread.is_alive(), "stream handler retained after cancellation"
        assert not self.errors
        assert self.client not in self.bridge.CLIENTS
        if self.client.encoder is not None:
            assert self.client.encoder.poll() is not None
            assert self.client.encoder.stdin.closed
            assert self.client.encoder.stdout.closed
        if self.client.feeder is not None:
            assert not self.client.feeder.is_alive()

    def receive(self, length):
        data = b""
        while len(data) < length:
            chunk = self.peer.recv(length - len(data))
            assert chunk, "response ended before expected bytes"
            data += chunk
        return data

    def close(self):
        with self.bridge.CLIENTS_LOCK:
            clients = [c for c in self.bridge.CLIENTS
                       if c.connection is self.connection]
        for client in clients:
            client.interrupt()
        self.thread.join(3)
        self.handler.wfile.close()
        self.peer.close()
        self.connection.close()


@pytest.fixture
def stream_probe(bridge, monkeypatch):
    bridge.AUDIO.begin()
    bridge.AUDIO.describe("44100/S16_LE/2")
    monkeypatch.setattr(bridge, "log", Mock())
    probes = []
    records = {}
    register = bridge.register_client

    def record(client):
        register(client)
        records[client.connection] = client

    monkeypatch.setattr(bridge, "register_client", record)

    def start(method="serve_flac", tcp=False):
        probe = StreamProbe(bridge, method, records, tcp=tcp)
        probes.append(probe)
        return probe

    yield start
    bridge.cancel_all_clients("test cleanup")
    bridge.AUDIO.end("test cleanup", drain=False)
    for probe in probes:
        probe.close()


@pytest.mark.parametrize("method", ["serve_flac", "serve_wav"])
@pytest.mark.parametrize("cancel", ["resync", "session", "ring"])
def test_idle_explicit_cancellation_releases_every_resource(bridge, stream_probe, method, cancel):
    probe = stream_probe(method)
    client = probe.connected()
    assert bridge.has_active_clients(client.session_id)
    if cancel == "resync":
        bridge.drop_clients("test resync")
        assert not bridge.has_active_clients(client.session_id)
        assert not client.ring.closed
    elif cancel == "session":
        bridge.cancel_session_clients(client.session_id, "test session cancellation")
        bridge.cancel_session_clients(client.session_id, "duplicate cancellation")
        assert not bridge.has_active_clients(client.session_id)
    else:
        bridge.AUDIO.end("test session ended", drain=False)
    probe.finished()


@pytest.mark.parametrize("method", ["serve_flac", "serve_wav"])
def test_idle_tcp_reset_releases_every_resource(bridge, stream_probe, method):
    probe = stream_probe(method, tcp=True)
    client = probe.connected()
    if method == "serve_wav":
        assert probe.receive(44).startswith(b"RIFF")
    probe.peer.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    probe.peer.close()
    # No PCM and no explicit cancellation: definite RST must trigger cleanup.
    probe.finished()
    assert not bridge.has_active_clients(client.session_id)


@pytest.mark.parametrize("method", ["serve_flac", "serve_wav"])
@pytest.mark.parametrize("tcp", [False, True])
def test_healthy_pause_and_request_half_close_remain_active(bridge, stream_probe, method, tcp):
    probe = stream_probe(method, tcp=tcp)
    client = probe.connected()
    # Closing request input does not establish that response consumption ended.
    probe.peer.shutdown(socket.SHUT_WR)
    probe.thread.join(0.3)
    assert probe.thread.is_alive()
    assert bridge.has_active_clients(client.session_id)
    if method == "serve_wav":
        assert probe.receive(44).startswith(b"RIFF")
        client.ring.write(b"\x01\x00\x02\x00")
        assert probe.receive(4) == b"\x01\x00\x02\x00"
    bridge.cancel_session_clients(client.session_id, "test complete")
    probe.finished()


def test_cancellation_is_session_scoped_and_resync_preserves_join_boundary(bridge):
    bridge.AUDIO.begin()
    bridge.AUDIO.describe("44100/S16_LE/2")
    pcm = bridge.PCMFormat(44100, "S16_LE", 2)
    sockets = []
    clients = []
    try:
        for session in (1, 2):
            peer, connection = socket.socketpair()
            sockets.extend([peer, connection])
            client = bridge.StreamClient(bridge.Ring(pcm, session), connection)
            clients.append(client)
            bridge.register_client(client)
        bridge.cancel_session_clients(1, "old session")
        assert not bridge.has_active_clients(1)
        assert bridge.has_active_clients(2)
        ring = bridge.AUDIO.snapshot()
        ring.write(b"\x00" * 400)
        bridge.drop_clients("resync")
        assert ring.session_start == ring.end()
        assert not bridge.has_active_clients(2)
    finally:
        for client in clients:
            client.interrupt()
            bridge.unregister_client(client)
        for sock in sockets:
            sock.close()


def test_encoder_spawn_failure_unregisters_without_acknowledging_success(bridge, stream_probe, monkeypatch):
    monkeypatch.setattr(bridge.subprocess, "Popen", Mock(side_effect=OSError("spawn failed")))
    probe = stream_probe()
    probe.thread.join(3)
    assert not probe.thread.is_alive()
    assert not probe.errors
    assert not bridge.CLIENTS
    probe.handler.send_response.assert_not_called()


def test_early_encoder_exit_reaps_process_and_joins_feeder(bridge, stream_probe, monkeypatch):
    monkeypatch.setattr(bridge.PCMFormat, "encoder_command",
                        lambda self: ["ffmpeg", "-hide_banner", "-version"])
    probe = stream_probe()
    probe.connected()
    probe.finished()


def test_repeated_idle_reconnects_return_registry_to_baseline(bridge, stream_probe):
    session = bridge.AUDIO.snapshot().session_id
    for _ in range(4):
        probe = stream_probe()
        probe.connected()
        bridge.cancel_session_clients(session, "reconnect")
        probe.finished()
        assert not bridge.CLIENTS


def test_cancellation_interrupts_a_stalled_response_write(bridge, stream_probe, monkeypatch):
    probe = stream_probe("serve_wav")
    client = probe.connected()
    assert probe.receive(44).startswith(b"RIFF")
    probe.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    entered, completed = threading.Event(), threading.Event()
    write = probe.handler.write_stream

    def tracked_write(*args):
        entered.set()
        write(*args)
        completed.set()

    monkeypatch.setattr(probe.handler, "write_stream", tracked_write)
    client.ring.write(b"\x00" * (44100 * 4))
    assert entered.wait(3)
    assert not completed.wait(0.2), "probe did not backpressure the response"
    bridge.cancel_session_clients(client.session_id, "stalled response")
    probe.finished()


@pytest.mark.parametrize("tcp", [False, True])
def test_encoder_output_after_half_close_is_still_readable(bridge, stream_probe, monkeypatch, tcp):
    ring = bridge.AUDIO.snapshot()
    feeder_waiting = threading.Event()
    read_from = ring.read_from

    def read(*args, **kwargs):
        feeder_waiting.set()
        return read_from(*args, **kwargs)

    monkeypatch.setattr(ring, "read_from", read)
    probe = stream_probe(tcp=tcp)
    client = probe.connected()
    assert feeder_waiting.wait(3)
    probe.peer.shutdown(socket.SHUT_WR)
    # Enough raw PCM to pass the current default input probing threshold.
    client.ring.write(b"\x00" * (44100 * 4 * 6))
    assert probe.receive(4) == b"fLaC"
    assert bridge.has_active_clients(client.session_id)
    bridge.cancel_session_clients(client.session_id, "complete")
    probe.finished()


def test_cancellation_unblocks_a_feeder_writing_to_a_full_encoder_pipe(bridge, stream_probe, monkeypatch):
    # This child deliberately never drains stdin. The ring payload exceeds pipe
    # capacity, so cancellation must kill it before joining/closing the feeder.
    monkeypatch.setattr(bridge.PCMFormat, "encoder_command", lambda self: [
        sys.executable, "-B", "-c", "import time; time.sleep(60)"])
    entered, completed = threading.Event(), threading.Event()
    popen = subprocess.Popen

    class TrackedInput:
        def __init__(self, pipe):
            self.pipe = pipe

        def write(self, data):
            entered.set()
            result = self.pipe.write(data)
            completed.set()
            return result

        def flush(self):
            self.pipe.flush()

        def close(self):
            self.pipe.close()

        @property
        def closed(self):
            return self.pipe.closed

    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        process.stdin = TrackedInput(process.stdin)
        return process

    monkeypatch.setattr(bridge.subprocess, "Popen", launch)
    probe = stream_probe()
    client = probe.connected()
    client.ring.write(b"\x00" * (44100 * 4))
    assert entered.wait(3)
    assert not completed.wait(0.1), "probe did not block on encoder input"
    bridge.cancel_session_clients(client.session_id, "blocked encoder")
    probe.finished()
