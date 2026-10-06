"""Artwork metadata must not control HTTP headers or disrupt audio."""
import hashlib
import http.client
import plistlib
import threading
from contextlib import closing
from unittest.mock import Mock

import pytest

from test_metadata_dispatch import item


PNG = b'\x89PNG\r\n\x1a\nexample PNG cover'
JPEG = b'\xff\xd8\xffexample JPEG cover'
INJECTED_MIME = 'image/jpeg\r\nX-Injected: yes'


@pytest.fixture
def artwork_http(bridge):
    server = bridge.ThreadingHTTPServer(('127.0.0.1', 0), bridge.StreamHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def get(data):
        path = f'/art-{hashlib.sha256(data).hexdigest()}.jpg'
        with closing(http.client.HTTPConnection(*server.server_address, timeout=3)) as connection:
            connection.request('GET', path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()

    yield get
    server.shutdown()
    server.server_close()
    worker.join(3)


def ingest(bridge, monkeypatch, code, artwork, declared_mime=INJECTED_MIME):
    """Exercise the metadata reader, then stop before its session-ending EOF."""
    if code == 'copl':
        artwork = plistlib.dumps({
            'kMRMediaRemoteNowPlayingInfoArtworkData': artwork,
            'kMRMediaRemoteNowPlayingInfoArtworkMIMEType': declared_mime,
        })
    renderer = Mock()
    payloads = [item(code, artwork), item('pvol', b'-15,0,0,0')]

    class Finished(BaseException):
        pass

    class Reader:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, _):
            if payloads:
                return payloads.pop(0)
            raise Finished()

    with monkeypatch.context() as metadata:
        metadata.setattr(bridge, 'open', lambda *_args, **_kwargs: Reader(), raising=False)
        metadata.setattr(bridge.threading, 'Thread', Mock())
        with pytest.raises(Finished):
            bridge.metadata_reader(renderer)
    renderer.set_volume.assert_called_once()
    renderer.stop.assert_not_called()


@pytest.mark.parametrize('code', ['copl', 'PICT'])
@pytest.mark.parametrize('image,mime', [(PNG, 'image/png'), (JPEG, 'image/jpeg')])
def test_metadata_ignores_injected_mime_for_current_and_cached_covers(
        bridge, monkeypatch, artwork_http, code, image, mime):
    ingest(bridge, monkeypatch, code, image)
    assert bridge.ART['mime'] == mime
    # The first request serves the current cover. The second serves it from cache.
    for current in (True, False):
        if not current:
            replacement = JPEG if image == PNG else PNG
            ingest(bridge, monkeypatch, code, replacement)
        status, headers, body = artwork_http(image)
        assert status == 200
        assert headers['Content-Type'] == mime
        assert 'X-Injected' not in headers
        assert body == image


@pytest.mark.parametrize('code', ['copl', 'PICT'])
@pytest.mark.parametrize('unsupported', [
    b'not an image', b'GIF89aunsupported GIF', b'<svg>unsupported SVG</svg>',
    b'\x89PNG\r\n', b'\xff\xd8',
])
def test_unsupported_metadata_preserves_cover_cache_and_valid_audio(
        bridge, monkeypatch, artwork_http, code, unsupported):
    bridge.handle_playback_metadata('pbeg', b'', Mock(), {})
    bridge.handle_playback_metadata('odsc', b'44100/S16_LE/2', Mock(), {})
    ingest(bridge, monkeypatch, code, PNG)
    old_art, old_cache = dict(bridge.ART), dict(bridge.ART_CACHE)
    old_url, old_revision = bridge.NOW_PLAYING['artwork'], bridge.STATE['revision']
    ring = bridge.AUDIO.snapshot()
    position = ring.session_start
    audio = b'\x01\x00\x02\x00' * 16
    bridge.AUDIO.feed(audio)

    ingest(bridge, monkeypatch, code, unsupported)

    assert bridge.ART == old_art
    assert bridge.ART_CACHE == old_cache
    assert bridge.NOW_PLAYING['artwork'] == old_url
    assert bridge.STATE['revision'] == old_revision
    ingest(bridge, monkeypatch, code, PNG)
    assert bridge.STATE['revision'] == old_revision
    assert bridge.ART_CACHE == old_cache
    assert bridge.AUDIO.snapshot() is ring
    assert ring.read_from(position)[1] == audio
    bridge.AUDIO.feed(audio)
    assert bridge.AUDIO.pcm_bytes == len(audio) * 2
    assert artwork_http(PNG)[0] == 200
    status, headers, _body = artwork_http(unsupported)
    assert status == 404
    assert 'X-Injected' not in headers


@pytest.mark.parametrize('declared_mime', [
    INJECTED_MIME, 'image/png\nX-Injected: yes', 'text/html', '', 42, b'image/png',
])
def test_declared_mime_never_admits_unsupported_artwork(
        bridge, monkeypatch, artwork_http, declared_mime):
    ingest(bridge, monkeypatch, 'copl', b'unsupported', declared_mime)
    assert bridge.ART['id'] is None
    assert not bridge.ART_CACHE
    assert bridge.NOW_PLAYING['artwork'] == ''
    assert artwork_http(b'unsupported')[0] == 404


@pytest.mark.parametrize('image,mime', [(PNG, 'image/png'), (JPEG, 'image/jpeg')])
def test_http_derives_type_even_when_cached_mime_is_unsafe(bridge, artwork_http, image, mime):
    art_id = hashlib.sha256(image).hexdigest()
    bridge.ART_CACHE[art_id] = (image, INJECTED_MIME)
    status, headers, body = artwork_http(image)
    assert status == 200
    assert headers['Content-Type'] == mime
    assert 'X-Injected' not in headers
    assert body == image


def test_http_rejects_unsupported_entry_even_with_image_mime(bridge, artwork_http):
    image = b'not an image'
    bridge.ART_CACHE[hashlib.sha256(image).hexdigest()] = (image, 'image/png')
    assert artwork_http(image)[0] == 404
