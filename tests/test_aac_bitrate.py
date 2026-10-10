from unittest.mock import Mock

import pytest


def metadata(bridge, code, payload):
    return bridge.handle_playback_metadata(code, payload, Mock(), {})


def start_aac(bridge, rate=48000, layout="2"):
    metadata(bridge, "sdsc", f"AAC/{rate}/F24/{layout}".encode())


@pytest.mark.parametrize("rate,samples,layout", [(48000, 96000, "2"), (44100, 88200, "2"),
                                                (48000, 96000, "5.1"), (48000, 96000, "7.1")])
def test_measured_average_uses_input_audio_duration(bridge, rate, samples, layout):
    start_aac(bridge, rate, layout)
    metadata(bridge, "abrt", f"64000/{samples}/{rate}".encode())
    audio = bridge.status_snapshot()["audio"]
    assert audio["aac_bitrate_bps"] == 256000
    assert audio["source_format"] == f"AAC/{rate}/F24/{layout}"


def test_stock_receiver_and_other_codecs_have_no_measurement(bridge):
    assert bridge.status_snapshot()["audio"]["aac_bitrate_bps"] is None
    start_aac(bridge)
    assert bridge.status_snapshot()["audio"]["aac_bitrate_bps"] is None
    metadata(bridge, "sdsc", b"ALAC/48000/S24_LE/2")
    metadata(bridge, "abrt", b"64000/96000/48000")
    assert bridge.AUDIO.aac_bitrate_bps is None


@pytest.mark.parametrize("payload", [
    b"", b"1/0/48000", b"0/96000/48000", b"64000/96000/44100",
    b"64000/1024/48000", b"64000/96000/0", b"-1/96000/48000",
    b"1/2/3/4", b"1" * 100, b"\xff", b"999999999999999999999/96000/48000",
    b"99999999999999999999/96000/48000", b"64000/96000/48000\n",
])
def test_invalid_measurement_does_not_replace_valid_value(bridge, payload):
    start_aac(bridge)
    metadata(bridge, "abrt", b"64000/96000/48000")
    metadata(bridge, "abrt", payload)
    assert bridge.AUDIO.aac_bitrate_bps == 256000


@pytest.mark.parametrize("boundary", ["reset", "pfls", "rate", "codec", "end", "fail"])
def test_measurement_resets_at_audio_boundaries(bridge, boundary):
    start_aac(bridge)
    metadata(bridge, "abrt", b"64000/96000/48000")
    if boundary == "reset":
        metadata(bridge, "abrt", b"0/0/0")
    elif boundary == "pfls":
        metadata(bridge, "pfls", b"123")
    elif boundary == "rate":
        start_aac(bridge, 44100)
    elif boundary == "codec":
        metadata(bridge, "sdsc", b"ALAC/48000/S24_LE/2")
    elif boundary == "end":
        bridge.AUDIO.end(drain=False)
    else:
        bridge.AUDIO.fail("sample failure")
    assert bridge.status_snapshot()["audio"]["aac_bitrate_bps"] is None


def test_duplicate_description_retains_measurement(bridge):
    start_aac(bridge)
    metadata(bridge, "abrt", b"64000/96000/48000")
    start_aac(bridge)
    assert bridge.AUDIO.aac_bitrate_bps == 256000


def test_late_measurement_cannot_reopen_ended_session(bridge):
    start_aac(bridge)
    bridge.AUDIO.end(drain=False)
    metadata(bridge, "abrt", b"64000/96000/48000")
    assert bridge.AUDIO.session_id is None
    assert bridge.AUDIO.aac_bitrate_bps is None


def test_bitrate_event_passes_real_metadata_parser(bridge):
    import base64
    start_aac(bridge)
    data = b"64000/96000/48000"
    item = (b"<item><type>73736e63</type><code>61627274</code><length>18</length>"
            b'<data encoding="base64">' + base64.b64encode(data) + b"</data></item>")
    # Use the actual byte count rather than relying on a fixture's formatter.
    item = item.replace(b"<length>18</length>", f"<length>{len(data)}</length>".encode())
    for typ, code, payload in bridge.MetadataParser().feed(item):
        assert typ == "ssnc"
        metadata(bridge, code, payload)
    assert bridge.AUDIO.aac_bitrate_bps == 256000


def test_receiver_counters_are_zero_when_reported_and_unavailable_when_absent(bridge):
    assert bridge.status_snapshot()["audio"]["receiver_stats"] is None
    start_aac(bridge)
    metadata(bridge, "arst", b"0/0/0")
    assert bridge.status_snapshot()["audio"]["receiver_stats"] == {
        "missing_audio_blocks": 0, "too_late_audio_blocks": 0, "retry_requests": 0}
    value = bridge.status_snapshot()
    value["audio"]["receiver_stats"]["missing_audio_blocks"] = 99
    assert bridge.AUDIO.receiver_stats["missing_audio_blocks"] == 0


@pytest.mark.parametrize("codec", ["AAC", "ALAC", "PCM"])
def test_receiver_counters_are_codec_independent_session_totals(bridge, codec):
    metadata(bridge, "sdsc", f"{codec}/48000/F24/2".encode())
    metadata(bridge, "arst", b"3/2/5")
    metadata(bridge, "pfls", b"123")
    assert bridge.status_snapshot()["audio"]["receiver_stats"] == {
        "missing_audio_blocks": 3, "too_late_audio_blocks": 2, "retry_requests": 5}


@pytest.mark.parametrize("payload", [
    b"", b"1/2", b"1/2/3/4", b"-1/2/3", b"1.5/2/3", b"1/2/3\n",
    b"1" * 100, b"\xff", b"18446744073709551616/0/0", b"9007199254740992/0/0"])
def test_invalid_receiver_counters_preserve_previous_report(bridge, payload):
    start_aac(bridge)
    metadata(bridge, "arst", b"3/2/5")
    metadata(bridge, "arst", payload)
    assert bridge.AUDIO.receiver_stats["missing_audio_blocks"] == 3


@pytest.mark.parametrize("boundary", ["end", "fail"])
def test_receiver_counters_clear_at_session_end_and_reject_late_reports(bridge, boundary):
    start_aac(bridge)
    metadata(bridge, "arst", b"3/2/5")
    if boundary == "end":
        bridge.AUDIO.end(drain=False)
    else:
        bridge.AUDIO.fail("sample failure")
    metadata(bridge, "arst", b"4/3/6")
    assert bridge.status_snapshot()["audio"]["receiver_stats"] is None


def test_receiver_counter_event_passes_real_metadata_parser(bridge):
    import base64
    start_aac(bridge)
    data = b"3/2/5"
    item = (b"<item><type>73736e63</type><code>61727374</code><length>5</length>"
            b'<data encoding="base64">' + base64.b64encode(data) + b"</data></item>")
    for typ, code, payload in bridge.MetadataParser().feed(item):
        assert typ == "ssnc"
        metadata(bridge, code, payload)
    assert bridge.status_snapshot()["audio"]["receiver_stats"]["retry_requests"] == 5


def test_late_receiver_counter_event_cannot_open_session(bridge):
    metadata(bridge, "arst", b"3/2/5")
    assert bridge.AUDIO.session_id is None
    assert bridge.AUDIO.receiver_stats is None
