"""Malformed metadata must have bounded memory and leave valid audio running."""
import base64
import hashlib
import plistlib
from unittest.mock import Mock

import pytest

from test_metadata_dispatch import item


PNG = b'\x89PNG\r\n\x1a\n'


def core_item(code, data):
    return item(code, data).replace(b'<type>73736e63</type>', b'<type>636f7265</type>')


def test_parser_preserves_every_fragment_boundary_and_line_wrapped_data(bridge):
    data = b'normal artwork' * 100
    encoded = base64.b64encode(data)
    wrapped = b'\n'.join(encoded[offset:offset + 76] for offset in range(0, len(encoded), 76))
    wire = item('PICT', data).replace(encoded, wrapped)
    for split in range(len(wire) + 1):
        parser = bridge.MetadataParser()
        events = list(parser.feed(wire[:split])) + list(parser.feed(wire[split:]))
        assert events == [('ssnc', 'PICT', data)]
        assert not parser.buffer


@pytest.mark.parametrize('broken', [
    item('pvol', b'x').replace(b'eA==', b'!!!!'),
    item('pvol', b'x').replace(b'eA==', b'=AAA'),
    item('pvol', b'x').replace(b'eA==', b'eA='),
    item('pvol', b'x').replace(b'<length>1</length>', b'<length>2</length>'),
    item('pvol', b'x').replace(b'<length>1</length>', b'<length>0</length>'),
    item('pvol').replace(b'<length>0</length>', b'<length>1</length>'),
    item('pvol', b'x').replace(b'<length>1</length>', b'<length>-1</length>'),
    item('pvol', b'x').replace(b'<length>1</length>', b'<length>' + b'9' * 1000 + b'</length>'),
    b'<item><invalid></item>',
    b'<item><type>73736e63</type><code>70696374</code><length>1</length>\n<data encoding="base64">eA==',
])
def test_parser_recovers_at_next_valid_item(bridge, broken):
    parser = bridge.MetadataParser()
    assert list(parser.feed(broken + item('pvol', b'-15,0,0,0'))) == [('ssnc', 'pvol', b'-15,0,0,0')]
    assert not parser.buffer


def test_incomplete_item_and_tail_stay_bounded_then_recover(bridge):
    parser = bridge.MetadataParser()
    prefix = item('test', b'x').split(b'eA==')[0]
    assert not list(parser.feed(prefix))
    # More than the raw item limit, with no closing tag.
    for _ in range(bridge.META_ITEM_MAX // 4096 + 2):
        assert not list(parser.feed(b'A' * 4096))
        assert len(parser.buffer) < bridge.META_ITEM_MAX
    events = list(parser.feed(item('pbeg') + b'junk' * (1024 * 1024) + b'<it'))
    assert events == [('ssnc', 'pbeg', b'')]
    assert parser.buffer == b'<it'
    assert list(parser.feed(item('pend')[3:])) == [('ssnc', 'pend', b'')]


def test_invalid_declared_length_is_rejected_before_base64_decode(bridge, monkeypatch):
    decode = Mock(side_effect=AssertionError('oversize item reached decoder'))
    monkeypatch.setattr(bridge.base64, 'b64decode', decode)
    parser = bridge.MetadataParser()
    prefix = item('test', b'x').split(b'eA==')[0]
    oversized = prefix.replace(b'<length>1</length>',
                               f'<length>{bridge.META_PAYLOAD_MAX + 1}</length>'.encode())
    assert not list(parser.feed(oversized))
    assert len(parser.buffer) <= 5
    decode.assert_not_called()


def test_decoded_payload_boundary(bridge):
    data = b'x' * bridge.META_PAYLOAD_MAX
    parser = bridge.MetadataParser()
    assert list(parser.feed(item('test', data))) == [('ssnc', 'test', data)]
    assert not parser.buffer


def test_raw_item_limit_includes_base64_whitespace(bridge, monkeypatch):
    monkeypatch.setattr(bridge, 'META_ITEM_MAX', 512)
    wire = item('test', b'x')
    padding = b' ' * (bridge.META_ITEM_MAX - len(wire))
    allowed = wire.replace(b'eA==', padding + b'eA==')
    assert list(bridge.MetadataParser().feed(allowed)) == [('ssnc', 'test', b'x')]
    parser = bridge.MetadataParser()
    assert list(parser.feed(allowed.replace(b'eA==', b' eA==') + item('pbeg'))) == [('ssnc', 'pbeg', b'')]
    assert not parser.buffer


@pytest.mark.parametrize('code', ['minm', 'asar', 'asal'])
def test_classic_text_limit_counts_utf8_bytes(bridge, code):
    parser = bridge.MetadataParser()
    allowed = ('é' * (bridge.META_TEXT_MAX // 2)).encode()
    assert list(parser.feed(core_item(code, allowed))) == [('core', code, allowed)]
    assert not list(parser.feed(core_item(code, allowed + b'x')))
    assert list(parser.feed(core_item(code, b'valid'))) == [('core', code, b'valid')]


def test_plist_text_and_artwork_limits_apply_to_nested_values(bridge):
    title = 'é' * (bridge.META_TEXT_MAX // 2)
    image = PNG + b'x' * (bridge.ART_ITEM_MAX - len(PNG))
    bridge.handle_copl(plistlib.dumps({'nested': [None, {
        'kMRMediaRemoteNowPlayingInfoTitle': title,
        'kMRMediaRemoteNowPlayingInfoArtworkData': image,
    }]}, fmt=plistlib.FMT_BINARY))
    assert bridge.NOW_PLAYING['title'] == title
    assert bridge.ART['bytes'] == image
    revision = bridge.STATE['revision']
    bridge.handle_copl(plistlib.dumps({'nested': {
        'kMRMediaRemoteNowPlayingInfoTitle': title + 'x',
        'kMRMediaRemoteNowPlayingInfoArtworkData': image + b'x',
    }}))
    assert bridge.NOW_PLAYING['title'] == title
    assert bridge.ART['bytes'] == image
    assert bridge.STATE['revision'] == revision


def test_plist_traversal_handles_cycles_and_null_values(bridge):
    fields = {'null': None, 'kMRMediaRemoteNowPlayingInfoTitle': 'valid'}
    fields['self'] = fields
    bridge.handle_copl(plistlib.dumps(fields, fmt=plistlib.FMT_BINARY))
    assert bridge.NOW_PLAYING['title'] == 'valid'


def test_direct_plist_input_rejects_oversize_bytes_before_parsing(bridge, monkeypatch):
    load = Mock(side_effect=AssertionError('oversize input reached plist parser'))
    monkeypatch.setattr(bridge.plistlib, 'loads', load)
    bridge.handle_copl(b'x' * (bridge.META_PAYLOAD_MAX + 1))
    load.assert_not_called()
    assert not bridge.NOW_PLAYING['title']


def test_replacement_characters_cannot_expand_track_field_above_utf8_limit(bridge):
    assert bridge.metadata_text('é' * (bridge.META_TEXT_MAX // 2)) is not None
    assert bridge.metadata_text((b'\xff' * bridge.META_TEXT_MAX).decode(errors='replace')) is None
    assert bridge.metadata_text('\ud800') is None


def test_artwork_cache_evicts_by_bytes_and_preserves_recent_covers(bridge):
    images = [PNG + bytes([index]) * (bridge.ART_ITEM_MAX - len(PNG)) for index in range(5)]
    ids = [hashlib.sha256(image).hexdigest() for image in images]
    for image in images[:4]:
        assert bridge.update_artwork(image)
    assert sum(len(entry[0]) for entry in bridge.ART_CACHE.values()) == bridge.ART_CACHE_BYTES_MAX
    assert bridge.update_artwork(images[0])  # Revisit the oldest cover.
    assert bridge.update_artwork(images[4])
    assert ids[1] not in bridge.ART_CACHE
    assert list(bridge.ART_CACHE) == [ids[2], ids[3], ids[0], ids[4]]
    assert bridge.ART['bytes'] == images[4]
    assert sum(len(entry[0]) for entry in bridge.ART_CACHE.values()) == bridge.ART_CACHE_BYTES_MAX


def test_oversize_cover_keeps_current_art_and_cache_unchanged(bridge):
    assert bridge.update_artwork(PNG + b'current')
    old_art, old_cache = dict(bridge.ART), dict(bridge.ART_CACHE)
    data = PNG + b'x' * bridge.ART_ITEM_MAX
    assert not bridge.update_artwork(data)
    assert not bridge.cache_art(hashlib.sha256(data).hexdigest(), data, 'image/png')
    assert bridge.ART == old_art
    assert dict(bridge.ART_CACHE) == old_cache
    assert not list(bridge.MetadataParser().feed(item('PICT', data)))


def test_cache_replacement_does_not_double_count_bytes(bridge):
    for index in range(4):
        assert bridge.cache_art(str(index), PNG + b'x' * (bridge.ART_ITEM_MAX - len(PNG)), 'image/png')
    assert bridge.cache_art('0', PNG + b'small', 'image/png')
    assert len(bridge.ART_CACHE) == 4
    assert sum(len(entry[0]) for entry in bridge.ART_CACHE.values()) < bridge.ART_CACHE_BYTES_MAX


def test_malformed_metadata_does_not_stop_audio_or_valid_volume(bridge, monkeypatch):
    renderer = Mock()
    bridge.handle_playback_metadata('pbeg', b'', renderer, {})
    bridge.handle_playback_metadata('odsc', b'44100/S16_LE/2', renderer, {})
    bridge.AUDIO.feed(b'\x01\0\x02\0' * 4)
    ring = bridge.AUDIO.snapshot()

    class Finished(BaseException):
        pass

    class Reader:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, _):
            if not hasattr(self, 'read_once'):
                self.read_once = True
                return (item('pvol', b'x').replace(b'eA==', b'!!!!')
                        + core_item('minm', b'\xff' * bridge.META_TEXT_MAX)
                        + item('mden') + item('pvol', b'-15,0,0,0'))
            raise Finished()

    monkeypatch.setattr(bridge, 'open', lambda *_args, **_kwargs: Reader(), raising=False)
    monkeypatch.setattr(bridge.threading, 'Thread', Mock())
    with pytest.raises(Finished):
        bridge.metadata_reader(renderer)
    renderer.set_volume.assert_called_once_with(50)
    renderer.stop.assert_not_called()
    assert bridge.AUDIO.snapshot() is ring
    assert bridge.STATE['active']
    assert not bridge.AUDIO.failed
    assert not bridge.NOW_PLAYING['title']
