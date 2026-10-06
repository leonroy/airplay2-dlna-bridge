"""Renderer dispatch tests use deterministic steps rather than sleeping workers."""
import socket
import threading
import time
from unittest.mock import Mock

import pytest


@pytest.fixture
def renderer(bridge, monkeypatch):
    monkeypatch.setattr(bridge, 'cancel_session_clients', lambda *_: None, raising=False)
    monkeypatch.setattr(bridge, 'has_active_clients', lambda _: False, raising=False)
    bridge.handle_playback_metadata('pbeg', b'', Mock(), {})
    bridge.handle_output_description(b'44100/S16_LE/2', Mock())
    bridge.NOW_PLAYING.update(title='Original', artist='Artist', album='Album')
    bridge.STATE['dirty'] = time.monotonic() - bridge.PUSH_SETTLE - 1
    result = bridge.Renderer('127.0.0.1')
    result.action = Mock(return_value=True)
    return result


def step(renderer):
    return renderer.dispatch_once(time.monotonic() + 10)


def names(renderer):
    return [call.args[0] for call in renderer.action.call_args_list]


@pytest.mark.parametrize('failed_stage', ['SetAVTransportURI', 'Play'])
def test_failed_stage_retries_without_acknowledging(bridge, renderer, failed_stage):
    outcomes = {'SetAVTransportURI': [failed_stage != 'SetAVTransportURI', True],
                'Play': [failed_stage != 'Play', True]}
    renderer.action.side_effect = lambda name, *_: outcomes[name].pop(0)
    assert step(renderer)
    if failed_stage == 'Play':
        assert step(renderer)
    assert bridge.STATE['pushed'] is None
    assert bridge.STATE['dirty']
    assert step(renderer)
    if failed_stage == 'SetAVTransportURI':
        assert step(renderer)
    assert bridge.STATE['pushed'][0] == 'Original'
    assert bridge.STATE['dirty'] == 0
    assert names(renderer).count('SetAVTransportURI') == (2 if failed_stage == 'SetAVTransportURI' else 1)


@pytest.mark.parametrize('stage', ['SetAVTransportURI', 'Play'])
@pytest.mark.parametrize('replace', [False, True])
def test_session_end_or_replacement_during_inflight_action(bridge, renderer, stage, replace):
    def action(name, *_):
        assert not bridge.AUDIO.lock._is_owned()
        if name == stage:
            bridge.handle_playback_metadata('pend', b'', renderer, {})
            if replace:
                bridge.handle_playback_metadata('pbeg', b'', renderer, {})
                bridge.handle_output_description(b'48000/S32_LE/2', renderer)
                bridge.NOW_PLAYING['title'] = 'Replacement'
                bridge.STATE['dirty'] = time.monotonic() - 3
        return True
    renderer.action.side_effect = action
    step(renderer)
    if stage == 'Play':
        step(renderer)
    step(renderer)
    assert names(renderer)[-1] == 'Stop'
    assert bridge.STATE['pushed'] is None
    renderer.action.side_effect = None
    if replace:
        step(renderer)
        step(renderer)
        assert bridge.STATE['pushed'][0] == 'Replacement'
    else:
        assert not step(renderer)
        assert names(renderer).count('Play') == (1 if stage == 'Play' else 0)


def test_update_racing_ack_preserves_latest_revision(bridge, renderer):
    step(renderer)
    def action(*_):
        with bridge.AUDIO.lock:
            bridge.NOW_PLAYING['title'] = 'Latest'
            bridge.mark_metadata_dirty()
        return True
    renderer.action.side_effect = action
    step(renderer)
    assert bridge.STATE['pushed'] is None
    assert bridge.STATE['dirty']
    renderer.action.side_effect = None
    step(renderer)
    step(renderer)
    assert bridge.STATE['pushed'][0] == 'Latest'


def test_uri_uses_captured_metadata(bridge, renderer):
    step(renderer)
    captured = renderer.action.call_args.args[1]
    bridge.NOW_PLAYING['title'] = 'Unrelated later mutation'
    assert 'Original' in captured
    assert 'Unrelated' not in captured


def test_stop_preempts_retries_and_clears_pending_volume(bridge, renderer):
    renderer.set_volume(10)
    renderer.action.return_value = False
    step(renderer)
    renderer.set_volume(90)
    bridge.handle_playback_metadata('pend', b'', renderer, {})
    renderer.action.return_value = True
    step(renderer)
    assert names(renderer) == ['SetAVTransportURI', 'Stop']
    assert not step(renderer)


def test_latest_volume_coalesces_and_has_no_network_in_publisher(bridge, renderer):
    bridge.STATE['dirty'] = 0
    renderer.set_volume(10)
    renderer.set_volume(20)
    renderer.set_volume(30)
    renderer.action.assert_not_called()
    step(renderer)
    assert names(renderer) == ['SetVolume']
    assert '<DesiredVolume>30</DesiredVolume>' in renderer.action.call_args.args[1]


def test_retry_budget_is_bounded_and_new_revision_recovers(bridge, renderer):
    renderer.action.return_value = False
    for _ in range(20):
        step(renderer)
    assert renderer.action.call_count == 4
    with bridge.AUDIO.lock:
        bridge.mark_metadata_dirty()
    renderer.action.return_value = True
    step(renderer)
    step(renderer)
    assert bridge.STATE['pushed']


def test_didl_disabled_still_starts_and_does_not_refresh(bridge, renderer, monkeypatch):
    monkeypatch.setattr(bridge, 'DIDL_PUSH', False)
    step(renderer)
    step(renderer)
    with bridge.AUDIO.lock:
        bridge.NOW_PLAYING['title'] = 'Changed'
        bridge.mark_metadata_dirty()
    assert not step(renderer)
    assert names(renderer) == ['SetAVTransportURI', 'Play']
    assert bridge.STATE['dirty'] == 0


def test_resume_is_session_bound(bridge, renderer):
    step(renderer)
    step(renderer)
    renderer.action.reset_mock()
    bridge.STATE['dirty'] = 0
    renderer.resume(bridge.AUDIO.session_id)
    step(renderer)
    assert names(renderer) == ['Play']
    renderer.resume(bridge.AUDIO.session_id)
    bridge.handle_playback_metadata('pend', b'', renderer, {})
    step(renderer)
    assert names(renderer) == ['Play', 'Stop']


@pytest.mark.parametrize('response,success', [
    (b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault/></s:Body></s:Envelope>', False),
    (b'<u:PlayResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"/>', True),
    (b'<unexpected/>', False),
    (b'not XML', False),
])
def test_action_validates_soap(bridge, monkeypatch, response, success):
    renderer = bridge.Renderer('127.0.0.1')
    renderer.controls['AVTransport'] = 'http://127.0.0.1/control'
    monkeypatch.setattr(bridge, 'http_req', Mock(return_value=response))
    assert renderer.action('Play', '<Speed>1</Speed>') is success


def test_resolve_and_action_share_deadline(bridge, monkeypatch):
    renderer = bridge.Renderer('127.0.0.1')
    deadlines = []
    def request(url, **kwargs):
        deadlines.append(kwargs['deadline'])
        if 'description.xml' in url:
            return b'<service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType><controlURL>/control</controlURL></service>'
        return b'<u:PlayResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"/>'
    monkeypatch.setattr(bridge, 'http_req', request)
    assert renderer.action('Play', '')
    assert len(deadlines) == 2 and deadlines[0] == deadlines[1]


@pytest.mark.parametrize('worker', ['command', 'observation'])
@pytest.mark.parametrize('namespace', ['', 'default', 'prefixed'])
@pytest.mark.parametrize('control', ['/control', 'control', 'http://192.0.2.10:49152/control'])
def test_both_workers_discover_xml_endpoints_and_build_soap(bridge, monkeypatch, worker, namespace, control):
    prefix = 'd:' if namespace == 'prefixed' else ''
    declaration = (' xmlns:d="urn:schemas-upnp-org:device-1-0"' if namespace == 'prefixed'
                   else ' xmlns="urn:schemas-upnp-org:device-1-0"' if namespace == 'default' else '')
    description = (
        f'<{prefix}root{declaration}><{prefix}device><{prefix}friendlyName> Example Speaker </{prefix}friendlyName>'
        f'<{prefix}service><{prefix}serviceType>urn:schemas-upnp-org:service:AVTransport:1</{prefix}serviceType>'
        f'<{prefix}controlURL>{control}</{prefix}controlURL></{prefix}service>'
        f'</{prefix}device></{prefix}root>'
    ).encode()
    calls = []
    def request(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith('/description.xml'):
            return description
        return (b'<u:GetTransportInfoResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                b'<CurrentTransportState>PLAYING</CurrentTransportState></u:GetTransportInfoResponse>')
    monkeypatch.setattr(bridge, 'http_req', request)
    if worker == 'command':
        instance = bridge.Renderer('192.0.2.10')
        assert instance.action('GetTransportInfo', '')
    else:
        instance = bridge.RecipientObserver('192.0.2.10')
        assert instance.read('GetTransportInfo', '', 'AVTransport', 'CurrentTransportState', time.monotonic() + 2) == 'PLAYING'
        assert instance.snapshot()['name'] == 'Example Speaker'
    assert instance.controls['AVTransport'] == 'http://192.0.2.10:49152/control'
    assert len(calls) == 2
    assert calls[0][1]['deadline'] == calls[1][1]['deadline']
    body = bridge.ElementTree.fromstring(calls[1][1]['data'])
    action = body.find('.//{urn:schemas-upnp-org:service:AVTransport:1}GetTransportInfo')
    assert action.find('InstanceID').text == '0'
    assert calls[1][1]['headers']['SOAPACTION'] == '"urn:schemas-upnp-org:service:AVTransport:1#GetTransportInfo"'
    assert calls[1][1]['max_bytes'] == bridge.SOAP_BODY_MAX
    assert calls[1][1].get('allowed_statuses', ()) == ((500,) if worker == 'observation' else ())


@pytest.mark.parametrize('worker', ['command', 'observation'])
@pytest.mark.parametrize('service', ['AVTransport', 'RenderingControl'])
@pytest.mark.parametrize('version', ['1', '2', '3', '02'])
def test_newer_services_accept_version_one_commands_and_observations(bridge, monkeypatch, worker, service, version):
    if service == 'AVTransport':
        name, args, field, value = 'GetTransportInfo', '', 'CurrentTransportState', 'PLAYING'
        expected = 'PLAYING'
    else:
        name, args, field, value = 'GetVolume', '<Channel>Master</Channel>', 'CurrentVolume', '25'
        expected = 25
    calls = []
    def request(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith('/description.xml'):
            return (
                '<root><service>'
                f'<serviceType>urn:schemas-upnp-org:service:{service}:{version}</serviceType>'
                '<controlURL>/control</controlURL></service></root>'
            ).encode()
        return (f'<u:{name}Response xmlns:u="urn:schemas-upnp-org:service:{service}:1">'
                f'<{field}>{value}</{field}></u:{name}Response>').encode()
    monkeypatch.setattr(bridge, 'http_req', request)
    if worker == 'command':
        instance = bridge.Renderer('192.0.2.10')
        assert instance.action(name, args, service)
    else:
        instance = bridge.RecipientObserver('192.0.2.10')
        assert instance.read(name, args, service, field, time.monotonic() + 2) == expected
    assert instance.controls[service] == 'http://192.0.2.10:49152/control'
    assert len(calls) == 2
    assert calls[0][1]['deadline'] == calls[1][1]['deadline']
    # Use only version-one functionality even when discovery advertises newer versions.
    assert calls[1][1]['headers']['SOAPACTION'] == f'"urn:schemas-upnp-org:service:{service}:1#{name}"'
    root = bridge.ElementTree.fromstring(calls[1][1]['data'])
    assert root.find(f'.//{{urn:schemas-upnp-org:service:{service}:1}}{name}') is not None


@pytest.mark.parametrize('suffix', ['0', '-1', '2.0', 'two', '2:extra', '2x', ''])
def test_discovery_rejects_invalid_service_versions(bridge, suffix):
    description = (
        '<root><service>'
        f'<serviceType>urn:schemas-upnp-org:service:AVTransport:{suffix}</serviceType>'
        '<controlURL>/wrong</controlURL></service></root>'
    ).encode()
    assert bridge.description_control(description, '192.0.2.10', 'AVTransport')[0] is None


@pytest.mark.parametrize('worker', ['command', 'observation'])
@pytest.mark.parametrize('control', [
    'http://192.0.2.11:49152/control', 'http://192.0.2.10:8080/control',
    'https://192.0.2.10:49152/control', 'http://user:password@192.0.2.10:49152/control',
    'http://example.invalid:49152/control',
])
def test_both_workers_reject_discovery_that_changes_the_target(bridge, monkeypatch, worker, control):
    request = Mock(return_value=(
        '<root><service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>'
        f'<controlURL>{control}</controlURL></service></root>'
    ).encode())
    monkeypatch.setattr(bridge, 'http_req', request)
    with pytest.raises(ValueError, match='changed the target'):
        if worker == 'command':
            bridge.Renderer('192.0.2.10').resolve('AVTransport', time.monotonic() + 2)
        else:
            bridge.RecipientObserver('192.0.2.10').read(
                'GetTransportInfo', '', 'AVTransport', 'CurrentTransportState', time.monotonic() + 2)
    assert request.call_count == 1


def test_discovery_matches_exact_service_and_preserves_independent_caches(bridge, monkeypatch):
    descriptions = [
        b'<root><service><serviceType>urn:schemas-upnp-org:service:AVTransportExtra:1</serviceType><controlURL>/wrong</controlURL></service></root>',
        b'<root><service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType><controlURL>/command</controlURL></service></root>',
        b'<root><service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType><controlURL>/observation</controlURL></service></root>',
    ]
    def request(url, **kwargs):
        if url.endswith('/description.xml'):
            return descriptions.pop(0)
        return (b'<u:GetTransportInfoResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                b'<CurrentTransportState>STOPPED</CurrentTransportState></u:GetTransportInfoResponse>')
    monkeypatch.setattr(bridge, 'http_req', request)
    command, observer = bridge.Renderer('192.0.2.10'), bridge.RecipientObserver('192.0.2.10')
    with pytest.raises(ValueError, match='no AVTransport control URL'):
        command.resolve('AVTransport', time.monotonic() + 2)
    command.resolve('AVTransport', time.monotonic() + 2)
    assert observer.read('GetTransportInfo', '', 'AVTransport', 'CurrentTransportState', time.monotonic() + 2) == 'STOPPED'
    assert command.controls['AVTransport'].endswith('/command')
    assert observer.controls['AVTransport'].endswith('/observation')
    assert not descriptions


def test_discovery_accepts_same_ipv6_target_and_soap_arguments(bridge, monkeypatch):
    renderer = bridge.Renderer('2001:db8::10')
    calls = []
    def request(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith('/description.xml'):
            return (b'<root><service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>'
                    b'<controlURL>http://[2001:db8::10]:49152/control</controlURL></service></root>')
        return b'<u:PlayResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"/>'
    monkeypatch.setattr(bridge, 'http_req', request)
    assert renderer.action('Play', '<Speed>1</Speed>')
    assert calls[0][0] == 'http://[2001:db8::10]:49152/description.xml'
    assert calls[1][0] == 'http://[2001:db8::10]:49152/control'
    body = bridge.ElementTree.fromstring(calls[1][1]['data'])
    assert body.find('.//Speed').text == '1'


@pytest.mark.parametrize('trickle_headers', [False, True])
def test_http_absolute_deadline_stops_trickle_and_releases_watcher(bridge, monkeypatch, trickle_headers):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    timers = []
    real_timer = bridge.threading.Timer
    def timer(*args):
        result = real_timer(*args)
        timers.append(result)
        return result
    monkeypatch.setattr(bridge.threading, 'Timer', timer)
    done = threading.Event()
    def serve():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.recv(4096)
                if trickle_headers:
                    connection.sendall(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                else:
                    connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n')
                while not done.wait(0.01):
                    connection.sendall(b'x')
        except OSError:
            pass
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises((TimeoutError, OSError, bridge.http.client.HTTPException)):
            bridge.http_req(f'http://127.0.0.1:{listener.getsockname()[1]}/', timeout=0.15)
        assert time.monotonic() - started < 1
        assert timers and all(not timer.is_alive() for timer in timers)
    finally:
        done.set()
        listener.close()
        thread.join(1)
    assert not thread.is_alive()


def test_http_rejects_hostname_without_dns(bridge, monkeypatch):
    resolve = Mock(side_effect=AssertionError('DNS should not be attempted'))
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    with pytest.raises(ValueError):
        bridge.http_req('http://renderer.invalid/control')
    resolve.assert_not_called()


def test_http_connect_failure_releases_watcher(bridge, monkeypatch):
    class FailedSocket:
        def settimeout(self, _): pass
        def connect(self, _): raise OSError('connection refused')
        def close(self): pass
    monkeypatch.setattr(bridge.socket, 'socket', lambda *_: FailedSocket())
    timers = []
    real_timer = bridge.threading.Timer
    def timer(*args):
        result = real_timer(*args)
        timers.append(result)
        return result
    monkeypatch.setattr(bridge.threading, 'Timer', timer)
    with pytest.raises(OSError):
        bridge.http_req('http://127.0.0.1/control')
    assert all(not timer.is_alive() for timer in timers)


def test_exhausted_uri_allows_volume_and_new_resume_rearms_uri(bridge, renderer):
    renderer.action.return_value = False
    for _ in range(10):
        step(renderer)
    assert names(renderer) == ['SetAVTransportURI'] * 4
    renderer.set_volume(40)
    renderer.action.return_value = True
    step(renderer)
    assert names(renderer)[-1] == 'SetVolume'
    renderer.resume(bridge.AUDIO.session_id)
    step(renderer)
    step(renderer)
    assert names(renderer)[-2:] == ['SetAVTransportURI', 'Play']
    assert bridge.STATE['pushed'][0] == 'Original'
    assert not step(renderer)


def test_volume_during_backoff_does_not_reset_uri_budget(bridge, renderer):
    renderer.action.return_value = False
    for attempt in range(4):
        step(renderer)
        renderer.set_volume(attempt)
        renderer.action.return_value = True
        renderer.dispatch_once()
        assert names(renderer)[-1] == 'SetVolume'
        renderer.action.return_value = False
    for _ in range(5):
        step(renderer)
    assert names(renderer).count('SetAVTransportURI') == 4


def test_exhausted_play_allows_resume_without_repeating_uri(bridge, renderer):
    step(renderer)
    renderer.action.return_value = False
    for _ in range(10):
        step(renderer)
    renderer.resume(bridge.AUDIO.session_id)
    renderer.action.return_value = True
    step(renderer)
    assert names(renderer)[-1] == 'Play'
    assert names(renderer).count('SetAVTransportURI') == 1
    assert bridge.STATE['pushed'][0] == 'Original'


def test_failed_old_stop_does_not_strand_valid_replacement(bridge, renderer):
    bridge.handle_playback_metadata('pend', b'', renderer, {})
    renderer.action.return_value = False
    step(renderer)
    assert names(renderer) == ['Stop']
    bridge.handle_playback_metadata('pbeg', b'', renderer, {})
    bridge.handle_output_description(b'48000/S32_LE/2', renderer)
    bridge.STATE['dirty'] = time.monotonic() - 3
    renderer.action.return_value = True
    step(renderer)
    step(renderer)
    assert names(renderer) == ['Stop', 'SetAVTransportURI', 'Play']
    assert bridge.STATE['pushed']


def test_eof_publishes_stop_before_cancellation_can_start_replacement(bridge, renderer, monkeypatch):
    ended = bridge.AUDIO.session_id
    def cancel(session, reason):
        assert session == ended
        assert not bridge.AUDIO.lock._is_owned()
        assert renderer.stop_revision == 1
        bridge.handle_playback_metadata('pbeg', b'', renderer, {})
        bridge.handle_output_description(b'48000/S32_LE/2', renderer)
        renderer.set_volume(75)
        renderer.resume(bridge.AUDIO.session_id)
    monkeypatch.setattr(bridge, 'cancel_session_clients', cancel)
    assert bridge.handle_audio_eof(ended, renderer)
    assert renderer.volume[1] == 75
    assert renderer.resume_session[0] == bridge.AUDIO.session_id
    assert bridge.AUDIO.session_id != ended


@pytest.mark.parametrize('operation', ['volume', 'resume'])
def test_republishing_same_value_recovers_exhausted_request(bridge, renderer, operation):
    if operation == 'resume':
        step(renderer)
        step(renderer)
        renderer.action.reset_mock()
    bridge.STATE['dirty'] = 0
    publish = (lambda: renderer.set_volume(30)) if operation == 'volume' else (lambda: renderer.resume(bridge.AUDIO.session_id))
    publish()
    renderer.action.return_value = False
    for _ in range(10):
        step(renderer)
    assert renderer.action.call_count == 4
    publish()
    renderer.action.return_value = True
    step(renderer)
    assert renderer.action.call_count == 5


@pytest.mark.parametrize('body,status,error', [(b'12345', 200, ValueError), (b'ok', 503, OSError)])
def test_http_bounds_body_and_releases_response(bridge, monkeypatch, body, status, error):
    response = Mock(status=status)
    response.read.return_value = body
    connection = Mock()
    connection.getresponse.return_value = response
    sock = Mock()
    monkeypatch.setattr(bridge.http.client, 'HTTPConnection', Mock(return_value=connection))
    monkeypatch.setattr(bridge.socket, 'socket', Mock(return_value=sock))
    with pytest.raises(error):
        bridge.http_req('http://127.0.0.1/control', max_bytes=4)
    response.read.assert_called_once_with(5)
    response.close.assert_called_once()
    sock.close.assert_called_once()
