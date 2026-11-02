"""Render measured input values using the existing isolated browser fixtures."""
from unittest.mock import Mock

import pytest

from test_log_browser import browser, bridge_page, expect


def snapshot(bridge, codec="AAC", measured=True, layout="2"):
    bridge.AUDIO.begin()
    bridge.handle_playback_metadata("sdsc", f"{codec}/48000/F24/{layout}".encode(), Mock(), {})
    bridge.AUDIO.describe("48000/S32_LE/2")
    bridge.AUDIO.feed(b"\x00" * 1024)
    if measured:
        bridge.handle_playback_metadata("abrt", b"64000/96000/48000", Mock(), {})
    return bridge.status_snapshot()


@pytest.mark.parametrize("layout,label", [("2", "Stereo"), ("5.1", "5.1"), ("7.1", "7.1")])
def test_page_displays_measured_aac_in_codec_and_details(bridge_page, bridge, layout, label):
    page = bridge_page
    page.evaluate("value => window.testLogs.status(value)", snapshot(bridge, layout=layout))
    # Stereo has no channel tag; surround adds one ahead of the codec tag.
    assert page.locator("#tags .tag").all_text_contents() == (["AAC"] if layout == "2" else [layout, "AAC"])
    expect(page.locator("#codec")).to_have_text(f"AAC {label} · 48 kHz · 256 kbps")
    expect(page.locator("#format")).to_have_text("FLAC Stereo · 48 kHz · 32-bit")
    expect(page.locator("#spec")).to_have_text("256 kbps · 48 kHz")
    expect(page.locator("#details")).to_contain_text("Received from AirPlay")
    expect(page.locator("#details")).to_contain_text(f"AAC · {label} · 48 kHz")
    expect(page.locator("#codec")).to_contain_text("256 kbps")
    expect(page.locator("#details")).to_contain_text("Input bitrate (measured average)")
    expect(page.locator("#details")).to_contain_text("256 kbps (measured average)")


def test_stock_receiver_is_unavailable_and_pcm_hides_bitrate(bridge_page, bridge):
    page = bridge_page
    value = snapshot(bridge, measured=False)
    value["audio"].pop("input_bitrate_bps")
    value["audio"].pop("source_format")
    value["audio"].pop("output_stream")
    page.evaluate("value => window.testLogs.status(value)", value)
    expect(page.locator("#codec")).to_have_text("Unknown")
    expect(page.locator("#format")).to_have_text("Unknown")
    expect(page.locator("#tags .tag")).to_have_count(0)
    expect(page.locator("#spec")).to_have_text("Unknown format")
    expect(page.locator("#details")).to_contain_text("Unavailable")
    expect(page.locator("#codec")).not_to_contain_text("kbps")
    page.evaluate("value => window.testLogs.status(value)", snapshot(bridge, codec="PCM"))
    expect(page.locator("#details")).not_to_contain_text("Input bitrate")
    expect(page.locator("#codec")).not_to_contain_text("kbps")


@pytest.mark.parametrize("rate,bits,payload,kbps", [
    (44100, "S16_LE", b"220500/88200/44100", "882"),
    (48000, "S24_LE", b"360000/96000/48000", "1440")])
def test_page_displays_measured_alac(bridge_page, bridge, rate, bits, payload, kbps):
    snapshot(bridge, codec="ALAC", measured=False)
    bridge.handle_playback_metadata("sdsc", f"ALAC/{rate}/{bits}/2".encode(), Mock(), {})
    bridge.handle_playback_metadata("abrt", payload, Mock(), {})
    page = bridge_page
    page.evaluate("value => window.testLogs.status(value)", bridge.status_snapshot())
    expect(page.locator("#codec")).to_contain_text("ALAC")
    # Lossless input shows its bit depth in the path; the bitrate is the chart's headline.
    expect(page.locator("#codec")).to_contain_text(f"{rate / 1000:g} kHz · {16 if rate == 44100 else 24}-bit")
    expect(page.locator("#codec")).not_to_contain_text("kbps")
    expect(page.locator("#bitrate-now")).to_have_text(kbps)
    expect(page.locator("#details")).to_contain_text("Input bitrate (measured average)")
    expect(page.locator("#details")).to_contain_text(f"{kbps} kbps (measured average)")


def test_reset_removes_old_measurement_from_page(bridge_page, bridge):
    page = bridge_page
    page.evaluate("value => window.testLogs.status(value)", snapshot(bridge))
    expect(page.locator("#codec")).to_contain_text("256 kbps")
    bridge.handle_playback_metadata("abrt", b"0/0/0", Mock(), {})
    page.evaluate("value => window.testLogs.status(value)", bridge.status_snapshot())
    expect(page.locator("#codec")).not_to_contain_text("kbps")
    expect(page.locator("#details")).to_contain_text("Unavailable")
    bridge.AUDIO.end(drain=False)
    page.evaluate("value => window.testLogs.status(value)", bridge.status_snapshot())
    expect(page.locator("#codec")).to_have_text("")
    expect(page.locator("#format")).to_have_text("")


def test_receiver_counters_appear_only_in_details(bridge_page, bridge):
    page = bridge_page
    value = snapshot(bridge)
    bridge.handle_playback_metadata("arst", b"3/2/5", Mock(), {})
    value = bridge.status_snapshot()
    page.evaluate("value => window.testLogs.status(value)", value)
    rows = page.locator("#details .detail-row")
    expect(rows.filter(has=page.locator("dt", has_text="Missing audio blocks (session)")).locator("dd")).to_have_text("3")
    expect(rows.filter(has=page.locator("dt", has_text="Too-late audio blocks (session)")).locator("dd")).to_have_text("2")
    expect(rows.filter(has=page.locator("dt", has_text="Retry requests (session)")).locator("dd")).to_have_text("5")
    expect(page.locator("#codec")).to_contain_text("256 kbps")
    expect(page.locator("#codec")).not_to_contain_text("blocks")
    expect(page.locator("#codec")).not_to_contain_text("Retry")
    bridge.handle_playback_metadata("arst", b"0/0/0", Mock(), {})
    page.evaluate("value => window.testLogs.status(value)", bridge.status_snapshot())
    expect(rows.filter(has=page.locator("dt", has_text="Missing audio blocks (session)")).locator("dd")).to_have_text("0")


def test_missing_receiver_counters_are_unavailable(bridge_page, bridge):
    page = bridge_page
    value = snapshot(bridge, measured=False)
    value["audio"].pop("receiver_stats")
    page.evaluate("value => window.testLogs.status(value)", value)
    rows = page.locator("#details .detail-row")
    for label in ("Missing audio blocks (session)", "Too-late audio blocks (session)", "Retry requests (session)"):
        expect(rows.filter(has=page.locator("dt", has_text=label)).locator("dd")).to_have_text("Unavailable")
