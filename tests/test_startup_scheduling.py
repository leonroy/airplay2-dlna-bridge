"""Drive real metadata events with a clock; no worker sleeps or live renderer."""
import plistlib
import threading
from unittest.mock import Mock

import pytest


@pytest.fixture
def startup(bridge, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(bridge, "STARTUP_SETTLE", 0.5)
    monkeypatch.setattr(bridge, "cancel_session_clients", lambda *_: None)
    renderer = bridge.Renderer("127.0.0.1")
    renderer.action = Mock(return_value=True)

    def event(code, data=b"", at=None):
        if at is not None:
            now[0] = at
        assert bridge.handle_playback_metadata(code, data, renderer, {})

    def metadata(title=None, artwork=None, at=None):
        if at is not None:
            now[0] = at
        fields = {}
        if title is not None:
            fields["kMRMediaRemoteNowPlayingInfoTitle"] = title
        if artwork is not None:
            fields["kMRMediaRemoteNowPlayingInfoArtworkData"] = artwork
        bridge.handle_copl(plistlib.dumps(fields))

    def step(at):
        now[0] = at
        return renderer.dispatch_once()

    return renderer, event, metadata, step


def names(renderer):
    return [call.args[0] for call in renderer.action.call_args_list]


@pytest.mark.parametrize("order", [("pbeg", "odsc"), ("odsc", "pbeg")])
def test_initial_start_needs_pbeg_and_valid_description(bridge, startup, order):
    renderer, event, metadata, step = startup
    for code in order:
        event(code, b"44100/S16_LE/2" if code == "odsc" else b"")
        metadata("Title")
        if code == order[0]:
            assert not step(103)
        else:
            assert not step(103.49)
            assert step(103.5)
            assert step(103.55)
    assert names(renderer) == ["SetAVTransportURI", "Play"]


def test_rejected_description_does_not_start(bridge, startup):
    renderer, event, metadata, step = startup
    event("pbeg")
    event("odsc", b"48000/S32_LE/8")
    metadata("Title")
    assert step(103)  # Stop the rejected session, never install a URI.
    assert not step(104)
    assert names(renderer) == ["Stop"]


def test_late_title_does_not_send_fast_placeholder(bridge, startup):
    renderer, event, metadata, step = startup
    event("pbeg")
    event("odsc", b"44100/S16_LE/2")
    assert not step(100.5)
    metadata("Late title", at=101)
    assert not step(101.49)
    assert step(101.5)
    assert step(101.55)
    assert names(renderer) == ["SetAVTransportURI", "Play"]
    assert "Late title" in renderer.action.call_args_list[0].args[1]


def test_titleless_start_keeps_existing_fallback(bridge, startup):
    renderer, event, metadata, step = startup
    event("pbeg")
    event("odsc", b"44100/S16_LE/2")
    metadata(artwork=b"cover", at=100.25)
    assert not step(102.24)
    assert step(102.25)
    assert step(102.3)
    assert names(renderer) == ["SetAVTransportURI", "Play"]
    assert "AirPlay 2" in renderer.action.call_args_list[0].args[1]


def test_title_and_art_inside_quiet_period_coalesce_one_uri(bridge, startup):
    renderer, event, metadata, step = startup
    event("pbeg")
    event("odsc", b"44100/S16_LE/2")
    metadata("Early title", at=100.1)
    metadata("Final title", at=100.4)
    metadata(artwork=b"late cover", at=100.7)
    assert not step(101.19)
    assert step(101.2)
    assert step(101.25)
    assert not step(110)
    assert names(renderer) == ["SetAVTransportURI", "Play"]
    args = renderer.action.call_args_list[0].args[1]
    assert "Final title" in args and "Early title" not in args
    assert "albumArtURI" in args


def test_identical_metadata_does_not_postpone_start(bridge, startup):
    renderer, event, metadata, step = startup
    event("pbeg")
    event("odsc", b"44100/S16_LE/2")
    metadata("Title", artwork=b"cover", at=100.1)
    metadata("Title", artwork=b"cover", at=100.4)
    assert step(100.61)
    assert step(100.66)
    assert names(renderer) == ["SetAVTransportURI", "Play"]


@pytest.mark.parametrize("didl", [False, True])
def test_late_art_after_success_keeps_mid_play_debounce(bridge, startup, monkeypatch, didl):
    renderer, event, metadata, step = startup
    monkeypatch.setattr(bridge, "DIDL_PUSH", didl)
    event("pbeg")
    event("odsc", b"44100/S16_LE/2")
    metadata("Title", at=100.1)
    assert step(100.61)
    assert step(100.66)
    metadata(artwork=b"late cover", at=100.8)
    assert not step(101.3)
    assert step(102.8) is didl
    assert step(102.85) is didl
    assert names(renderer) == ["SetAVTransportURI", "Play"] * (2 if didl else 1)


def test_default_still_waits_two_seconds(bridge, startup, monkeypatch):
    renderer, event, metadata, step = startup
    monkeypatch.setattr(bridge, "STARTUP_SETTLE", 2.0)
    event("pbeg")
    event("odsc", b"44100/S16_LE/2")
    metadata("Title", at=100.1)
    assert not step(100.6)
    assert not step(102.09)
    assert step(102.11)
    assert step(102.16)
    assert names(renderer) == ["SetAVTransportURI", "Play"]


def test_update_during_uri_cancels_stale_play_and_coalesces_retry(bridge, startup):
    renderer, event, metadata, step = startup
    event("pbeg")
    event("odsc", b"44100/S16_LE/2")
    metadata("Title", at=100.1)
    entered, release = threading.Event(), threading.Event()

    def action(*_):
        entered.set()
        assert release.wait(2)
        return True

    renderer.action.side_effect = action
    worker = threading.Thread(target=lambda: step(100.61))
    worker.start()
    try:
        assert entered.wait(1)
        metadata(artwork=b"cover", at=100.7)
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    renderer.action.side_effect = None
    assert not step(101.19)
    assert step(101.2)
    assert step(101.25)
    assert names(renderer) == ["SetAVTransportURI", "SetAVTransportURI", "Play"]
    assert bridge.STATE["pushed"][3] == bridge.ART["id"]


def test_failed_initial_uri_respects_backoff_and_new_session(bridge, startup):
    renderer, event, metadata, step = startup
    event("pbeg")
    event("odsc", b"44100/S16_LE/2")
    metadata("Old title", at=100.1)
    renderer.action.return_value = False
    assert step(100.61)
    assert not step(100.8)
    event("pend", at=100.81)
    event("pbeg", at=100.82)
    event("odsc", b"48000/S32_LE/2")
    metadata("New title", at=100.83)
    renderer.action.return_value = True
    assert step(100.84)  # Old Stop retains priority.
    assert not step(101.32)
    assert step(101.34)
    assert step(101.39)
    assert names(renderer) == ["SetAVTransportURI", "Stop", "SetAVTransportURI", "Play"]
    assert bridge.STATE["pushed"][0] == "New title"


def test_command_diagnostics_have_duration_result_and_no_payload(bridge, startup, monkeypatch):
    renderer, event, metadata, step = startup
    log = Mock()
    monkeypatch.setattr(bridge, "log", log)
    event("pbeg")
    event("odsc", b"44100/S16_LE/2")
    metadata("Private title", at=100.1)
    renderer.action.return_value = False
    step(100.61)
    step(100.9)
    records = [call.args[0] for call in log.call_args_list if call.args[0].startswith("timing ")]
    assert len([r for r in records if "event=command_start" in r]) == 2
    ends = [r for r in records if "event=command_end" in r]
    assert len(ends) == 2
    assert "attempt=1 result=failure duration_ms=" in ends[0]
    assert "attempt=2 result=failure duration_ms=" in ends[1]
    assert all("session=1 revision=" in r and "Private title" not in r for r in records)


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 8192])
def test_flac_frame_probe_skips_metadata_and_logs_once(bridge, chunk_size):
    # Metadata contains a false frame sync; the real sync spans arbitrary reads.
    data = b"fLaC" + b"\0\0\0\4" + b"\xff\xf8xx" + b"\x80\0\0\3" + b"art" + b"\xff\xf9audio"
    probe = bridge.FlacFrameProbe()
    found = []
    for start in range(0, len(data), chunk_size):
        found.append(probe.feed(data[start:start + chunk_size]))
        assert len(probe.header) <= 4
    assert found.count(True) == 1
    assert not probe.feed(b"\xff\xf8another-frame")


@pytest.mark.parametrize("data", [b"not flac", b"fLaC\x80\0\0\0invalid"])
def test_invalid_flac_does_not_claim_frame_emission(bridge, data):
    probe = bridge.FlacFrameProbe()
    assert not probe.feed(data)
    assert probe.phase == "invalid"
