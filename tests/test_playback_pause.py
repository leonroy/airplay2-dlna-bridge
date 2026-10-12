"""Pause replay, deadline, and transport races with controlled time and real audio."""
import socket
import struct
import subprocess
import threading
from unittest.mock import Mock

import pytest

from test_stream_lifecycle import stream_probe
from test_http_limits import http_server, response_headers, wait_until


@pytest.fixture
def playback(bridge, monkeypatch, request):
    clock = Mock(return_value=100.0)
    monkeypatch.setattr(bridge.time, "monotonic", clock)
    renderer = bridge.Renderer("127.0.0.1")
    renderer.action = Mock(return_value=True)
    stream_type = getattr(request, "param", "Buffered")
    if stream_type is not None:
        bridge.handle_playback_metadata("styp", stream_type.encode(), renderer, {})
    bridge.handle_playback_metadata("pbeg", b"", renderer, {})
    bridge.handle_output_description(b"44100/S16_LE/2", renderer)
    bridge.NOW_PLAYING.update(title="Test track", artist="Test artist")
    bridge.STATE["dirty"] = 99.0
    bridge.AUDIO.feed(b"\x01\x00\x02\x00" * 8192)
    renderer.dispatch_once()
    renderer.dispatch_once()
    renderer.action.reset_mock()
    return renderer, clock


def event(bridge, renderer, code):
    assert bridge.handle_playback_metadata(code, b"", renderer, {})


def actions(renderer):
    return [call.args[0] for call in renderer.action.call_args_list]


def rejected_fetch(bridge, method="serve_flac"):
    handler = object.__new__(bridge.StreamHandler)
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    getattr(handler, method)()
    handler.send_response.assert_called_once_with(503)


@pytest.mark.parametrize("duration", [.234, .372, .749])
@pytest.mark.parametrize("playback,resume_code", [
    ("Buffered", "pres"), ("Classic", "prsm"), ("Realtime", "prsm"), (None, "prsm"),
], indirect=["playback"])
def test_brief_pause_preserves_stream_without_transport_commands(bridge, playback, duration, resume_code):
    renderer, clock = playback
    original = bridge.AUDIO.ring
    event(bridge, renderer, "paus")
    assert bridge.AUDIO.snapshot() is None
    assert bridge.status_snapshot()["audio"]["state"] == "paused"
    assert bridge.status_snapshot()["track"]["title"] == "Test track"
    assert not renderer.dispatch_once()
    clock.return_value += duration
    event(bridge, renderer, resume_code)
    bridge.AUDIO.maintain(renderer)
    assert bridge.AUDIO.snapshot() is original
    assert not renderer.dispatch_once()
    assert actions(renderer) == []


@pytest.mark.parametrize("playback", ["Buffered", None], indirect=True)
@pytest.mark.parametrize("phase", ["startup", "flush"])
@pytest.mark.parametrize("delay", [.1, .749, .75, 15.0])
def test_buffering_completion_does_not_resume_authoritative_pause(bridge, playback, phase, delay):
    renderer, clock = playback
    if bridge.AUDIO.stream_type is None:
        event(bridge, renderer, "pres")  # Learn support before stream metadata arrives.
    original = bridge.AUDIO.ring
    generation = bridge.AUDIO.playback_generation
    event(bridge, renderer, "paus")
    deadline = bridge.AUDIO.pause_deadline
    if phase == "flush":
        event(bridge, renderer, "pfls")
    clock.return_value += delay
    for _ in range(2):
        event(bridge, renderer, "prsm")
        assert bridge.AUDIO.paused
        assert bridge.AUDIO.snapshot() is None
        for method in ("serve_flac", "serve_wav"):
            rejected_fetch(bridge, method)
    assert bridge.AUDIO.playback_generation == generation
    if delay < .75:
        assert bridge.AUDIO.pause_deadline == deadline
        assert not original.closed
        assert not renderer.dispatch_once()
    clock.return_value = 115.0
    bridge.AUDIO.maintain(renderer)
    assert original.closed
    assert renderer.stop_revision == 1
    assert renderer.dispatch_once()
    bridge.AUDIO.feed(b"\x55" * 8)
    event(bridge, renderer, "prsm")
    assert bridge.AUDIO.paused
    assert bridge.AUDIO.snapshot() is None
    assert renderer.stop_revision == 1
    assert not renderer.dispatch_once()
    assert actions(renderer) == ["Stop"]

    event(bridge, renderer, "pres")
    fresh_ring = bridge.AUDIO.ring
    assert fresh_ring is not original
    assert bridge.AUDIO.awaiting_pcm
    event(bridge, renderer, "prsm")
    event(bridge, renderer, "pres")
    assert bridge.AUDIO.ring is fresh_ring
    fresh = b"\x03\x00\x04\x00" * 100
    bridge.AUDIO.feed(fresh)
    assert fresh_ring.read_from(0)[1] == fresh
    clock.return_value += 1
    renderer.dispatch_once()
    renderer.dispatch_once()
    assert actions(renderer) == ["Stop", "SetAVTransportURI", "Play"]


@pytest.mark.parametrize("notice", ["pres", "Buffered"])
@pytest.mark.parametrize("later_type", [None, "Classic", "Realtime"])
def test_authoritative_resume_survives_startup_and_resets_at_session_end(
        bridge, monkeypatch, notice, later_type):
    clock = Mock(return_value=100.0)
    monkeypatch.setattr(bridge.time, "monotonic", clock)
    renderer = Mock()
    if notice == "pres":
        event(bridge, renderer, "pres")
    else:
        bridge.handle_playback_metadata("styp", b"Buffered", renderer, {})
    event(bridge, renderer, "pbeg")
    if later_type is not None:
        bridge.handle_playback_metadata("styp", later_type.encode(), renderer, {})
    event(bridge, renderer, "pbeg")  # Duplicate startup must not forget the notice.
    bridge.handle_output_description(b"44100/S16_LE/2", renderer)
    bridge.AUDIO.feed(b"\x01\x00\x02\x00")
    event(bridge, renderer, "paus")
    clock.return_value += .1
    event(bridge, renderer, "prsm")
    assert bridge.AUDIO.paused
    assert bridge.AUDIO.snapshot() is None
    clock.return_value += .65
    bridge.AUDIO.maintain(renderer)
    renderer.stop.assert_called_once_with(bridge.AUDIO.session_id, preserve_volume=True)

    event(bridge, renderer, "pend")
    bridge.handle_playback_metadata("styp", b"Classic", renderer, {})
    event(bridge, renderer, "pbeg")
    bridge.handle_output_description(b"44100/S16_LE/2", renderer)
    bridge.AUDIO.feed(b"\x03\x00\x04\x00")
    legacy_ring = bridge.AUDIO.ring
    event(bridge, renderer, "paus")
    clock.return_value += .1
    event(bridge, renderer, "prsm")
    assert bridge.AUDIO.snapshot() is legacy_ring
    assert not bridge.AUDIO.paused


@pytest.mark.parametrize("duration", [.75, .751, 15.0])
def test_expired_deadline_enforced_even_before_maintenance_tick(bridge, playback, duration):
    renderer, clock = playback
    original = bridge.AUDIO.ring
    event(bridge, renderer, "paus")
    clock.return_value += duration
    event(bridge, renderer, "pres")
    assert original.closed
    assert bridge.AUDIO.ring is not original
    assert bridge.AUDIO.snapshot() is None  # Resume needs new PCM, not old backlog.
    assert bridge.AUDIO.awaiting_pcm
    renderer.dispatch_once()
    assert actions(renderer) == ["Stop"]
    bridge.AUDIO.feed(b"\x03\x00\x04\x00" * 8192)
    clock.return_value += 1
    renderer.dispatch_once()
    renderer.dispatch_once()
    assert actions(renderer) == ["Stop", "SetAVTransportURI", "Play"]


def test_duplicate_pause_does_not_extend_deadline_and_duplicate_resume_does_not_reset_audio(bridge, playback):
    renderer, clock = playback
    event(bridge, renderer, "paus")
    deadline = bridge.AUDIO.pause_deadline
    clock.return_value += .5
    event(bridge, renderer, "paus")
    assert bridge.AUDIO.pause_deadline == deadline
    clock.return_value += .25
    bridge.AUDIO.maintain(renderer)
    event(bridge, renderer, "pres")
    fresh = b"\x03\x00\x04\x00" * 100
    bridge.AUDIO.feed(fresh)
    ring = bridge.AUDIO.ring
    event(bridge, renderer, "prsm")
    assert bridge.AUDIO.ring is ring
    assert ring.read_from(0)[1] == fresh


def test_long_pause_discards_audio_and_preserves_format_and_counters(bridge, playback):
    renderer, clock = playback
    bridge.AUDIO.receiver_stats = {"missing_audio_blocks": 1, "too_late_audio_blocks": 2, "retry_requests": 3}
    event(bridge, renderer, "paus")
    clock.return_value += .75
    bridge.AUDIO.maintain(renderer)
    before = bridge.AUDIO.discarded_bytes
    bridge.AUDIO.feed(b"\x55" * 1000)
    assert bridge.AUDIO.discarded_bytes == before + 1000
    assert not bridge.AUDIO.pending
    assert bridge.AUDIO.pcm.description == "44100/S16_LE/2"
    assert bridge.AUDIO.receiver_stats["retry_requests"] == 3
    event(bridge, renderer, "pres")
    fresh = b"\x03\x00\x04\x00" * 100
    bridge.AUDIO.feed(fresh)
    assert bridge.AUDIO.ring.read_from(0)[1] == fresh


def test_discarded_partial_frames_cannot_shift_resumed_channels(bridge, monkeypatch):
    clock = Mock(return_value=100.0)
    monkeypatch.setattr(bridge.time, "monotonic", clock)
    renderer = bridge.Renderer("127.0.0.1")
    bridge.AUDIO.begin()
    bridge.AUDIO.describe("44100/S24_3LE/2")
    bridge.AUDIO.feed(b"\x11")  # Partial frame at the pause boundary.
    bridge.AUDIO.pause()
    clock.return_value += .75
    bridge.AUDIO.maintain(renderer)
    bridge.AUDIO.feed(b"\x22\x33")
    bridge.AUDIO.resume_playback()
    fresh_frame = b"\x03\x00\x00\x04\x00\x00"
    bridge.AUDIO.feed(b"\x44")
    bridge.AUDIO.feed(b"\x55\x66" + fresh_frame)
    assert bridge.AUDIO.ring.read_from(0)[1] == fresh_frame
    assert not bridge.AUDIO.pending


@pytest.mark.parametrize("offset", range(1, 6))
@pytest.mark.parametrize("paused_bytes", [0, 1, 5, 6, 7])
def test_consecutive_pauses_preserve_partial_frame_offset(bridge, offset, paused_bytes):
    renderer = Mock()
    audio = bridge.AUDIO
    audio.begin()
    audio.describe("44100/S24_3LE/2")
    # Distinct samples and channels expose a byte shift in the resumed output.
    source = (b"\x11\x22\x33\x44\x55\x66"
              b"\x77\x88\x99\xaa\xbb\xcc"
              b"\x03\x00\x00\x04\x00\x00"
              b"\xfd\xff\xff\xfc\xff\xff")
    audio.feed(source[:1])
    audio.pause(now=100)
    audio.maintain(renderer, now=100.75)
    audio.resume_playback(now=101)
    audio.feed(source[1:offset])  # Resume before the discarded frame is complete.
    assert not audio.pending
    assert audio.awaiting_pcm

    audio.pause(now=102)
    audio.maintain(renderer, now=102.75)
    audio.feed(source[offset:offset + paused_bytes])
    audio.resume_playback(now=103)
    consumed = offset + paused_bytes
    # Continue the same PCM input in byte-sized fragments across the frame edge.
    for byte in source[consumed:]:
        audio.feed(bytes([byte]))
    first_complete_frame = (consumed + 5) // 6 * 6
    expected = source[first_complete_frame:]
    assert audio.ring.read_from(0)[1] == expected
    assert audio.discarded_bytes == first_complete_frame
    assert audio.pcm_bytes == len(expected)
    assert not audio.pending
    assert not audio.discard_alignment
    assert not audio.awaiting_pcm


@pytest.mark.parametrize("method", ["serve_flac", "serve_wav"])
def test_pause_reconnects_at_fifteen_and_thirty_seconds_never_start_audio(bridge, playback, method):
    renderer, clock = playback
    event(bridge, renderer, "paus")
    handler = object.__new__(bridge.StreamHandler)
    handler.connection = Mock()
    handler.headers = {}
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    for delay in (0, 15, 30):
        clock.return_value = 100 + delay
        bridge.AUDIO.maintain(renderer)
        getattr(handler, method)()
        handler.send_response.assert_called_with(503)
        assert ("Retry-After", "1") in [call.args for call in handler.send_header.call_args_list]
        assert not bridge.CLIENTS
    assert bridge.AUDIO.rejected_reconnects == 3
    event(bridge, renderer, "pend")
    assert bridge.status_snapshot()["audio"]["state"] == "idle"


def test_pause_during_encoder_permit_acquisition_is_rechecked_before_registration(bridge, playback, monkeypatch):
    renderer, _ = playback
    permits = Mock()
    def acquire(**_):
        event(bridge, renderer, "paus")
        return True
    permits.acquire.side_effect = acquire
    monkeypatch.setattr(bridge, "ENCODER_SLOTS", permits)
    launch = Mock()
    monkeypatch.setattr(bridge.subprocess, "Popen", launch)
    handler = object.__new__(bridge.StreamHandler)
    handler.connection = Mock()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.serve_flac()
    launch.assert_not_called()
    handler.send_response.assert_called_with(503)
    permits.release.assert_called_once()
    assert not bridge.CLIENTS


@pytest.mark.parametrize("path", ["/stream.flac", "/stream.wav"])
def test_real_paused_http_response_is_empty_and_releases_connection_slot(bridge, playback, http_server, monkeypatch, path):
    renderer, clock = playback
    event(bridge, renderer, "paus")
    popen = Mock()
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    server = http_server(max_connections=1)
    for delay in (0, 15, 30):
        clock.return_value = 100 + delay
        bridge.AUDIO.maintain(renderer)
        peer = server.connect(f"GET {path} HTTP/1.0\r\n\r\n".encode())
        headers = response_headers(peer)
        assert headers.startswith(b"HTTP/1.0 503")
        assert b"Retry-After: 1\r\n" in headers
        assert b"Content-Length: 0\r\n" in headers
        assert headers.endswith(b"\r\n\r\n")
        assert peer.recv(1) == b""
        wait_until(lambda: server.active == 0)
    popen.assert_not_called()
    assert not bridge.CLIENTS


@pytest.mark.parametrize("method", ["serve_flac", "serve_wav"])
def test_sustained_pause_releases_existing_stream_resources(bridge, stream_probe, method):
    renderer = Mock()
    probe = stream_probe(method)
    client = probe.connected()
    bridge.AUDIO.pause(now=100)
    bridge.AUDIO.maintain(renderer, now=100.75)
    assert client.ring.closed
    probe.finished()
    assert not bridge.CLIENTS
    renderer.stop.assert_called_once_with(client.session_id, preserve_volume=True)


def test_old_ring_cleanup_cannot_cancel_resumed_ring(bridge, playback):
    renderer, clock = playback
    old_ring = bridge.AUDIO.ring
    event(bridge, renderer, "paus")
    clock.return_value += .75
    event(bridge, renderer, "pres")
    peer, connection = socket.socketpair()
    client = bridge.StreamClient(bridge.AUDIO.ring, connection)
    try:
        bridge.register_client(client)
        bridge.cancel_ring_clients(old_ring, "delayed cleanup")
        assert not client.cancelled.is_set()
    finally:
        client.interrupt()
        bridge.unregister_client(client)
        peer.close()
        connection.close()


@pytest.mark.parametrize("stage", ["SetAVTransportURI", "Play"])
def test_pause_racing_inflight_playback_does_not_play_until_resume(bridge, playback, stage):
    renderer, clock = playback
    bridge.STATE["pushed"] = None
    bridge.STATE["dirty"] = 99
    def action(name, *_):
        if name == stage:
            event(bridge, renderer, "paus")
            event(bridge, renderer, "prsm")
        return True
    renderer.action.side_effect = action
    renderer.dispatch_once()
    if stage == "Play":
        renderer.dispatch_once()
    assert not renderer.dispatch_once()
    clock.return_value += .372
    event(bridge, renderer, "pres")
    renderer.action.side_effect = None
    renderer.dispatch_once()
    assert actions(renderer).count("SetAVTransportURI") == 1
    assert actions(renderer).count("Play") == 1


@pytest.mark.parametrize("kind", ["initial", "resume"])
@pytest.mark.parametrize("resume_before_ack", [False, True])
def test_rejected_fetch_during_inflight_play_is_retried_on_brief_resume(
        bridge, playback, monkeypatch, kind, resume_before_ack):
    renderer, clock = playback
    monkeypatch.setattr(bridge, "DIDL_PUSH", False)
    if kind == "initial":
        bridge.STATE["pushed"] = None
        bridge.STATE["dirty"] = 99
        renderer.dispatch_once()  # Install the URI before the in-flight Play.
    else:
        renderer.resume(bridge.AUDIO.session_id)
    def play_action(name, *_):
        assert name == "Play"
        event(bridge, renderer, "paus")
        rejected_fetch(bridge)
        if resume_before_ack:
            clock.return_value += .372
            event(bridge, renderer, "pres")
        return True  # SOAP success despite the speaker's rejected HTTP fetch.
    renderer.action.side_effect = play_action
    renderer.dispatch_once()
    if kind == "initial":
        assert renderer.job[1] == "play"
        assert bridge.STATE["pushed"] is None
    if not resume_before_ack:
        assert not renderer.dispatch_once()
        clock.return_value += .372
        event(bridge, renderer, "pres")
    renderer.action.side_effect = None
    assert renderer.dispatch_once()  # No two-second gap or new PCM needed.
    expected = ["SetAVTransportURI", "Play", "Play"] if kind == "initial" else ["Play", "Play"]
    assert actions(renderer) == expected
    assert renderer.job is None
    assert renderer.resume_session is None
    assert not renderer.dispatch_once()


def test_fetch_rejected_after_play_ack_still_requeues_resume(bridge, playback):
    renderer, clock = playback
    event(bridge, renderer, "paus")
    rejected_fetch(bridge)
    clock.return_value += .234
    event(bridge, renderer, "pres")
    event(bridge, renderer, "prsm")  # Duplicate resume must not enqueue twice.
    assert renderer.dispatch_once()
    assert actions(renderer) == ["Play"]
    assert not renderer.dispatch_once()


@pytest.mark.parametrize("method", ["serve_flac", "serve_wav"])
def test_resume_before_rejection_headers_preserves_fetch_recovery(bridge, playback, method):
    renderer, clock = playback
    def play_action(name, *_):
        assert name == "Play"
        event(bridge, renderer, "paus")
        handler = object.__new__(bridge.StreamHandler)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        send_rejection = handler.reject_audio
        def resume_then_reject(retrying):
            clock.return_value += .234
            event(bridge, renderer, "pres")
            send_rejection(retrying)
        handler.reject_audio = resume_then_reject
        getattr(handler, method)()
        handler.send_response.assert_called_once_with(503)
        return True
    renderer.resume(bridge.AUDIO.session_id)
    renderer.action.side_effect = play_action
    renderer.dispatch_once()
    renderer.action.side_effect = None
    assert renderer.dispatch_once()
    assert actions(renderer) == ["Play", "Play"]
    assert not renderer.dispatch_once()


def test_rejected_fetch_recovery_does_not_cross_session_boundary(bridge, playback):
    renderer, clock = playback
    event(bridge, renderer, "paus")
    rejected_fetch(bridge)
    event(bridge, renderer, "pend")
    event(bridge, renderer, "pbeg")
    bridge.handle_output_description(b"48000/S16_LE/2", renderer)
    event(bridge, renderer, "pres")
    clock.return_value += 3
    while renderer.dispatch_once():
        pass
    assert actions(renderer) == ["Stop", "SetAVTransportURI", "Play"]


@pytest.mark.parametrize("resume_before_stop_ack", [False, True])
def test_pause_stop_preserves_latest_volume_queued_while_worker_busy(
        bridge, playback, resume_before_stop_ack):
    renderer, clock = playback
    bridge.STATE["pushed"] = None
    bridge.STATE["dirty"] = 99
    def busy_uri(name, *_):
        assert name == "SetAVTransportURI"
        event(bridge, renderer, "paus")
        for volume in (20, 45, 70):
            renderer.set_volume(volume)
        clock.return_value += .75
        bridge.AUDIO.maintain(renderer)
        return True
    renderer.action.side_effect = busy_uri
    renderer.dispatch_once()
    assert renderer.volume[1] == 70
    def stop_action(name, *_):
        assert name == "Stop"
        if resume_before_stop_ack:
            event(bridge, renderer, "pres")
            bridge.AUDIO.feed(b"\x03\x00\x04\x00" * 8192)
        return True
    renderer.action.side_effect = stop_action
    renderer.dispatch_once()
    renderer.action.side_effect = None
    if not resume_before_stop_ack:
        event(bridge, renderer, "pres")
        bridge.AUDIO.feed(b"\x03\x00\x04\x00" * 8192)
    clock.return_value += 1
    while renderer.dispatch_once():
        pass
    assert actions(renderer) == ["SetAVTransportURI", "Stop", "SetAVTransportURI", "Play", "SetVolume"]
    assert "<DesiredVolume>70</DesiredVolume>" in renderer.action.call_args.args[1]
    assert renderer.volume is None


@pytest.mark.parametrize("ending", ["pend", "rejection"])
def test_session_cleanup_still_discards_volume_after_pause_stop(bridge, playback, ending):
    renderer, clock = playback
    event(bridge, renderer, "paus")
    renderer.set_volume(70)
    clock.return_value += .75
    bridge.AUDIO.maintain(renderer)
    assert renderer.volume[1] == 70
    if ending == "pend":
        event(bridge, renderer, "pend")
    else:
        bridge.handle_output_description(b"48000/S16_LE/2", renderer)
    assert renderer.volume is None
    while renderer.dispatch_once():
        pass
    assert actions(renderer) == ["Stop"]


@pytest.mark.parametrize("success", [False, True])
def test_resume_racing_inflight_stop_reasserts_fresh_uri(bridge, playback, success):
    renderer, clock = playback
    event(bridge, renderer, "paus")
    clock.return_value += .75
    bridge.AUDIO.maintain(renderer)
    def stop_action(name, *_):
        assert name == "Stop"
        event(bridge, renderer, "pres")
        bridge.AUDIO.feed(b"\x03\x00\x04\x00" * 8192)
        return success
    renderer.action.side_effect = stop_action
    renderer.dispatch_once()
    renderer.action.side_effect = None
    clock.return_value += 1
    renderer.dispatch_once()
    renderer.dispatch_once()
    assert actions(renderer) == ["Stop", "SetAVTransportURI", "Play"]


def test_blocked_speaker_command_does_not_delay_pause_cleanup(bridge, playback):
    renderer, clock = playback
    entered, release = threading.Event(), threading.Event()
    bridge.STATE["pushed"] = None
    bridge.STATE["dirty"] = 99
    def blocked(*_):
        assert not bridge.AUDIO.lock._is_owned()
        entered.set()
        assert release.wait(3)
        return True
    renderer.action.side_effect = blocked
    worker = threading.Thread(target=renderer.dispatch_once)
    worker.start()
    try:
        assert entered.wait(3)
        old_ring = bridge.AUDIO.ring
        event(bridge, renderer, "paus")
        clock.return_value += .75
        bridge.AUDIO.maintain(renderer)
        assert old_ring.closed
        assert renderer.stop_revision == 1
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    renderer.action.side_effect = None
    renderer.dispatch_once()
    assert actions(renderer) == ["SetAVTransportURI", "Stop"]


def test_failed_pause_stop_is_bounded_and_never_reopens_old_stream(bridge, playback):
    renderer, clock = playback
    event(bridge, renderer, "paus")
    clock.return_value += .75
    bridge.AUDIO.maintain(renderer)
    renderer.action.return_value = False
    for _ in range(20):
        renderer.dispatch_once()
        clock.return_value += 1
    assert actions(renderer) == ["Stop"] * 4
    assert bridge.AUDIO.snapshot() is None
    assert bridge.AUDIO.ring.closed


def test_fresh_resume_reinstalls_uri_even_when_display_refresh_is_disabled(bridge, playback, monkeypatch):
    renderer, clock = playback
    monkeypatch.setattr(bridge, "DIDL_PUSH", False)
    event(bridge, renderer, "paus")
    clock.return_value += .75
    event(bridge, renderer, "pres")
    renderer.dispatch_once()
    bridge.AUDIO.feed(b"\x03\x00\x04\x00" * 8192)
    clock.return_value += 1
    renderer.dispatch_once()
    renderer.dispatch_once()
    assert actions(renderer) == ["Stop", "SetAVTransportURI", "Play"]


@pytest.mark.parametrize("ending", ["pend", "eof"])
def test_end_preempts_pending_pause_and_cannot_stop_replacement_session(bridge, playback, ending):
    renderer, clock = playback
    old_session = bridge.AUDIO.session_id
    event(bridge, renderer, "paus")
    if ending == "pend":
        event(bridge, renderer, "pend")
    else:
        bridge.handle_audio_eof(old_session, renderer)
    assert bridge.AUDIO.pause_deadline is None
    event(bridge, renderer, "pbeg")
    bridge.handle_output_description(b"48000/S16_LE/2", renderer)
    clock.return_value += 5
    bridge.AUDIO.maintain(renderer)
    renderer.dispatch_once()
    renderer.dispatch_once()
    renderer.dispatch_once()
    assert actions(renderer) == ["Stop", "SetAVTransportURI", "Play"]


def test_stall_diagnostics_are_bounded_and_saved_before_session_reset(bridge, playback, capsys):
    renderer, clock = playback
    bridge.AUDIO.receiver_stats = {"missing_audio_blocks": 1, "too_late_audio_blocks": 2, "retry_requests": 3}
    capsys.readouterr()
    clock.return_value += 2.1
    for _ in range(100):
        bridge.AUDIO.maintain(renderer)
    output = capsys.readouterr().out
    assert output.count("event=pcm_stall ") == 1
    assert output.count("reason=periodic") == 1
    clock.return_value += 1
    bridge.AUDIO.feed(b"\x01\x00\x02\x00")
    assert "event=pcm_recovered" in capsys.readouterr().out
    event(bridge, renderer, "paus")
    clock.return_value += 30
    bridge.AUDIO.maintain(renderer)
    event(bridge, renderer, "pend")
    output = capsys.readouterr().out
    assert "pcm_stall" not in output
    assert "reason=session_end" in output
    assert "missing=1 too_late=2 retries=3" in output
    assert bridge.AUDIO.receiver_stats is None


def test_overrun_diagnostics_aggregate_across_playback_generations(bridge, playback, capsys):
    renderer, clock = playback
    ring = bridge.AUDIO.ring
    ring.capacity = 16
    capsys.readouterr()
    for _ in range(100):
        ring.write(b"\x00" * 32)
        ring.read_from(0)
    assert capsys.readouterr().out == ""
    event(bridge, renderer, "paus")
    assert "ring_overruns=100" in capsys.readouterr().out
    clock.return_value += .75
    event(bridge, renderer, "pres")
    event(bridge, renderer, "pend")
    output = capsys.readouterr().out
    assert "reason=session_end" in output
    assert "ring_overruns=100" in output


@pytest.mark.parametrize("method", ["serve_flac", "serve_wav"])
def test_real_resumed_stream_contains_only_fresh_audio(bridge, stream_probe, method):
    renderer = Mock()
    bridge.AUDIO.feed(struct.pack("<hh", -12000, 12000) * 44100)
    bridge.AUDIO.pause(now=100)
    bridge.AUDIO.maintain(renderer, now=100.75)
    bridge.AUDIO.resume_playback(now=101)
    fresh_frame = struct.pack("<hh", 1234, -4321)
    bridge.AUDIO.feed(fresh_frame * (44100 * 4))
    probe = stream_probe(method)
    client = probe.connected()
    if method == "serve_wav":
        assert probe.receive(44).startswith(b"RIFF")
        assert probe.receive(4096) == fresh_frame * 1024
    else:
        # Keep live input arriving beyond the 1.5-second join backlog; the
        # production encoder may still be probing its raw input at this point.
        bridge.AUDIO.feed(fresh_frame * (44100 * 6))
        encoded = bytearray()
        decoded = b""
        for _ in range(20):
            encoded.extend(probe.peer.recv(8192))
            result = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "pipe:1"],
                                    input=encoded, capture_output=True, timeout=3)
            decoded = result.stdout
            if len(decoded) >= 4096:
                break
        assert len(decoded) >= 4096
        assert decoded == fresh_frame * (len(decoded) // 4)
    bridge.cancel_ring_clients(client.ring, "test complete")
    probe.finished()
