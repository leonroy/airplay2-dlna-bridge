"""Exercise the real log UI with synthetic events and an isolated browser."""
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
def log_page(browser, bridge):
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
    page.get_by_role("button", name="Open menu").click()
    page.get_by_role("button", name="Live logs", exact=True).click()
    expect(page.locator("#log-state")).to_have_text("Live")
    yield page
    context.close()
    assert errors == []


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
