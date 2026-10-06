"""Track updates preserve absent fields and remain safe for ICY stream readers."""
import base64
import http.client
import plistlib
import random
import threading
from unittest.mock import Mock

import pytest


def copl(bridge, **fields):
    bridge.handle_copl(plistlib.dumps({f"kMRMediaRemoteNowPlayingInfo{key.title()}": value
                                     for key, value in fields.items()}))


def item(typ, code, data=b""):
    encoded = b'\n<data encoding="base64">\n' + base64.b64encode(data) + b'</data>' if data else b''
    return (b'<item><type>' + typ.encode().hex().encode() + b'</type><code>' + code.encode().hex().encode()
            + b'</code><length>' + str(len(data)).encode() + b'</length>' + encoded + b'</item>')


class Finished(BaseException):
    pass


def ingest(bridge, monkeypatch, streams, renderer=None):
    """Exercise the real pipe reader, including optional EOF and reconnects."""
    class Reader:
        def __init__(self, payload, eof):
            self.payload, self.eof = payload, eof
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, size):
            if self.payload:
                data, self.payload = self.payload[:size], self.payload[size:]
                return data
            if self.eof:
                return b""
            raise Finished()

    readers = iter(Reader(payload, eof) for payload, eof in streams)
    def open_reader(*_, **__):
        try:
            return next(readers)
        except StopIteration:
            raise Finished()

    monkeypatch.setattr(bridge, "open", open_reader, raising=False)
    monkeypatch.setattr(bridge.threading, "Thread", Mock())
    with pytest.raises(Finished):
        bridge.metadata_reader(renderer or Mock())


@pytest.mark.parametrize("fields,expected", [
    ({"title": "Next"}, {"title": "Next", "artist": "Artist", "album": "Album"}),
    ({"artist": "Other"}, {"title": "Title", "artist": "Other", "album": "Album"}),
    ({"album": "Other"}, {"title": "Title", "artist": "Artist", "album": "Other"}),
    ({"title": "", "artist": "", "album": ""}, {"title": "", "artist": "", "album": ""}),
])
def test_airplay_two_partial_updates_through_pipe(bridge, monkeypatch, fields, expected):
    copl(bridge, title="Title", artist="Artist", album="Album")
    revision = bridge.STATE["revision"]
    data = plistlib.dumps({f"kMRMediaRemoteNowPlayingInfo{key.title()}": value for key, value in fields.items()})
    ingest(bridge, monkeypatch, [(item("ssnc", "copl", data), False)])
    assert {key: bridge.NOW_PLAYING[key] for key in expected} == expected
    assert bridge.STATE["revision"] == revision + 1


@pytest.mark.parametrize("fields", [{}, {"title": 42}, {"artist": ["Wrong"]}, {"title": "Title", "album": "Album"}])
def test_airplay_two_unchanged_or_invalid_fields_do_not_refresh(bridge, fields):
    copl(bridge, title="Title", artist="Artist", album="Album")
    revision = bridge.STATE["revision"]
    copl(bridge, **fields)
    assert bridge.NOW_PLAYING == dict(title="Title", artist="Artist", album="Album", artwork="")
    assert bridge.STATE["revision"] == revision


@pytest.mark.parametrize("field", ["title", "artist", "album"])
def test_partial_updates_preserve_existing_field_when_replacement_is_oversize(bridge, field):
    copl(bridge, title="Title", artist="Artist", album="Album")
    revision = bridge.STATE["revision"]
    copl(bridge, **{field: "🎵" * (bridge.META_TEXT_MAX // 4 + 1)})
    assert bridge.NOW_PLAYING == dict(title="Title", artist="Artist", album="Album", artwork="")
    assert bridge.STATE["revision"] == revision


def test_classic_decode_expansion_cannot_bypass_track_field_limit(bridge, monkeypatch):
    copl(bridge, title="Title", artist="Artist", album="Album")
    payload = (item("ssnc", "mdst") + item("core", "asar", b"\xff" * bridge.META_TEXT_MAX)
               + item("core", "asal", b"New album") + item("ssnc", "mden"))
    ingest(bridge, monkeypatch, [(payload, False)])
    assert bridge.NOW_PLAYING == dict(title="Title", artist="Artist", album="New album", artwork="")


def test_classic_partial_transactions_and_explicit_clears(bridge, monkeypatch):
    copl(bridge, title="Title", artist="Artist", album="Album")
    payload = (item("ssnc", "mdst") + item("core", "asal", b"Other album") + item("ssnc", "mden")
               + item("ssnc", "mdst") + item("core", "minm", b"Next") + item("ssnc", "mden")
               + item("ssnc", "mdst") + item("core", "asar") + item("ssnc", "mden"))
    revision = bridge.STATE["revision"]
    ingest(bridge, monkeypatch, [(payload, False)])
    assert bridge.NOW_PLAYING == dict(title="Next", artist="", album="Other album", artwork="")
    assert bridge.STATE["revision"] == revision + 3


@pytest.mark.parametrize("boundary", ["mdst", "mden"])
def test_classic_pending_fields_do_not_cross_transactions(bridge, monkeypatch, boundary):
    copl(bridge, title="Title", artist="Artist", album="Album")
    payload = (item("ssnc", "mdst") + item("core", "minm", b"First") + item("ssnc", boundary)
               + item("core", "asal", b"New album") + item("ssnc", "mden"))
    revision = bridge.STATE["revision"]
    ingest(bridge, monkeypatch, [(payload, False)])
    assert bridge.NOW_PLAYING["title"] == ("Title" if boundary == "mdst" else "First")
    assert bridge.NOW_PLAYING["artist"] == "Artist"
    assert bridge.NOW_PLAYING["album"] == "New album"
    assert bridge.STATE["revision"] == revision + (1 if boundary == "mdst" else 2)


def test_classic_does_not_publish_before_mden(bridge, monkeypatch):
    copl(bridge, title="Title", artist="Artist", album="Album")
    revision = bridge.STATE["revision"]
    ingest(bridge, monkeypatch, [(item("ssnc", "mdst") + item("core", "minm", b"Pending"), False)])
    assert bridge.NOW_PLAYING["title"] == "Title"
    assert bridge.STATE["revision"] == revision


@pytest.mark.parametrize("boundary", ["pend", "pbeg", "eof"])
def test_new_sessions_cannot_borrow_pending_or_previous_fields(bridge, monkeypatch, boundary):
    prefix = (item("ssnc", "pbeg") + item("ssnc", "mdst") + item("core", "minm", b"Old")
              + item("core", "asar", b"Old artist") + item("core", "asal", b"Old album")
              + item("ssnc", "mden") + item("ssnc", "mdst") + item("core", "asar", b"Unfinished"))
    suffix = item("core", "minm", b"New") + item("ssnc", "mden")
    if boundary == "eof":
        streams = [(prefix, True), (suffix, False)]
    else:
        if boundary == "pbeg":
            prefix = prefix.replace(item("ssnc", "pbeg"), b"", 1)
            copl(bridge, title="Old", artist="Old artist", album="Old album")
        streams = [(prefix + item("ssnc", boundary) + suffix, False)]
    ingest(bridge, monkeypatch, streams)
    assert bridge.NOW_PLAYING == dict(title="New", artist="", album="", artwork="")


@pytest.mark.parametrize("ending", ["eof", "error"])
def test_metadata_pipe_loss_clears_current_and_pending_metadata(bridge, monkeypatch, ending):
    class Reader:
        read_count = 0
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, _):
            self.read_count += 1
            if self.read_count == 1:
                return (item("ssnc", "pbeg") + item("ssnc", "mdst")
                        + item("core", "minm", b"Old title") + item("ssnc", "mden")
                        + item("ssnc", "PICT", b"\xff\xd8\xffold cover")
                        + item("ssnc", "mdst") + item("core", "asar", b"Unfinished"))
            if ending == "error":
                raise OSError("pipe lost")
            return b""

    second = Mock()
    second.__enter__ = Mock(return_value=second)
    second.__exit__ = Mock(return_value=False)
    second.read.side_effect = [item("core", "minm", b"New title") + item("ssnc", "mden"), Finished()]
    calls = [Reader(), second]
    def reopen(*_, **__):
        if len(calls) == 1:
            assert bridge.NOW_PLAYING == dict(title="", artist="", album="", artwork="")
            assert bridge.ART["bytes"] == b""
            assert bridge.ART["id"] is None
            assert not bridge.STATE["active"]
        return calls.pop(0)

    monkeypatch.setattr(bridge, "open", reopen, raising=False)
    monkeypatch.setattr(bridge.threading, "Thread", Mock())
    monkeypatch.setattr(bridge.time, "sleep", Mock())
    renderer = Mock()
    with pytest.raises(Finished):
        bridge.metadata_reader(renderer)
    assert bridge.NOW_PLAYING == dict(title="New title", artist="", album="", artwork="")
    renderer.stop.assert_called_once()


def decoded_block(bridge, last=None):
    block, current = bridge.StreamHandler.icy_block(last)
    assert len(block) == 1 + block[0] * 16
    return block[1:].rstrip(b"\x00").decode("utf-8"), current, block


@pytest.mark.parametrize("char", ["a", "é", "🎵"])
def test_icy_long_multibyte_title_fits_one_byte_length(bridge, char):
    bridge.NOW_PLAYING.update(title=char * 10000, artist="Artist", artwork="http://example.invalid/art.jpg")
    text, current, block = decoded_block(bridge)
    assert 0 < block[0] <= 255
    assert len(block) <= 4081
    assert text.startswith("StreamTitle='Artist - ")
    assert text.endswith("';")
    assert "�" not in text
    assert bridge.StreamHandler.icy_block(current) == (b"\x00", current)


def test_icy_quotes_controls_and_url_cannot_add_fields(bridge):
    bridge.NOW_PLAYING.update(title="First\r\nSecond';Injected='bad\\\x00\x7f\x85", artist="O'Neil",
                             artwork="http://example.invalid/a';Injected='url")
    text, _, _ = decoded_block(bridge)
    assert text.count("='") == 2
    assert text.count("';") == 2
    assert "First  Second" in text
    assert "O’Neil" in text
    assert "\\" not in text
    assert all(ord(char) >= 32 and not 127 <= ord(char) <= 159 for char in text)


def test_icy_normal_title_and_artwork_keep_original_format(bridge):
    bridge.NOW_PLAYING.update(title="Title", artist="Artist", artwork="http://example.invalid/art.jpg")
    text, _, _ = decoded_block(bridge)
    assert text == "StreamTitle='Artist - Title';StreamUrl='http://example.invalid/art.jpg';"


def test_icy_oversize_artwork_url_is_omitted_instead_of_truncated(bridge):
    bridge.NOW_PLAYING.update(title="Title", artwork="http://example.invalid/" + "x" * 10000)
    text, _, _ = decoded_block(bridge)
    assert text == "StreamTitle='Title';"


def test_icy_explicit_title_clear_clears_client_display_once(bridge):
    copl(bridge, title="Title", artist="Artist")
    _, last, _ = decoded_block(bridge)
    copl(bridge, title="")
    text, cleared, _ = decoded_block(bridge, last)
    assert text == "StreamTitle='';"
    assert bridge.StreamHandler.icy_block(cleared) == (b"\x00", cleared)


@pytest.mark.integration
def test_live_flac_stream_sends_bounded_utf8_metadata_and_continues_audio(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "ICY_META_INT", 1024)
    bridge.AUDIO.begin()
    bridge.AUDIO.describe("44100/S16_LE/2")
    copl(bridge, title="🎵" * 1024, artist="O'Neil\nArtist")
    server = bridge.ThreadingHTTPServer(("127.0.0.1", 0), bridge.StreamHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=15)
    response = None
    stop = threading.Event()
    feeder = None
    def feed():
        source = random.Random(19)
        while not stop.is_set():
            bridge.AUDIO.feed(source.randbytes(4096))
            stop.wait(.005)
    try:
        connection.request("GET", "/stream.flac", headers={"Icy-MetaData": "1"})
        response = connection.getresponse()
        assert response.status == 200
        interval = int(response.getheader("icy-metaint"))
        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        assert response.read(interval).startswith(b"fLaC")
        length = response.read(1)[0]
        assert 0 < length <= 255
        text = response.read(length * 16).rstrip(b"\x00").decode("utf-8")
        assert text.startswith("StreamTitle='O’Neil Artist - ")
        assert text.endswith("';")
        assert "�" not in text
        assert len(response.read(interval)) == interval
        assert response.read(1) == b"\x00"
    finally:
        stop.set()
        if feeder:
            feeder.join(3)
        if response:
            response.close()
        connection.close()
        bridge.cancel_all_clients("test complete")
        bridge.AUDIO.end("test complete", drain=False)
        server.shutdown()
        server.server_close()
        worker.join(3)


def test_album_only_update_refreshes_renderer_without_changing_audio_session(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "DIDL_PUSH", True)
    renderer = bridge.Renderer("127.0.0.1")
    renderer.action = Mock(return_value=True)
    bridge.handle_playback_metadata("pbeg", b"", renderer, {})
    bridge.handle_playback_metadata("odsc", b"44100/S16_LE/2", renderer, {})
    bridge.AUDIO.feed(b"\x00" * 64)
    ring = bridge.AUDIO.snapshot()
    copl(bridge, title="Title", artist="Artist", album="Album")
    bridge.STATE["dirty"] -= 10
    assert renderer.dispatch_once()
    assert renderer.dispatch_once()
    copl(bridge, album="New album")
    bridge.STATE["dirty"] -= 10
    assert renderer.dispatch_once()
    assert renderer.dispatch_once()
    assert bridge.STATE["pushed"][:3] == ("Title", "Artist", "New album")
    assert bridge.AUDIO.snapshot() is ring
    assert not ring.closed
    assert [call.args[0] for call in renderer.action.call_args_list] == ["SetAVTransportURI", "Play"] * 2
