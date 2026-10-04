"""Playback ordering, pipe boundaries, HTTP gating and diagnostic logs."""
import base64
import os
import threading
from unittest.mock import Mock

import pytest

from audio_helpers import samples


@pytest.mark.parametrize("order", ("description_first", "begin_first"))
def test_session_waits_for_both_begin_and_description(bridge, order):
    audio = bridge.AudioStream()
    first, second = ((lambda: audio.describe("48000/S24_3LE/2"), audio.begin)
                     if order == "description_first" else
                     (audio.begin, lambda: audio.describe("48000/S24_3LE/2")))
    first()
    assert audio.snapshot() is None
    second()
    assert audio.snapshot().pcm == bridge.PCMFormat(48000, "S24_3LE", 2)


def test_session_change_closes_old_readers_and_forgets_format(bridge):
    audio = bridge.AudioStream()
    audio.begin()
    audio.describe("44100/S16_LE/2")
    audio.feed(b"old!" * 8)
    old = audio.snapshot()
    audio.end()
    with pytest.raises(bridge.StreamEnded):
        old.read_from(0)
    audio.begin()
    assert audio.snapshot() is None
    source, expected = samples("S24_3LE", frames=32)
    audio.feed(source)
    audio.describe("48000/S24_3LE/2")
    _, received = audio.snapshot().read_from(0)
    assert received == expected


def test_ended_session_tail_is_not_buffered_for_next_format(bridge):
    audio = bridge.AudioStream()
    audio.begin()
    audio.describe("44100/S16_LE/2")
    audio.end()
    audio.feed(b"old-tail")  # Eight bytes: not aligned to a six-byte S24 frame.
    assert not audio.pending
    audio.describe("48000/S24_3LE/2")
    audio.begin()
    source, expected = samples("S24_3LE", frames=8)
    audio.feed(source)
    assert audio.snapshot().read_from(0)[1] == expected
    assert not audio.pending


@pytest.mark.parametrize("metadata_before_eof_read", (False, True))
def test_old_audio_eof_cleanup_preserves_new_metadata_session(bridge, monkeypatch, metadata_before_eof_read, audio_pipe):
    class Finished(BaseException):
        pass

    wiim, pending, next_ring = Mock(), {}, []
    bridge.handle_playback_metadata("pbeg", b"", wiim, pending)
    bridge.handle_playback_metadata("odsc", b"44100/S16_LE/2", wiim, pending)
    pipe = audio_pipe()
    pipe.write(b"old!")
    pipe.close_writer()
    reader = os.fdopen(os.dup(pipe.read_fd), "rb", buffering=0)
    context = Mock()
    context.__enter__ = Mock(return_value=reader)

    def next_session():
        bridge.handle_playback_metadata("pend", b"", wiim, pending)
        bridge.handle_playback_metadata("odsc", b"48000/S24_3LE/2", wiim, pending)
        bridge.handle_playback_metadata("pbeg", b"", wiim, pending)
        next_ring.append(bridge.AUDIO.snapshot())

    real_select, selects = bridge.select.select, []

    def select_audio(*args):
        selects.append(True)
        if metadata_before_eof_read and len(selects) == 2:
            next_session()
        return real_select(*args)

    def exit_context(*args):
        reader.close()
        if not metadata_before_eof_read:
            next_session()  # EOF read, but cleanup has not yet run.
        return False

    context.__exit__ = Mock(side_effect=exit_context)
    monkeypatch.setattr(bridge, "open", Mock(side_effect=[context, Finished()]), raising=False)
    monkeypatch.setattr(bridge.threading, "Thread", Mock())
    monkeypatch.setattr(bridge.select, "select", select_audio)
    with pytest.raises(Finished):
        bridge.audio_reader(wiim)
    assert bridge.AUDIO.snapshot() is next_ring[0]
    assert not next_ring[0].closed
    assert bridge.STATE["active"]


@pytest.mark.parametrize("order", (("pbeg", "odsc"), ("odsc", "pbeg")))
def test_persistent_audio_writer_is_drained_at_pend_without_eof(bridge, order, audio_pipe):
    pipe = audio_pipe(bridge.AUDIO)
    audio = bridge.AUDIO
    wiim, pending = Mock(), {}
    audio.attach_audio(pipe.read_fd)
    bridge.handle_playback_metadata("pbeg", b"", wiim, pending)
    bridge.handle_playback_metadata("odsc", b"44100/S16_LE/2", wiim, pending)
    pipe.write(b"old!")
    audio.read_audio(pipe.read_fd)
    old_ring = audio.snapshot()
    pipe.write(b"old-tail")
    bridge.handle_playback_metadata("pend", b"", wiim, pending)
    assert old_ring.closed
    assert audio.read_audio(pipe.read_fd)[0] is None  # Empty, writer still open.
    for code in order:
        bridge.handle_playback_metadata(code, b"48000/S24_3LE/2" if code == "odsc" else b"",
                                        wiim, pending)
    source, expected = samples("S24_3LE", frames=8)
    pipe.write(source)
    audio.read_audio(pipe.read_fd)
    assert audio.snapshot().read_from(0)[1] == expected
    assert not audio.pending  # In particular, no two-byte old-format offset.


def test_queued_tail_is_drained_when_reader_attaches_after_new_metadata(bridge, audio_pipe):
    audio = bridge.AudioStream()
    pipe = audio_pipe(audio)
    audio.begin()
    audio.describe("44100/S16_LE/2")
    pipe.write(b"old-tail")
    audio.end()  # No registered reader yet.
    audio.describe("48000/S24_3LE/2")
    audio.begin()
    audio.attach_audio(pipe.read_fd)
    assert audio.read_audio(pipe.read_fd)[0] is None
    source, expected = samples("S24_3LE", frames=8)
    pipe.write(source)
    audio.read_audio(pipe.read_fd)
    assert audio.snapshot().read_from(0)[1] == expected
    assert not audio.pending


def test_completed_old_eof_does_not_drain_new_writers_first_audio(bridge, audio_pipe):
    audio = bridge.AudioStream()
    old_pipe = audio_pipe(audio)
    audio.attach_audio(old_pipe.read_fd)
    audio.begin()
    audio.describe("44100/S16_LE/2")
    old_pipe.write(b"old!")
    audio.read_audio(old_pipe.read_fd)
    old_pipe.close_writer()
    assert audio.read_audio(old_pipe.read_fd)[0] == b""
    old_pipe.close()
    audio.end()
    audio.describe("48000/S24_3LE/2")
    audio.begin()
    new_pipe = audio_pipe(audio)
    source, expected = samples("S24_3LE", frames=8)
    new_pipe.write(source)  # New data is queued before reader attachment.
    audio.attach_audio(new_pipe.read_fd)
    audio.read_audio(new_pipe.read_fd)
    assert audio.snapshot().read_from(0)[1] == expected


def test_pend_cannot_interleave_between_audio_read_and_feed(bridge, monkeypatch, audio_pipe):
    audio = bridge.AudioStream()
    pipe = audio_pipe(audio)
    read_fd = pipe.read_fd
    entered, release, end_requested, ended = (threading.Event() for _ in range(4))
    failures, paused = [], []
    real_read = os.read

    def pause_read(fd, size):
        if fd == read_fd and not paused:
            paused.append(True)
            entered.set()
            assert release.wait(2)
        return real_read(fd, size)

    def read():
        try:
            audio.read_audio(read_fd)
        except BaseException as error:
            failures.append(error)

    def end():
        end_requested.set()
        try:
            audio.end()
            ended.set()
        except BaseException as error:
            failures.append(error)

    reader, ender = threading.Thread(target=read), threading.Thread(target=end)
    try:
        audio.attach_audio(read_fd)
        audio.begin()
        audio.describe("44100/S16_LE/2")
        pipe.write(b"old-tail")
        monkeypatch.setattr(bridge.os, "read", pause_read)
        reader.start()
        assert entered.wait(1)
        ender.start()
        assert end_requested.wait(1)
        assert not ended.wait(0.05)
        release.set()
        reader.join(2)
        ender.join(2)
        assert ended.is_set() and not failures
        audio.describe("48000/S24_3LE/2")
        audio.begin()
        source, expected = samples("S24_3LE", frames=8)
        pipe.write(source)
        audio.read_audio(read_fd)
        assert audio.snapshot().read_from(0)[1] == expected
        assert not audio.pending
    finally:
        release.set()
        if reader.ident is not None:
            reader.join(2)
        if ender.ident is not None:
            ender.join(2)


def test_ring_close_wakes_a_blocked_reader(bridge):
    ring = bridge.Ring(bridge.PCMFormat(48000, "S24_3LE", 2))
    closed = threading.Event()

    def read():
        try:
            ring.read_from(0, timeout=30)
        except bridge.StreamEnded:
            closed.set()

    worker = threading.Thread(target=read)
    worker.start()
    ring.close()
    assert closed.wait(1)
    worker.join(1)


def test_format_change_without_session_boundary_rejects_audio(bridge):
    audio = bridge.AudioStream()
    audio.begin()
    audio.describe("44100/S16_LE/2")
    old = audio.snapshot()
    with pytest.raises(ValueError, match="format changed"):
        audio.describe("48000/S24_3LE/2")
    assert audio.snapshot() is None
    assert old.closed
    audio.feed(b"discard")
    assert not audio.pending
    audio.end()
    audio.begin()
    audio.describe("48000/S24_3LE/2")
    assert audio.snapshot() is not None


def test_duplicate_description_keeps_current_audio(bridge):
    audio = bridge.AudioStream()
    audio.begin()
    assert audio.describe("48000/S24_3LE/2")
    audio.feed(b"\x01\x02\x03" * 2)
    ring = audio.snapshot()
    assert not audio.describe("48000/S24_3LE/2")
    assert audio.snapshot() is ring
    assert ring.end() == 6


def test_missing_description_has_bounded_storage(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "PENDING_MAX", 12)
    audio = bridge.AudioStream()
    audio.begin()
    audio.feed(b"x" * 13)
    assert audio.failed
    assert not audio.pending
    assert audio.snapshot() is None


def test_bad_metadata_stops_playback_and_does_not_fall_back(bridge):
    wiim = Mock()
    bridge.AUDIO.begin()
    bridge.handle_output_description(b"48000/F24/2", wiim)
    wiim.stop.assert_called_once()
    assert bridge.AUDIO.failed
    assert bridge.AUDIO.snapshot() is None


def test_logs_correlate_early_audio_description_and_format_change(bridge, capsys):
    audio = bridge.AudioStream()
    audio.feed(b"abcd")
    audio.describe("44100/S16_LE/2")
    audio.begin()
    with pytest.raises(ValueError):
        audio.describe("48000/S24_3LE/2")
    audio.feed(b"discard")
    audio.end()
    audio.begin()
    output = capsys.readouterr().out
    assert "audio waiting for odsc" in output
    assert "buffered_before_odsc=4" in output
    assert "odsc_timing=" in output and "after first audio" in output
    assert "pbeg received; format ready; session=1" in output
    assert "44100/S16_LE/2 -> 48000/S24_3LE/2 without pend/pbeg boundary" in output
    assert "discarded_bytes=7" in output
    assert "pbeg received; waiting for odsc; session=2" in output


def test_waiting_logs_are_periodic_not_per_audio_chunk(bridge, monkeypatch, capsys):
    now = [0.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: now[0])
    audio = bridge.AudioStream()
    audio.begin()
    for _ in range(100):
        audio.feed(b"abcd")
    now[0] = 5.0
    audio.feed(b"abcd")
    output = capsys.readouterr().out
    assert output.count("audio waiting for odsc") == 2
    assert output.count("first audio received") == 1
    assert "pending_bytes=404" in output


def test_malformed_description_logs_are_escaped_and_bounded(bridge, capsys):
    wiim = Mock()
    payload = b"bad\n" + b"x" * 1000
    bridge.handle_output_description(payload, wiim)
    output = capsys.readouterr().out
    assert "bad\\n" in output
    assert "length=1004" in output
    assert "x" * 97 not in output
    assert "odsc rejected" in output and "stopping renderer" in output
    wiim.stop.assert_called_once()


def test_fragmented_metadata_pipe_logs_source_output_and_session_order(bridge, monkeypatch, capsys):
    class Finished(BaseException):
        pass

    def item(code, data=b""):
        encoded = b"\n<data encoding=\"base64\">\n" + base64.b64encode(data) + b"</data>" if data else b""
        return (b"<item><type>73736e63</type><code>" + code.encode().hex().encode()
                + b"</code><length>" + str(len(data)).encode() + b"</length>" + encoded + b"</item>")

    payload = (item("pbeg") + item("sdsc", b"AAC/48000/F24/2")
               + item("odsc", b"48000/S24_3LE/2") + item("pend"))
    reader = Mock()
    reader.read.side_effect = [payload[:53], payload[53:157], payload[157:], b""]
    context = Mock()
    context.__enter__ = Mock(return_value=reader)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(bridge, "open", Mock(side_effect=[context, Finished()]), raising=False)
    monkeypatch.setattr(bridge.threading, "Thread", Mock())
    wiim = Mock()
    with pytest.raises(Finished):
        bridge.metadata_reader(wiim)
    output = capsys.readouterr().out
    assert "seq=2 code=sdsc" in output and "AAC/48000/F24/2" in output
    assert "seq=3 code=odsc" in output and "odsc accepted source=48000/S24_3LE/2" in output
    assert "seq=4 code=pend" in output
    assert "metadata pipe EOF unparsed_bytes=0" in output
    wiim.stop.assert_called_once()


@pytest.mark.parametrize("format_state", ("unknown", "known", "rejected"))
@pytest.mark.parametrize("active_client", (False, True))
def test_audio_resume_requires_a_valid_description(bridge, monkeypatch, format_state, active_client):
    class Finished(BaseException):
        pass

    monkeypatch.setattr(bridge, "log", Mock())
    bridge.AUDIO.begin()
    bridge.STATE["active"] = True
    if format_state != "unknown":
        bridge.AUDIO.describe("44100/S16_LE/2")
    if format_state == "rejected":
        bridge.AUDIO.fail("test rejection")
    reader = Mock()
    reader.fileno.return_value = 123
    monkeypatch.setattr(bridge.AUDIO, "attach_audio", Mock())
    monkeypatch.setattr(bridge.AUDIO, "detach_audio", Mock())
    clock = Mock(return_value=10.0)
    reads = iter((b"abcd", b"efgh"))
    def read_audio(_):
        try:
            data = next(reads)
        except StopIteration:
            raise Finished()
        clock.return_value += 3
        return data, bridge.AUDIO.session_id
    monkeypatch.setattr(bridge.AUDIO, "read_audio", read_audio)
    monkeypatch.setattr(bridge.select, "select", Mock())
    context = Mock()
    context.__enter__ = Mock(return_value=reader)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(bridge, "open", Mock(return_value=context), raising=False)
    monkeypatch.setattr(bridge.time, "monotonic", clock)
    monkeypatch.setattr(bridge, "has_active_clients", Mock(return_value=active_client))
    worker = Mock()
    monkeypatch.setattr(bridge.threading, "Thread", worker)
    wiim = Mock()
    with pytest.raises(Finished):
        bridge.audio_reader(wiim)
    if format_state == "known" and not active_client:
        wiim.resume.assert_called_once_with(bridge.AUDIO.session_id)
    else:
        wiim.resume.assert_not_called()
    worker.assert_not_called()


@pytest.mark.parametrize("order", (("pbeg", "odsc"), ("odsc", "pbeg")))
def test_actual_playback_events_allow_each_order_and_clear_between_sessions(bridge, order):
    wiim, pending = Mock(), {"minm": "previous track"}
    for description in (b"44100/S16_LE/2", b"48000/S24_3LE/2"):
        for code in order:
            assert bridge.handle_playback_metadata(code, description if code == "odsc" else b"",
                                                   wiim, pending)
        assert bridge.STATE["active"]
        assert not pending
        assert bridge.AUDIO.snapshot().pcm == bridge.PCMFormat.from_description(description)
        ring = bridge.AUDIO.snapshot()
        bridge.handle_playback_metadata("pend", b"", wiim, pending)
        assert ring.closed
        assert not bridge.STATE["active"]
        assert bridge.AUDIO.snapshot() is None
    assert wiim.stop.call_count == 2


@pytest.mark.parametrize("method", ("serve_wav", "serve_flac"))
def test_http_rejects_audio_before_format_is_known(bridge, method):
    handler = object.__new__(bridge.StreamHandler)
    handler.send_error = Mock()
    getattr(handler, method)()
    assert handler.send_error.call_args.args[0] == 503
