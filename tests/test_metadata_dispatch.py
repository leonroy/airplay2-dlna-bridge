"""Renderer stalls must not prevent metadata boundary consumption."""
import base64
import threading
import time
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('stream_type,version', [('Classic', 1), ('Realtime', 2), ('Buffered', 2)])
@pytest.mark.parametrize('codec', ['AAC', 'ALAC', 'PCM'])
def test_source_metadata_reports_negotiated_codec_and_protocol_and_resets(bridge, stream_type, version, codec):
    renderer = Mock()
    pending = {}
    bridge.handle_playback_metadata('styp', stream_type.encode(), renderer, pending)
    bridge.handle_playback_metadata('sdsc', f'{codec}/48000/F24/2'.encode(), renderer, pending)
    bridge.handle_playback_metadata('pbeg', b'', renderer, pending)
    audio = bridge.status_snapshot()['audio']
    assert audio['airplay_version'] == version
    assert audio['codec'] == codec
    bridge.handle_playback_metadata('pend', b'', renderer, pending)
    audio = bridge.status_snapshot()['audio']
    assert audio['codec'] is None
    assert audio['airplay_version'] is None


def test_unrecognized_source_metadata_does_not_guess_airplay_version(bridge):
    renderer = Mock()
    bridge.handle_playback_metadata('styp', b'Unknown', renderer, {})
    bridge.handle_playback_metadata('sdsc', b'invalid\xff', renderer, {})
    audio = bridge.status_snapshot()['audio']
    assert audio['airplay_version'] is None
    assert audio['codec'] is None


def item(code, data=b''):
    encoded = b'\n<data encoding="base64">\n' + base64.b64encode(data) + b'</data>' if data else b''
    return (b'<item><type>73736e63</type><code>' + code.encode().hex().encode()
            + b'</code><length>' + str(len(data)).encode() + b'</length>' + encoded + b'</item>')


@pytest.mark.parametrize('stall', ['SetVolume', 'Stop'])
def test_metadata_consumes_new_session_while_renderer_stalls(bridge, monkeypatch, stall):
    monkeypatch.setattr(bridge, 'cancel_session_clients', lambda *_: None, raising=False)
    renderer = bridge.Renderer('127.0.0.1')
    renderer.run = lambda: None
    entered, release = threading.Event(), threading.Event()
    old_ring = []
    worker = []
    class Finished(BaseException):
        pass
    def action(name, *_):
        assert name == stall
        entered.set()
        assert release.wait(2)
        return True
    renderer.action = action
    class Reader:
        reads = 0
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, _):
            self.reads += 1
            if self.reads == 1:
                return (item('pbeg') + item('odsc', b'44100/S16_LE/2')
                        + (item('pvol', b'-15,0,0,0') if stall == 'SetVolume' else item('pend')))
            if self.reads == 2:
                old_ring.append(bridge.AUDIO.ring)
                thread = threading.Thread(target=renderer.dispatch_once)
                worker.append(thread)
                thread.start()
                assert entered.wait(1)
                return ((item('pend') if stall == 'SetVolume' else b'')
                        + item('pbeg') + item('odsc', b'48000/S32_LE/2'))
            assert bridge.AUDIO.snapshot().pcm.description == '48000/S32_LE/2'
            assert not release.is_set()
            bridge.AUDIO.feed(b'\x01\0\0\0' * 2)
            assert bridge.AUDIO.pcm_bytes == 8
            if old_ring[0] is not None:
                assert old_ring[0].closed
            raise Finished()
    monkeypatch.setattr(bridge, 'open', lambda *_args, **_kwargs: Reader(), raising=False)
    try:
        with pytest.raises(Finished):
            bridge.metadata_reader(renderer)
    finally:
        release.set()
        for thread in worker:
            thread.join(2)
            assert not thread.is_alive()
    assert bridge.STATE['active']


@pytest.mark.parametrize('order', [('audio', 'odsc', 'pbeg'), ('odsc', 'audio', 'pbeg')])
def test_early_audio_and_description_do_not_dispatch_before_pbeg(bridge, monkeypatch, order):
    monkeypatch.setattr(bridge, 'cancel_session_clients', lambda *_: None, raising=False)
    renderer = bridge.Renderer('127.0.0.1')
    renderer.action = Mock(return_value=True)
    for event in order:
        if event == 'audio':
            bridge.AUDIO.feed(b'\0' * 8)
        else:
            bridge.handle_playback_metadata(event, b'44100/S16_LE/2' if event == 'odsc' else b'', renderer, {})
        renderer.dispatch_once(time.monotonic() + 10)
        if event != 'pbeg':
            renderer.action.assert_not_called()
    renderer.dispatch_once(time.monotonic() + 10)
    assert [call.args[0] for call in renderer.action.call_args_list] == ['SetAVTransportURI', 'Play']
    assert bridge.AUDIO.pcm_bytes == 8
