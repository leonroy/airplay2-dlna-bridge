"""Page-lifetime measured bitrate history and the audio view's layout."""
from copy import deepcopy
from datetime import datetime
import pytest
from test_log_browser import browser, bridge_page, expect, playing_status, log_page, open_panel, settled, send


@pytest.mark.parametrize("width", [320, 402, 599, 600, 874])
@pytest.mark.parametrize("source,bitrate", [("AAC/48000/F24/7.1", 256000), ("ALAC/44100/S16_LE/2", 882000), ("ALAC/48000/S24_LE/2", 1440000), (None, None)])
def test_audio_view_fits_every_width(bridge_page, bridge, width, source, bitrate):
    page = bridge_page
    page.set_viewport_size({"width": width, "height": 900})
    data = playing_status(bridge)
    data["audio"].update(source_format=source, codec="ALAC" if source and source.startswith("ALAC") else "AAC", input_bitrate_bps=bitrate)
    send(page, data)
    expect(page.locator("#tags")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    # The cover stack, lockup and capsule stay inside the window.
    for selector in ("#title", "#tags", "#room"):
        rect = page.locator(selector).bounding_box()
        assert rect["x"] >= 0 and rect["x"] + rect["width"] <= width, selector
    page.locator("#tags").click()
    expect(page.locator("#audio-view")).to_be_visible()
    settled(page)
    panel = page.locator("#panel")
    rect = panel.bounding_box()
    assert rect["x"] >= 0 and rect["x"] + rect["width"] <= width
    assert panel.evaluate("node => node.scrollWidth <= node.clientWidth")
    assert page.locator(".panel-body").evaluate("node => node.scrollWidth <= node.clientWidth")
    for selector in ("#codec", "#format", "#bitrate-foot"):
        if page.locator(selector).is_visible():
            box = page.locator(selector).bounding_box()
            assert box["x"] >= rect["x"] - 1 and box["x"] + box["width"] <= rect["x"] + rect["width"] + 1, selector
    # No width hides the chart; it follows whether a bitrate has been measured.
    assert page.locator("#bitrate-history").is_visible() == (bitrate is not None)
    if bitrate is not None:
        box = page.locator("#bitrate-history").bounding_box()
        assert box["width"] > 40 and box["x"] + box["width"] <= width


def test_history_cap_boundaries_gaps_age_and_reload(bridge_page, bridge):
    page = bridge_page
    page.clock.install(time=datetime(2026, 1, 1))
    page.clock.pause_at(datetime(2026, 1, 1))
    data = playing_status(bridge)
    for i in range(35):
        data["audio"].update(input_bitrate_bps=(200 + i) * 1000)
        send(page, data)
        page.clock.run_for(2000)
    chart = page.locator("#bitrate-history")
    expect(chart).to_have_attribute("data-count", "30")
    foot = page.locator("#bitrate-foot")
    expect(foot).to_contain_text("Min 205 kbps")
    expect(foot).to_contain_text("Max 234 kbps")
    expect(foot).to_contain_text("History 0m 58s")
    expect(page.locator("#bitrate-now")).to_have_text("234")
    for state in ("idle", "waiting", "error"):
        data["audio"]["state"] = state
        send(page, data)
    page.clock.run_for(120000)
    expect(chart).to_have_attribute("data-count", "30")
    expect(chart).to_have_attribute("data-stale", "true")
    expect(foot).to_contain_text("(old)")
    data["audio"].update(state="receiving", session=99, source_format="ALAC/48000/S24_LE/2", codec="ALAC")
    data["instance"] = "new-instance"
    data["track"]["title"] = "Next track"
    send(page, data)
    expect(chart).to_have_attribute("data-count", "30")
    expect(foot).to_contain_text("History 2m")
    # The chart shows a fixed 60 seconds, so readings from before the long
    # pause have scrolled out of view although they are still retained.
    assert page.locator("#bitrate-line").get_attribute("d").count("M") == 1
    data["audio"]["input_bitrate_bps"] = None
    send(page, data)
    expect(chart).to_have_attribute("data-count", "30")
    page.reload()
    expect(chart).to_have_attribute("data-count", "0")


def test_history_breaks_the_line_at_session_boundaries_and_time_gaps(bridge_page, bridge):
    page = bridge_page
    page.clock.install(time=datetime(2026, 1, 1))
    page.clock.pause_at(datetime(2026, 1, 1))
    data = playing_status(bridge)
    data["audio"]["input_bitrate_bps"] = 256000
    for i in range(3):
        data["audio"]["input_bitrate_bps"] = (250 + i) * 1000
        send(page, data)
        page.clock.run_for(2000)
    chart = page.locator("#bitrate-history")
    expect(chart).to_have_attribute("data-count", "3")
    line = page.locator("#bitrate-line")
    assert line.get_attribute("d").count("M") == 1
    # A new session starts a new run of the line.
    data["audio"]["session"] = 2
    send(page, data)
    page.clock.run_for(2000)
    data["audio"]["input_bitrate_bps"] = 260000
    send(page, data)
    expect(chart).to_have_attribute("data-count", "5")
    assert line.get_attribute("d").count("M") == 2
    # So does a silence of more than five seconds within the same session.
    page.clock.run_for(8000)
    data["audio"]["input_bitrate_bps"] = 270000
    send(page, data)
    expect(chart).to_have_attribute("data-count", "6")
    assert line.get_attribute("d").count("M") == 3


def test_connection_interruptions_and_invalid_values_do_not_erase_history(log_page, bridge):
    page = log_page
    data = playing_status(bridge)
    data["audio"]["input_bitrate_bps"] = 256000
    send(page, data)
    page.evaluate("window.testLogs.disconnect()")
    page.evaluate("value => window.testLogs.reconnect(value)", data)
    chart = page.locator("#bitrate-history")
    expect(chart).to_have_attribute("data-count", "2")
    assert page.locator("#bitrate-line").get_attribute("d").count("M") == 2
    for invalid in (None, 0, -1, "256000"):
        data["audio"]["input_bitrate_bps"] = invalid
        send(page, data)
    expect(chart).to_have_attribute("data-count", "2")
    data["audio"].update(codec="PCM", input_bitrate_bps=256000)
    send(page, data)
    expect(chart).to_have_attribute("data-count", "2")


def test_demo_builds_live_history_and_centres_flat_readings(bridge_page):
    page = bridge_page
    page.clock.install()
    page.goto("http://bridge.test/?demo=playing")
    chart = page.locator("#bitrate-history")
    expect(chart).to_have_attribute("data-count", "1")
    # One reading is flat: the line is a dot and the marker sits mid-height.
    assert " L" in page.locator("#bitrate-line").get_attribute("d")
    assert page.locator("#trace-dot").evaluate("node => node.style.getPropertyValue('--y')") == "50.0%"
    expect(page.locator("#codec")).to_have_text("AAC 5.1 · 48 kHz · 256 kbps")
    expect(page.locator("#format")).to_have_text("FLAC Stereo · 48 kHz · 32-bit")
    page.clock.run_for(6000)
    expect(chart).to_have_attribute("data-count", "4")
    expect(chart).to_have_attribute("data-stale", "false")
    path = page.locator("#bitrate-line").get_attribute("d")
    assert path.count("M") == 1 and "C" in path


@pytest.mark.parametrize("width", [320, 402, 599, 600, 874])
def test_idle_and_playing_transitions_preserve_history_without_stray_graph(bridge_page, bridge, width):
    page = bridge_page
    page.set_viewport_size({"width": width, "height": 900})
    chart = page.locator("#bitrate-history")
    tags = page.locator("#tags")
    expect(page.locator("#title")).to_have_text("Ready for AirPlay")
    expect(page.locator("#eyebrow")).to_be_hidden()
    expect(tags).to_be_hidden()
    expect(page.locator("#bitrate-chart")).to_be_hidden()
    expect(chart).to_have_attribute("data-count", "0")

    playing = playing_status(bridge)
    send(page, playing)
    expect(tags).to_be_visible()
    expect(page.locator("#room-label")).to_have_text("Playing on Example Speaker")
    expect(page.locator("#volume")).to_have_text("25%")
    # The chart lives in the Audio view, and there is no reading to draw yet.
    open_panel(page, "Audio")
    expect(page.locator("#audio-view")).to_be_visible()
    expect(page.locator("#bitrate-chart")).to_be_hidden()
    playing["audio"]["input_bitrate_bps"] = 256000
    send(page, playing)
    expect(chart).to_have_attribute("data-count", "1")
    expect(page.locator("#bitrate-chart")).to_be_visible()
    expect(chart).to_be_visible()
    expect(page.locator("#bitrate-foot")).to_contain_text("Min 256 kbps · Max 256 kbps")
    idle = deepcopy(playing)
    idle["audio"]["state"] = "idle"
    idle["track"].update(title="", artist="", album="", artwork=None)
    send(page, idle)
    expect(page.locator("#title")).to_have_text("Ready for AirPlay")
    expect(page.locator("#eyebrow")).to_be_hidden()
    expect(tags).to_be_hidden()
    # While idle the chart is hidden but the history is kept.
    expect(page.locator("#bitrate-chart")).to_be_hidden()
    expect(chart).to_have_attribute("data-count", "1")
    expect(page.locator("#quality-title")).to_have_text("No audio yet")
    expect(page.locator("#codec")).to_have_text("")
    # The panel covers the capsule here, but the idle speaker volume is still kept in it.
    expect(page.locator("#vol:not([hidden]):not(.off)")).to_have_count(1)
    expect(page.locator("#volume")).to_have_text("25%")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    playing["audio"]["session"] = 2
    send(page, playing)
    expect(tags).to_be_visible()
    expect(page.locator("#room-label")).to_have_text("Playing on Example Speaker")
    expect(chart).to_have_attribute("data-count", "2")
    assert page.locator("#bitrate-line").get_attribute("d").count("M") == 2
    expect(page.locator("#bitrate-chart")).to_be_visible()
    expect(chart).to_be_visible()


def test_idle_connection_failure_remains_visible(log_page, bridge):
    page = log_page
    idle = bridge.status_snapshot()
    send(page, idle)
    expect(page.locator("#eyebrow")).to_be_hidden()
    expect(page.locator("#title")).to_have_text("Ready for AirPlay")
    page.evaluate("window.testLogs.disconnect()")
    expect(page.locator("#eyebrow")).to_be_visible()
    expect(page.locator("#eyebrow")).to_contain_text("Reconnecting")
    expect(page.locator("#title")).to_have_text("Can’t reach the bridge")
    page.evaluate("value => window.testLogs.reconnect(value)", idle)
    expect(page.locator("#eyebrow")).to_be_hidden()
    expect(page.locator("#title")).to_have_text("Ready for AirPlay")
    expect(page.locator("#tags")).to_be_hidden()
