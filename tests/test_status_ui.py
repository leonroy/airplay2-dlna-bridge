"""Status and logs must remain bounded and independent of playback resources."""
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import plistlib
import socket
import struct
import threading
import time
from unittest.mock import Mock

import pytest


def eventually(predicate, timeout=3):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


@pytest.fixture
def http_server(bridge):
    server = bridge.ThreadingHTTPServer(('127.0.0.1', 0), bridge.StreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connections = []

    def get(path):
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        connections.append(connection)
        connection.request('GET', path)
        return connection, connection.getresponse()

    yield get
    for connection in connections:
        connection.close()
    eventually(lambda: bridge.VIEWER_COUNT == 0)
    server.shutdown()
    server.server_close()
    thread.join(3)


def event(response):
    kind = None
    while True:
        line = response.readline().decode()
        assert line
        if line.startswith('event: '):
            kind = line.strip()[7:]
        if line.startswith('data: '):
            return kind, json.loads(line[6:])


def close_stream(connection, response):
    # HTTP/1.0 relinquishes the socket to the response after getresponse().
    response.close()
    connection.close()


def test_status_tracks_real_session_without_network_or_encoder(bridge, monkeypatch):
    network = Mock(side_effect=AssertionError('status requested device I/O'))
    encoder = Mock(side_effect=AssertionError('status started an encoder'))
    monkeypatch.setattr(bridge, 'http_req', network)
    monkeypatch.setattr(bridge.subprocess, 'Popen', encoder)
    assert bridge.status_snapshot()['audio']['state'] == 'idle'
    bridge.AUDIO.begin()
    assert bridge.status_snapshot()['audio']['state'] == 'waiting'
    bridge.AUDIO.describe('44100/S16_LE/2')
    bridge.AUDIO.feed(b'\x00' * 1024)
    bridge.NOW_PLAYING.update(title='<script>alert(1)</script>', artist='Example Artist')
    value = bridge.status_snapshot()
    assert value['audio']['state'] == 'receiving'
    assert value['audio']['format'] == dict(rate=44100, bits=16, channels=2)
    assert value['audio']['buffered_bytes'] == 1024
    assert value['track']['title'] == '<script>alert(1)</script>'
    assert value['connections'] == 0
    assert not bridge.CLIENTS
    bridge.AUDIO.fail('sample failure')
    assert bridge.status_snapshot()['audio']['state'] == 'error'
    bridge.AUDIO.end(drain=False)
    assert bridge.status_snapshot()['track']['title'] == ''
    assert bridge.status_snapshot()['audio']['state'] == 'idle'


@pytest.mark.parametrize("sample_format,bits", [("S8", 16), ("S16_LE", 16), ("S24_LE", 24), ("S32_LE", 32)])
def test_output_stream_describes_flac_encoder_and_clears_at_end(bridge, sample_format, bits):
    assert bridge.status_snapshot()["audio"]["output_stream"] is None
    bridge.AUDIO.describe(f"48000/{sample_format}/2")
    audio = bridge.status_snapshot()["audio"]
    assert audio["output_stream"] == dict(codec="FLAC", rate=48000, bits=bits, channels=2)
    command = bridge.AUDIO.pcm.encoder_command()
    assert int(command[command.index("-bits_per_raw_sample") + 1]) == bits
    bridge.AUDIO.end(drain=False)
    assert bridge.status_snapshot()["audio"]["output_stream"] is None


def test_status_artwork_is_local_and_empty_when_session_ends(bridge):
    bridge.AUDIO.begin()
    bridge.update_artwork(b'\x89PNG\r\n\x1a\nsample')
    bridge.NOW_PLAYING['artwork'] = 'http://example.invalid/private.jpg'
    assert bridge.status_snapshot()['track']['artwork'] == f"/art-{hashlib.sha256(b'\x89PNG\r\n\x1a\nsample').hexdigest()}.jpg"
    bridge.AUDIO.end(drain=False)
    assert bridge.status_snapshot()['track']['artwork'] is None


def test_cached_artwork_urls_return_their_own_image_and_mime(http_server, bridge):
    older, current = hashlib.sha256(b'\x89PNG\r\n\x1a\nolder cover').hexdigest(), hashlib.sha256(b'\xff\xd8\xffcurrent cover').hexdigest()
    bridge.cache_art(older, b'\x89PNG\r\n\x1a\nolder cover', 'image/png')
    bridge.cache_art(current, b'\xff\xd8\xffcurrent cover', 'image/jpeg')
    bridge.ART.update(id=current, bytes=b'\xff\xd8\xffcurrent cover', mime='image/jpeg')
    for art_id, image, mime in [(older, b'\x89PNG\r\n\x1a\nolder cover', 'image/png'),
                                (current, b'\xff\xd8\xffcurrent cover', 'image/jpeg')]:
        connection, response = http_server(f'/art-{art_id}.jpg')
        assert response.status == 200
        assert response.getheader('Content-Type') == mime
        assert int(response.getheader('Content-Length')) == len(image)
        assert response.read() == image
        connection.close()


def test_evicted_artwork_url_does_not_return_current_cover(http_server, bridge):
    bridge.update_artwork(b'\x89PNG\r\n\x1a\nevicted cover')
    expired = bridge.NOW_PLAYING['artwork']
    expired_id = bridge.ART['id']
    for index in range(bridge.ART_CACHE_MAX):
        bridge.update_artwork(b'\x89PNG\r\n\x1a\n' + f'current cover {index}'.encode())
    assert expired_id not in bridge.ART_CACHE
    assert bridge.ART['id'] in bridge.ART_CACHE
    assert len(bridge.ART_CACHE) == bridge.ART_CACHE_MAX
    connection, response = http_server(expired)
    assert response.status == 404
    assert b'\xff\xd8\xffcurrent cover' not in response.read()
    connection.close()


def test_previous_instance_artwork_url_does_not_return_new_cover(http_server, bridge, monkeypatch):
    # Load two actual instances with clocks that collided under the old counter.
    def load(second):
        spec = importlib.util.spec_from_file_location('restart_bridge', Path(bridge.__file__))
        module = importlib.util.module_from_spec(spec)
        with monkeypatch.context() as clock:
            clock.setattr(time, 'time', lambda: second)
            clock.setattr(time, 'time_ns', lambda: second * 1_000_000_000)
            spec.loader.exec_module(module)
        return module

    def ingest(module, data):
        module.handle_copl(plistlib.dumps({'kMRMediaRemoteNowPlayingInfoArtworkData': data}))

    old, new = load(1000), load(1001)
    ingest(old, b'\x89PNG\r\n\x1a\nfirst old cover')
    ingest(old, b'\x89PNG\r\n\x1a\nprevious instance cover')
    ingest(new, b'\x89PNG\r\n\x1a\nnew instance cover')
    old_id, new_id = old.ART['id'], new.ART['id']
    assert old_id != new_id
    # The existing test server now serves the new instance's real artwork cache.
    monkeypatch.setattr(bridge, 'ART_CACHE', new.ART_CACHE)
    connection, response = http_server(f'/art-{old_id}.jpg')
    assert response.status == 404
    assert b'\x89PNG\r\n\x1a\nnew instance cover' not in response.read()
    connection.close()
    connection, response = http_server(f'/art-{new_id}.jpg')
    assert response.status == 200
    assert response.read() == b'\x89PNG\r\n\x1a\nnew instance cover'
    connection.close()
    ingest(new, b'\x89PNG\r\n\x1a\nprevious instance cover')
    assert new.ART['id'] == old_id
    connection, response = http_server(f'/art-{old_id}.jpg')
    assert response.status == 200
    assert response.read() == b'\x89PNG\r\n\x1a\nprevious instance cover'
    connection.close()


@pytest.mark.parametrize('path', [
    '/art-' + 'f' * 64 + '.jpg', '/art-invalid.jpg', '/art-100.jpg',
    '/art-' + hashlib.sha256(b'\xff\xd8\xffcurrent cover').hexdigest() + '.jpg/extra',
])
def test_unknown_artwork_urls_do_not_return_current_cover(http_server, bridge, path):
    bridge.update_artwork(b'\xff\xd8\xffcurrent cover')
    connection, response = http_server(path)
    assert response.status == 404
    assert b'\xff\xd8\xffcurrent cover' not in response.read()
    connection.close()


@pytest.mark.parametrize('code,declared_mime', [('PICT', None), ('copl', None), ('copl', 'wrong')])
@pytest.mark.parametrize('images', [
    [(b'\x89PNG\r\n\x1a\nfirst', 'image/png'), (b'\xff\xd8\xffsecond', 'image/jpeg')],
    [(b'\xff\xd8\xfffirst', 'image/jpeg'), (b'\x89PNG\r\n\x1a\nsecond', 'image/png')],
])
def test_ingested_artwork_has_its_own_mime_over_http(http_server, bridge, monkeypatch, code, declared_mime, images):
    from test_metadata_dispatch import item

    payloads = []
    for image, mime in images:
        if code == 'PICT':
            data = image
        else:
            fields = {'kMRMediaRemoteNowPlayingInfoArtworkData': image}
            if declared_mime == 'wrong':
                # Incorrect metadata must not override the actual image format.
                fields['kMRMediaRemoteNowPlayingInfoArtworkMIMEType'] = 'image/jpeg' if mime == 'image/png' else 'image/png'
            data = plistlib.dumps(fields)
        payloads.append(item(code, data))

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
            bridge.metadata_reader(Mock())
    for image, mime in images:
        art_id = hashlib.sha256(image).hexdigest()
        connection, response = http_server(f'/art-{art_id}.jpg')
        assert response.status == 200
        assert response.getheader('Content-Type') == mime
        assert response.read() == image
        connection.close()


def test_duplicate_artwork_does_not_dirty_metadata_and_reused_cover_stays_cached(bridge):
    bridge.update_artwork(b'\x89PNG\r\n\x1a\nreused cover')
    revision, first_id = bridge.STATE['revision'], bridge.ART['id']
    assert not bridge.update_artwork(b'\x89PNG\r\n\x1a\nreused cover')
    assert bridge.STATE['revision'] == revision
    bridge.update_artwork(b'\x89PNG\r\n\x1a\nother cover')
    bridge.update_artwork(b'\x89PNG\r\n\x1a\nreused cover')
    assert bridge.ART['id'] == first_id
    for index in range(bridge.ART_CACHE_MAX - 1):
        bridge.update_artwork(b'\x89PNG\r\n\x1a\n' + f'next cover {index}'.encode())
    assert first_id in bridge.ART_CACHE
    assert hashlib.sha256(b'\x89PNG\r\n\x1a\nother cover').hexdigest() not in bridge.ART_CACHE


def test_log_history_bounds_bytes_entries_and_reports_gap(bridge):
    history = bridge.LogHistory(max_entries=3, max_bytes=10)
    for line in ['aaaa', 'bbbb', 'cccc', 'dddd']:
        history.append(line)
    assert history.bytes <= 10
    records, gap = history.read(0)
    assert records == [(3, 'cccc'), (4, 'dddd')]
    assert gap
    assert history.read(None)[1] is False


def test_log_unicode_truncation_and_bounded_batch(bridge):
    history = bridge.LogHistory()
    for _ in range(30):
        history.append('🎵' * 2000)
    records, _ = history.read(None)
    assert len(records) == 16
    assert all(len(line.encode()) <= 4096 for _, line in records)
    assert all('\ufffd' not in line for _, line in records)


def test_log_still_prints_and_reaches_history(bridge, capsys):
    bridge.log('sample message')
    stdout = capsys.readouterr().out
    assert stdout.endswith('sample message\n')
    assert bridge.LOG_HISTORY.read(None)[0][0][1] == stdout.strip()


@pytest.mark.parametrize('path,content_type', [('/', 'text/html'), ('/app.css', 'text/css'),
    ('/app.js', 'text/javascript'), ('/demo.svg', 'image/svg+xml'), ('/placeholder.svg', 'image/svg+xml'),
    ('/favicon.svg', 'image/svg+xml')])
def test_page_assets_are_served_without_audio_clients(http_server, bridge, path, content_type):
    connection, response = http_server(path)
    assert response.status == 200
    assert response.getheader('Content-Type').startswith(content_type)
    assert response.read()
    connection.close()
    assert not bridge.CLIENTS


@pytest.mark.parametrize('path', ['/../server.py', '/server.py', '/web/../../server.py', '/.env',
    '/favicon-16.png/../../server.py', '/%2e%2e/server.py', '/icon-999.png', '/favicon-16.png', '/icon-1024.png'])
def test_unlisted_paths_do_not_serve_files(http_server, path):
    connection, response = http_server(path)
    assert response.status == 404
    response.read()
    connection.close()


def test_browser_and_phone_icons_have_valid_formats(http_server):
    for path, size in [('/apple-touch-icon.png', 180), ('/icon-192.png', 192), ('/icon-512.png', 512)]:
        connection, response = http_server(path)
        assert response.status == 200
        assert response.getheader('Content-Type') == 'image/png'
        body = response.read()
        assert body.startswith(b'\x89PNG\r\n\x1a\n')
        assert struct.unpack('>II', body[16:24]) == (size, size)
        connection.close()
    connection, response = http_server('/favicon.ico')
    assert response.getheader('Content-Type') == 'image/vnd.microsoft.icon'
    body = response.read()
    reserved, kind, count = struct.unpack('<HHH', body[:6])
    assert (reserved, kind, count) == (0, 1, 6)
    for index in range(count):
        width, height, _, _, planes, bits, length, offset = struct.unpack('<BBBBHHII', body[6 + index * 16:22 + index * 16])
        image = body[offset:offset + length]
        assert len(image) == length
        if image.startswith(b'\x89PNG\r\n\x1a\n'):
            assert struct.unpack('>II', image[16:24]) == (width or 256, height or 256)
        else:
            # ICO can contain a Windows bitmap with both pixels and a mask.
            header, bitmap_width, bitmap_height, bitmap_planes, bitmap_bits = struct.unpack('<IiiHH', image[:16])
            assert header >= 40
            assert (bitmap_width, bitmap_height) == (width or 256, (height or 256) * 2)
            assert (bitmap_planes, bitmap_bits) == (1, 32)
        assert (planes, bits) == (1, 32)
    connection.close()


def test_manifest_references_phone_icons(http_server):
    connection, response = http_server('/site.webmanifest')
    assert response.status == 200
    assert response.getheader('Content-Type').startswith('application/manifest+json')
    manifest = json.loads(response.read())
    assert manifest['start_url'] == '/'
    assert [(icon['src'], icon['sizes']) for icon in manifest['icons']] == [
        ('/icon-192.png', '192x192'), ('/icon-512.png', '512x512')]
    connection.close()


def test_snapshot_route_does_not_enable_observation(http_server, bridge):
    connection, response = http_server('/api/status')
    assert response.status == 200
    assert json.loads(response.read())['audio']['state'] == 'idle'
    assert bridge.VIEWER_COUNT == 0
    assert not bridge.LOG_HISTORY.read(None)[0]
    connection.close()


def test_sse_initial_status_logs_and_disconnect_release_viewer(http_server, bridge):
    bridge.log('sample log line')
    connection, response = http_server('/api/events?logs=1')
    assert response.getheader('Content-Type').startswith('text/event-stream')
    kind, status = event(response)
    assert kind == 'status'
    assert status['audio']['state'] == 'idle'
    assert bridge.VIEWER_COUNT == 1
    assert not bridge.CLIENTS
    kind, entry = event(response)
    assert kind == 'log' and entry['line'].endswith('sample log line')
    close_stream(connection, response)
    eventually(lambda: bridge.VIEWER_COUNT == 0)


def test_sse_status_continues_without_log_subscription(http_server, bridge):
    bridge.log('hidden log line')
    connection, response = http_server('/api/events')
    assert event(response)[0] == 'status'
    assert event(response)[0] == 'status'
    close_stream(connection, response)


def test_sse_connections_are_capped(http_server, bridge):
    streams = [http_server('/api/events') for _ in range(bridge.MAX_VIEWERS)]
    for _, response in streams:
        assert event(response)[0] == 'status'
    connection, response = http_server('/api/events')
    assert response.status == 503
    response.read(); connection.close()
    for connection, response in streams:
        close_stream(connection, response)
    eventually(lambda: bridge.VIEWER_COUNT == 0)


def soap(name, field, value, service='AVTransport'):
    return (f'<u:{name}Response xmlns:u="urn:schemas-upnp-org:service:{service}:1">'
            f'<{field}>{value}</{field}></u:{name}Response>').encode()


def observer(bridge):
    instance = bridge.RecipientObserver('192.0.2.10')
    instance.controls = {'AVTransport': 'http://192.0.2.10/control',
                         'RenderingControl': 'http://192.0.2.10/volume'}
    return instance


def test_observer_uses_only_read_actions_and_reserves_volume_budget(bridge, monkeypatch):
    instance = observer(bridge)
    calls = []

    def request(url, **kwargs):
        assert not bridge.AUDIO.lock._is_owned()
        envelope = bridge.ElementTree.fromstring(kwargs['data'])
        soap_namespace = 'http://schemas.xmlsoap.org/soap/envelope/'
        assert envelope.tag == f'{{{soap_namespace}}}Envelope'
        assert envelope.attrib[f'{{{soap_namespace}}}encodingStyle'] == 'http://schemas.xmlsoap.org/soap/encoding/'
        calls.append(kwargs)
        if url.endswith('/volume'):
            return soap('GetVolume', 'CurrentVolume', '38', 'RenderingControl')
        return soap('GetTransportInfo', 'CurrentTransportState', 'PLAYING')

    monkeypatch.setattr(bridge, 'http_req', request)
    instance.observe_once()
    value = instance.snapshot()
    assert value['playback']['value'] == 'PLAYING'
    assert value['volume']['value'] == 38
    assert calls[1]['deadline'] - calls[0]['deadline'] == 1
    assert all(call['max_bytes'] == bridge.SOAP_BODY_MAX for call in calls)
    assert 'GetTransportInfo' in calls[0]['headers']['SOAPACTION']
    assert 'GetVolume' in calls[1]['headers']['SOAPACTION']


def test_playback_timeout_leaves_time_for_volume_request(bridge, monkeypatch):
    instance = observer(bridge)
    now = [100.0]
    monkeypatch.setattr(bridge.time, 'monotonic', lambda: now[0])
    calls = []

    def request(url, **kwargs):
        calls.append(url)
        assert kwargs['deadline'] > now[0]
        if url.endswith('/control'):
            now[0] = kwargs['deadline']
            raise TimeoutError('playback timed out')
        now[0] += .1
        return soap('GetVolume', 'CurrentVolume', '38', 'RenderingControl')

    monkeypatch.setattr(bridge, 'http_req', request)
    instance.observe_once()
    assert len(calls) == 2 and calls[1].endswith('/volume')
    assert instance.snapshot()['playback']['error'] == 'connection'
    assert instance.snapshot()['volume']['value'] == 38
    assert instance.snapshot()['volume']['error'] is None
    assert now[0] <= 102


@pytest.mark.parametrize('raw,error', [
    (b'not XML', 'connection'), (b'<unexpected/>', 'connection'),
    (soap('GetTransportInfo', 'CurrentTransportState', 'UNKNOWN'), 'unavailable'),
    (b'<s:Fault xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><errorCode>401</errorCode></s:Fault>', 'unavailable')])
def test_observer_reports_invalid_or_unsupported_playback_independently(bridge, monkeypatch, raw, error):
    instance = observer(bridge)
    monkeypatch.setattr(bridge, 'http_req', lambda url, **kwargs:
        soap('GetVolume', 'CurrentVolume', '38', 'RenderingControl') if url.endswith('/volume') else raw)
    instance.observe_once()
    value = instance.snapshot()
    assert value['playback']['error'] == error
    assert value['volume']['value'] == 38
    assert value['volume']['error'] is None


def test_failed_observation_preserves_last_value_with_error_and_staleness(bridge, monkeypatch):
    instance = observer(bridge)
    instance.fields['playback'] = dict(value='PLAYING', at=time.time() - 20, error=None)
    monkeypatch.setattr(bridge, 'http_req', Mock(side_effect=TimeoutError()))
    instance.observe_once()
    assert instance.snapshot()['playback']['value'] == 'PLAYING'
    assert instance.snapshot()['playback']['stale']
    assert instance.snapshot()['playback']['error'] == 'connection'
    assert not instance.controls


@pytest.mark.parametrize('control', ['/control', 'http://192.0.2.10:49152/control'])
def test_observer_discovers_description_and_reads_state(bridge, monkeypatch, control):
    instance = bridge.RecipientObserver('192.0.2.10')
    calls = []

    def request(url, **kwargs):
        calls.append(kwargs)
        if url.endswith('/description.xml'):
            return (f'<root xmlns="urn:schemas-upnp-org:device-1-0"><device><friendlyName> Living Room Speaker </friendlyName><service>'
                    f'<serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>'
                    f'<controlURL>{control}</controlURL></service></device></root>').encode()
        return soap('GetTransportInfo', 'CurrentTransportState', 'PLAYING')

    monkeypatch.setattr(bridge, 'http_req', request)
    result = instance.read('GetTransportInfo', '', 'AVTransport', 'CurrentTransportState', time.monotonic() + 2)
    assert result == 'PLAYING'
    assert instance.snapshot()['name'] == 'Living Room Speaker'
    assert instance.controls['AVTransport'] == 'http://192.0.2.10:49152/control'
    assert calls[0]['deadline'] == calls[1]['deadline']


def test_observer_never_follows_description_to_another_device(bridge, monkeypatch):
    instance = bridge.RecipientObserver('192.0.2.10')
    request = Mock(return_value=b'<root><service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType><controlURL>http://192.0.2.11:49152/control</controlURL></service></root>')
    monkeypatch.setattr(bridge, 'http_req', request)
    with pytest.raises(ValueError, match='changed the target'):
        instance.read('GetTransportInfo', '', 'AVTransport', 'CurrentTransportState', time.monotonic() + 2)
    assert request.call_count == 1


def test_fixed_recipient_port_applies_to_commands_and_observations(monkeypatch, request):
    monkeypatch.setenv('RENDERER_PORT', '8080')
    bridge = request.getfixturevalue('bridge')
    calls = []

    def request(url, **kwargs):
        calls.append(url)
        if url.endswith('/description.xml'):
            return b'<root><service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType><controlURL>/control</controlURL></service></root>'
        return soap('GetTransportInfo', 'CurrentTransportState', 'STOPPED')

    monkeypatch.setattr(bridge, 'http_req', request)
    renderer = bridge.Renderer('192.0.2.10')
    assert renderer.resolve('AVTransport', time.monotonic() + 2) == 'http://192.0.2.10:49152/control'
    instance = bridge.RecipientObserver('192.0.2.10')
    assert instance.read('GetTransportInfo', '', 'AVTransport', 'CurrentTransportState', time.monotonic() + 2) == 'STOPPED'
    assert all(url.startswith('http://192.0.2.10:49152/') for url in calls)


def test_observer_sleeps_without_viewers_and_shares_work_across_viewers(bridge):
    instance = observer(bridge)
    instance.observe_once = Mock()
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    try:
        time.sleep(.05)
        instance.observe_once.assert_not_called()
        with bridge.VIEWERS:
            bridge.VIEWER_COUNT = 3
            bridge.VIEWERS.notify_all()
        eventually(lambda: instance.observe_once.call_count == 1)
        time.sleep(.3)
        assert instance.observe_once.call_count == 1
        with bridge.VIEWERS:
            bridge.VIEWER_COUNT = 0
            bridge.VIEWERS.notify_all()
        count = instance.observe_once.call_count
        time.sleep(.3)
        assert instance.observe_once.call_count == count
    finally:
        instance.stopping.set()
        with bridge.VIEWERS:
            bridge.VIEWER_COUNT = 0
            bridge.VIEWERS.notify_all()
        thread.join(3)
    assert not thread.is_alive()
