"""Exercise the playback and log UI with synthetic events and an isolated browser."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

playwright = pytest.importorskip(
    "playwright.sync_api", reason="Install requirements-browser.txt to run browser tests"
)
expect = playwright.expect


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as runtime:
        options = {"headless": True}
        if executable := os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
            options["executable_path"] = executable
        instance = runtime.chromium.launch(**options)
        yield instance
        instance.close()


@pytest.fixture
def bridge_page(browser, bridge):
    context = browser.new_context(viewport={"width": 900, "height": 600})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    assets = Path(__file__).parents[1] / "bridge" / "web"
    types = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}

    def asset(route):
        path = urlsplit(route.request.url).path
        name = "index.html" if path == "/" else path.removeprefix("/")
        assert name in {"index.html", "app.css", "app.js", "placeholder.svg", "demo.svg"}
        route.fulfill(body=(assets / name).read_bytes(), content_type=types[Path(name).suffix])

    # No HTTP server, receiver, renderer, or real EventSource is contacted.
    page.route("**/*", asset)
    page.add_init_script("""
      (() => {
        const sources = [];
        window.EventSource = class extends EventTarget {
          constructor(url) {
            super(); this.url = url; this.closed = false; sources.push(this);
            queueMicrotask(() => {
              if (this.closed) return;
              this.dispatchEvent(new Event('open'));
              this.dispatchEvent(new MessageEvent('status', {data: JSON.stringify(STATUS)}));
            });
          }
          close() { this.closed = true; }
        };
        const current = () => sources.findLast(source => !source.closed && source.url.includes('logs=1'));
        window.testLogs = {
          status(value) {
            const source = sources.findLast(source => !source.closed);
            source.dispatchEvent(new MessageEvent('status', {data: JSON.stringify(value)}));
          },
          reconnect(value) {
            current().dispatchEvent(new Event('open'));
            this.status(value);
          },
          emit(entries) {
            const source = current();
            if (!source) throw new Error('No live log subscription');
            for (const entry of entries) source.dispatchEvent(new MessageEvent('log', {data: JSON.stringify(entry)}));
          },
          disconnect() { current().dispatchEvent(new Event('error')); }
        };
      })();
    """.replace("STATUS", json.dumps(bridge.status_snapshot())))
    page.goto("http://bridge.test/")
    yield page
    context.close()
    assert errors == []


@pytest.fixture
def log_page(bridge_page):
    page = bridge_page
    page.get_by_role("button", name="Open menu").click()
    page.get_by_role("button", name="Live logs", exact=True).click()
    expect(page.locator("#log-state")).to_have_text("Live")
    yield page


def emit(page, start, count):
    entries = [{"sequence": index, "line": f"sample entry {index:04d}"}
               for index in range(start, start + count)]
    page.evaluate("entries => window.testLogs.emit(entries)", entries)
    expect(page.locator("#log-output > span").last).to_have_text(entries[-1]["line"])


def bottom_distance(page):
    return page.locator("#log-output").evaluate(
        "node => node.scrollHeight - node.clientHeight - node.scrollTop"
    )


def anchor(page):
    return page.locator("#log-output").evaluate("""node => {
      const top = node.getBoundingClientRect().top + node.clientTop;
      const entry = Array.from(node.children).find(entry => entry.getBoundingClientRect().bottom > top);
      return {text: entry.textContent, offset: entry.getBoundingClientRect().top - top};
    }""")


def test_live_logs_preserve_reading_position_and_resume_via_badge(log_page):
    page = log_page
    emit(page, 1, 500)
    page.wait_for_function("document.querySelector('#log-output').scrollHeight - document.querySelector('#log-output').clientHeight - document.querySelector('#log-output').scrollTop <= 8")
    output = page.locator("#log-output")
    output.hover()
    page.mouse.wheel(0, -500)
    page.wait_for_function("document.querySelector('#log-output').scrollHeight - document.querySelector('#log-output').clientHeight - document.querySelector('#log-output').scrollTop > 8")
    before = anchor(page)
    expect(page.locator("#log-state")).not_to_have_class("log-pulse")

    # The oldest entries expire, but the reader's retained entry stays in place.
    emit(page, 501, 20)
    expect(page.locator("#new-logs")).to_have_text("20 new ↓")
    after = anchor(page)
    assert after["text"] == before["text"]
    assert abs(after["offset"] - before["offset"]) <= 1
    expect(page.locator("#log-output > span")).to_have_count(500)
    expect(page.locator("#log-state")).to_have_text("Live")
    assert page.locator("#log-state").evaluate("node => getComputedStyle(node).animationName") == "none"

    # Replayed events neither duplicate entries nor inflate the unread count.
    page.evaluate("window.testLogs.emit([{sequence: 520, line: 'duplicate'}])")
    expect(page.locator("#new-logs")).to_have_text("20 new ↓")
    page.get_by_role("button", name="Show 20 new log entries").click()
    expect(page.locator("#new-logs")).to_be_hidden()
    assert bottom_distance(page) <= 8
    assert page.locator("#log-state").evaluate("node => getComputedStyle(node).animationName") == "live-pulse"
    emit(page, 521, 1)
    assert bottom_distance(page) <= 8
    expect(page.locator("#new-logs")).to_be_hidden()


def test_expired_reading_anchor_stays_at_oldest_retained_entry(log_page):
    page = log_page
    emit(page, 1, 500)
    output = page.locator("#log-output")
    output.click()
    output.press("Home")
    page.wait_for_function("document.querySelector('#log-output').scrollTop === 0")
    emit(page, 501, 500)
    expect(page.locator("#log-output > span")).to_have_count(500)
    expect(page.locator("#log-output > span").first).to_have_text("sample entry 0501")
    assert output.evaluate("node => node.scrollTop") == 0
    expect(page.locator("#new-logs")).to_be_visible()


def test_live_indicator_pulses_without_entries_and_respects_reduced_motion(log_page):
    page = log_page
    state = page.locator("#log-state")
    expect(page.locator("#log-output > span")).to_have_count(0)
    assert state.evaluate("node => getComputedStyle(node).animationIterationCount") == "infinite"
    page.wait_for_function("document.querySelector('#log-state').getAnimations()[0].currentTime > 200")
    page.emulate_media(reduced_motion="reduce")
    assert state.evaluate("node => getComputedStyle(node).animationName") == "none"
    assert state.evaluate("node => getComputedStyle(node).color") == "rgb(224, 160, 154)"
    page.evaluate("window.testLogs.disconnect()")
    expect(state).to_have_text("Reconnecting…")
    assert state.evaluate("node => getComputedStyle(node).animationName") == "none"


def playing_status(bridge, artwork=None, title="Once Upon a Time In the West"):
    data = bridge.status_snapshot()
    data["audio"].update(state="receiving", codec="AAC", airplay_version=2,
                         format={"rate": 48000, "bits": 32, "channels": 2})
    data["track"].update(title=title, artist="Example Artist", album="Example Album", artwork=artwork)
    data["recipient"].update(configured=True, name="Example Speaker")
    for key, value in (("playback", "PLAYING"), ("volume", 25)):
        data["recipient"][key].update(value=value, at=1, error=None, stale=False)
    return data


def test_artwork_waits_for_load_then_fades_and_handles_failure(bridge_page, bridge):
    page = bridge_page
    pending = []
    page.route("**/art-1.jpg", lambda route: pending.append(route))
    page.evaluate("value => window.testLogs.status(value)", playing_status(bridge, "/art-1.jpg"))
    page.wait_for_function("document.querySelector('#artwork').getAttribute('src') === '/art-1.jpg'")
    assert pending
    expect(page.locator(".ambience img.visible")).to_have_attribute("src", "/placeholder.svg")
    for route in pending:
        route.fulfill(body='<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><path fill="red" d="M0 0h8v8H0z"/></svg>', content_type="image/svg+xml")
    expect(page.locator(".ambience img.visible")).to_have_attribute("src", "/art-1.jpg")
    page.wait_for_function("(() => { const opacity = Number(getComputedStyle(document.querySelector('.ambience img.visible')).opacity); return opacity > 0 && opacity < .3; })()")
    page.wait_for_function("getComputedStyle(document.querySelector('.ambience img.visible')).opacity === '0.3'")
    page.route("**/art-2.jpg", lambda route: route.fulfill(status=404))
    page.evaluate("value => window.testLogs.status(value)", playing_status(bridge, "/art-2.jpg"))
    expect(page.locator("#artwork")).to_have_attribute("src", "/placeholder.svg")
    expect(page.locator(".ambience img.visible")).to_have_attribute("src", "/placeholder.svg")
    expect(page.locator("#artwork")).to_have_attribute("alt", "No cover art")
    page.emulate_media(reduced_motion="reduce")
    assert page.locator(".ambience img.visible").evaluate("node => getComputedStyle(node).transitionDuration") == "0s"


def test_late_artwork_cannot_replace_a_newer_track(bridge_page, bridge):
    page = bridge_page
    pending = []
    page.route("**/art-1.jpg", lambda route: pending.append(route))
    page.evaluate("value => window.testLogs.status(value)", playing_status(bridge, "/art-1.jpg", "Old track"))
    page.wait_for_function("document.querySelector('#artwork').getAttribute('src') === '/art-1.jpg'")
    assert pending
    page.evaluate("value => window.testLogs.status(value)", playing_status(bridge, "/demo.svg", "New track"))
    expect(page.locator(".ambience img.visible")).to_have_attribute("src", "/demo.svg")
    for route in pending:
        route.fulfill(body=(Path(__file__).parents[1] / "bridge/web/demo.svg").read_bytes(), content_type="image/svg+xml")
    # Let the old response and the entire fade finish before checking the final result.
    page.wait_for_timeout(550)
    expect(page.locator(".ambience img.visible")).to_have_attribute("src", "/demo.svg")
    expect(page.locator("#artwork")).to_have_attribute("src", "/demo.svg")
    expect(page.locator("#title")).to_have_text("New track")


@pytest.mark.parametrize("width,height", [(868, 695), (390, 640), (868, 500)])
def test_small_windows_fit_track_metadata_and_popups(bridge_page, bridge, width, height):
    page = bridge_page
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate("value => window.testLogs.status(value)", playing_status(bridge, "/demo.svg"))
    expect(page.locator("#codec")).to_have_text("AirPlay 2 · AAC → FLAC")
    page.wait_for_function("document.documentElement.scrollHeight <= innerHeight && document.documentElement.scrollWidth <= innerWidth", timeout=5000)
    for selector in ("#title", "#artist", "#album", "#status", "#codec", "#format", "#volume", "footer"):
        rect = page.locator(selector).bounding_box()
        assert rect and rect["y"] >= 0 and rect["y"] + rect["height"] <= height
    for name in ("Connection details", "Live logs"):
        page.get_by_role("button", name="Open menu").click()
        page.get_by_role("button", name=name, exact=True).click()
        panel = page.locator("#panel")
        rect = panel.bounding_box()
        assert rect["x"] >= 0 and rect["y"] >= 0
        assert rect["x"] + rect["width"] <= width and rect["y"] + rect["height"] <= height
        assert panel.evaluate("node => node.scrollWidth <= node.clientWidth")
        page.get_by_role("button", name="Close panel").click()


def test_connection_recovers_without_duplicate_logs_and_resets_on_restart(log_page, bridge):
    page = log_page
    data = playing_status(bridge)
    page.evaluate("value => window.testLogs.status(value)", data)
    emit(page, 1, 2)
    page.evaluate("window.testLogs.disconnect()")
    expect(page.locator("#status-text")).to_contain_text("Reconnecting")
    expect(page.locator("#volume")).to_be_empty()
    expect(page.locator("#issue")).to_be_visible()
    data["track"]["title"] = "Recovered track"
    page.evaluate("value => window.testLogs.reconnect(value)", data)
    expect(page.locator("#title")).to_have_text("Recovered track")
    expect(page.locator("#issue")).to_be_hidden()
    expect(page.locator("#log-state")).to_have_class("log-pulse")
    emit(page, 1, 3)
    expect(page.locator("#log-output > span")).to_have_count(3)
    data["instance"] = "restarted-backend"
    page.evaluate("value => window.testLogs.status(value)", data)
    expect(page.locator("#log-output > span")).to_have_count(0)
    emit(page, 1, 1)
    expect(page.locator("#log-output > span")).to_have_count(1)


def test_keyboard_closes_popups_restores_focus_and_resumes_logs(log_page):
    page = log_page
    emit(page, 1, 500)
    output = page.locator("#log-output")
    output.focus()
    output.press("Home")
    page.wait_for_function("document.querySelector('#log-output').scrollTop === 0")
    emit(page, 501, 2)
    badge = page.get_by_role("button", name="Show 2 new log entries")
    badge.focus()
    badge.press("Enter")
    expect(badge).to_be_hidden()
    assert bottom_distance(page) <= 8
    page.keyboard.press("Escape")
    expect(page.locator("#panel")).not_to_be_visible()
    button = page.get_by_role("button", name="Open menu")
    expect(button).to_be_focused()
    button.press("Enter")
    expect(page.get_by_role("button", name="Connection details", exact=True)).to_be_focused()
    page.keyboard.press("Escape")
    expect(button).to_be_focused()
    expect(page.locator("#menu")).to_be_hidden()
    button.press("Enter")
    page.get_by_role("button", name="Connection details", exact=True).press("Enter")
    expect(page.locator("#panel")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#panel")).not_to_be_visible()
    expect(button).to_be_focused()
