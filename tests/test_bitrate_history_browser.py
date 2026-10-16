"""Aligned stats and page-lifetime measured bitrate history."""
from copy import deepcopy
from datetime import datetime
import pytest
from test_log_browser import browser, bridge_page, expect, playing_status, log_page


def send(page, data):
    page.evaluate("value => window.testLogs.status(value)", data)


@pytest.mark.parametrize("width", [320, 402, 599, 600, 874])
@pytest.mark.parametrize("source,bitrate", [("AAC/48000/F24/7.1", 256000), ("ALAC/44100/S16_LE/2", 882000), ("ALAC/48000/S24_LE/2", 1440000), (None, None)])
def test_stats_columns_fit_and_align(bridge_page, bridge, width, source, bitrate):
    page = bridge_page
    page.set_viewport_size({"width": width, "height": 900})
    data = playing_status(bridge)
    data["audio"].update(source_format=source, codec="ALAC" if source and source.startswith("ALAC") else "AAC", input_bitrate_bps=bitrate)
    send(page, data)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for selector in ("#codec", "#format"):
        assert page.locator(selector).evaluate("node => [...node.querySelectorAll('.format-cell')].every(cell => Math.abs((cell.getBoundingClientRect().y + cell.getBoundingClientRect().height / 2) - (node.getBoundingClientRect().y + node.getBoundingClientRect().height / 2)) < 1)")
        rect = page.locator(selector).bounding_box()
        assert rect["x"] >= 0 and rect["x"] + rect["width"] <= width
    if source:
        for field in range(4):
            a = page.locator(f"#codec .field-{field}").bounding_box()
            b = page.locator(f"#format .field-{field}").bounding_box()
            assert a["x"] == b["x"]
    # The value centre must bisect the two adjacent separator centres.
    if source:
        for selector in ("#codec", "#format"):
            for field in (1, 2, 3):
                cell = page.locator(f"{selector} .field-{field}")
                if not cell.locator("xpath=following-sibling::span[contains(@class, 'format-separator')][1]").count():
                    continue
                centres = cell.evaluate("""node => {
                  const centre = element => {const r = element.getBoundingClientRect(); return r.x + r.width / 2;};
                  return [centre(node.firstElementChild), (centre(node.previousElementSibling) + centre(node.nextElementSibling)) / 2];
                }""")
                assert abs(centres[0] - centres[1]) < 1
            assert page.locator(selector).evaluate("node => [...node.querySelectorAll('.format-cell > span')].every(value => {const a = value.getBoundingClientRect(), b = value.parentElement.getBoundingClientRect(); return a.left >= b.left - 1 && a.right <= b.right + 1;})")
        incoming = page.locator("#codec .format-separator").all()
        outgoing = page.locator("#format .format-separator").all()
        for a, b in zip(incoming, outgoing):
            assert a.bounding_box()["x"] == b.bounding_box()["x"]
    assert page.locator("#bitrate-history").is_visible() == (width >= 600 and bitrate is not None)


def test_history_cap_boundaries_gaps_age_and_reload(bridge_page, bridge):
    page = bridge_page
    page.clock.install(time=datetime(2026, 1, 1))
    page.clock.pause_at(datetime(2026, 1, 1))
    data = playing_status(bridge)
    for i in range(35):
        data["audio"].update(input_bitrate_bps=(200 + i) * 1000)
        send(page, data)
        page.clock.run_for(2000)
    button = page.locator("#bitrate-history")
    expect(button).to_have_attribute("data-count", "30")
    button.click()
    expect(page.locator("#bitrate-popover")).to_contain_text("Min 205 kbps")
    expect(page.locator("#bitrate-popover")).to_contain_text("History 0m 58s")
    for state in ("idle", "waiting", "error"):
        data["audio"]["state"] = state
        send(page, data)
    page.clock.run_for(120000)
    expect(button).to_have_attribute("data-count", "30")
    expect(button).to_have_attribute("data-stale", "true")
    data["audio"].update(state="receiving", session=99, source_format="ALAC/48000/S24_LE/2", codec="ALAC")
    data["instance"] = "new-instance"
    data["track"]["title"] = "Next track"
    send(page, data)
    expect(button).to_have_attribute("data-count", "30")
    assert page.locator("#bitrate-line").get_attribute("d").count("M") == 2
    expect(page.locator("#bitrate-popover")).to_contain_text("History 2m")
    data["audio"]["input_bitrate_bps"] = None
    send(page, data)
    expect(button).to_have_attribute("data-count", "30")
    page.reload()
    expect(button).to_have_attribute("data-count", "0")


def test_history_mouse_keyboard_and_touch(bridge_page, bridge, browser):
    page = bridge_page
    data = playing_status(bridge)
    data["audio"]["input_bitrate_bps"] = 256000
    send(page, data)
    button = page.locator("#bitrate-history")
    popup = page.locator("#bitrate-popover")
    button.hover()
    expect(popup).to_be_visible()
    page.keyboard.press("Escape")
    expect(popup).to_be_hidden()
    page.mouse.move(0, 0)
    button.focus()
    page.keyboard.press("Tab")
    page.keyboard.press("Shift+Tab")
    expect(popup).to_be_visible()
    page.keyboard.press("Escape")
    expect(popup).to_be_hidden()
    page.emulate_media(reduced_motion="reduce")
    # Enable touch on the same isolated fixture through Chromium's emulation.
    session = page.context.new_cdp_session(page)
    session.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
    def tap(locator):
        box = locator.bounding_box()
        session.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2}]})
        session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.set_viewport_size({"width": 820, "height": 1180})
    tap(button)
    expect(popup).to_be_visible()
    tap(button)
    expect(popup).to_be_hidden()
    tap(button)
    tap(page.locator("#title"))
    expect(popup).to_be_hidden()


def test_connection_interruptions_and_invalid_values_do_not_erase_history(log_page, bridge):
    page = log_page
    data = playing_status(bridge)
    data["audio"]["input_bitrate_bps"] = 256000
    send(page, data)
    page.evaluate("window.testLogs.disconnect()")
    page.evaluate("value => window.testLogs.reconnect(value)", data)
    button = page.locator("#bitrate-history")
    expect(button).to_have_attribute("data-count", "2")
    assert page.locator("#bitrate-line").get_attribute("d").count("M") == 2
    for invalid in (None, 0, -1, "256000"):
        data["audio"]["input_bitrate_bps"] = invalid
        send(page, data)
    expect(button).to_have_attribute("data-count", "2")
    data["audio"].update(codec="PCM", input_bitrate_bps=256000)
    send(page, data)
    expect(button).to_have_attribute("data-count", "2")


def test_demo_builds_live_history_and_centres_flat_readings(bridge_page):
    page = bridge_page
    page.clock.install()
    page.goto("http://bridge.test/?demo=playing")
    button = page.locator("#bitrate-history")
    expect(button).to_have_attribute("data-count", "1")
    expect(page.locator("#bitrate-dot")).to_have_attribute("cy", "13")
    expect(page.locator("#codec .field-3")).to_have_text("—")
    assert page.locator("#codec .field-3 > span").last.evaluate("node => getComputedStyle(node).color") == page.locator("#format .field-3 > span").last.evaluate("node => getComputedStyle(node).color")
    page.clock.run_for(6000)
    expect(button).to_have_attribute("data-count", "4")
    expect(button).to_have_attribute("data-stale", "false")
    assert "L" in page.locator("#bitrate-line").get_attribute("d")


@pytest.mark.parametrize("width", [320, 402, 599, 600, 874])
def test_idle_and_playing_transitions_preserve_history_without_stray_graph(bridge_page, bridge, width):
    page = bridge_page
    page.set_viewport_size({"width": width, "height": 900})
    button = page.locator("#bitrate-history")
    stats = page.locator(".audio-formats")
    expect(page.locator("#title")).to_have_text("Ready for AirPlay")
    expect(page.locator("#status")).to_be_hidden()
    expect(stats).to_be_hidden()
    expect(button).to_be_hidden()
    expect(button).to_have_attribute("data-count", "0")

    playing = playing_status(bridge)
    send(page, playing)
    expect(stats).to_be_visible()
    expect(page.locator("#status")).to_be_visible()
    expect(button).to_be_hidden()
    playing["audio"]["input_bitrate_bps"] = 256000
    send(page, playing)
    expect(button).to_have_attribute("data-count", "1")
    if width >= 600:
        button.hover()
        expect(page.locator("#bitrate-popover")).to_be_visible()
    idle = deepcopy(playing)
    idle["audio"]["state"] = "idle"
    idle["track"].update(title="", artist="", album="", artwork=None)
    send(page, idle)
    expect(page.locator("#title")).to_have_text("Ready for AirPlay")
    expect(page.locator("#status")).to_be_hidden()
    expect(stats).to_be_hidden()
    expect(button).to_have_attribute("data-count", "1")
    expect(page.locator("#bitrate-popover")).to_be_hidden()
    expect(button).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#volume")).to_have_text("Volume · 25%")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.mouse.move(0, 0)
    playing["audio"]["session"] = 2
    send(page, playing)
    expect(stats).to_be_visible()
    expect(page.locator("#status-text")).to_have_text("AirPlay 2 · Playing")
    expect(button).to_have_attribute("data-count", "2")
    assert page.locator("#bitrate-line").get_attribute("d").count("M") == 2
    assert button.is_visible() == (width >= 600)
    expect(page.locator("#bitrate-popover")).to_be_hidden()


def test_idle_connection_failure_remains_visible(log_page, bridge):
    page = log_page
    idle = bridge.status_snapshot()
    send(page, idle)
    expect(page.locator("#status")).to_be_hidden()
    page.evaluate("window.testLogs.disconnect()")
    expect(page.locator("#status")).to_be_visible()
    expect(page.locator("#status-text")).to_contain_text("Reconnecting")
    page.evaluate("value => window.testLogs.reconnect(value)", idle)
    expect(page.locator("#status")).to_be_hidden()
    expect(page.locator(".audio-formats")).to_be_hidden()
