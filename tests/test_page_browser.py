"""The redesigned status page: quality tags, states, pulse, logs, resting and panel behaviour."""
from datetime import datetime, timedelta

import pytest

from test_log_browser import (browser, bridge_page, expect, playing_status, log_page, send, open_panel,
                              close_panel, settled, next_frames, emit, bottom_distance)

TITLE = "Once Upon a Time In the West"


def tags(page):
    return [text.strip() for text in page.locator("#tags .tag").all_text_contents()]


# ------------------------------------------------------------------ quality tags
@pytest.mark.parametrize("source,codec,bitrate,expected,spec,title", [
    ("ALAC/44100/S16_LE/2", "ALAC", None, ["Lossless"], "16-bit / 44.1 kHz", "Lossless"),
    ("ALAC/96000/S24_LE/2", "ALAC", None, ["Hi-Res Lossless"], "24-bit / 96 kHz", "Hi-Res Lossless"),
    ("ALAC/48000/S24_LE/2", "ALAC", None, ["Lossless"], "24-bit / 48 kHz", "Lossless"),
    ("AAC/48000/F24/2", "AAC", 256000, ["AAC"], "256 kbps · 48 kHz", "AAC"),
    ("AAC/48000/F24/5.1", "AAC", 256000, ["5.1", "AAC"], "256 kbps · 48 kHz", "AAC"),
    ("PCM/44100/S16_LE/2", "PCM", None, ["PCM"], "16-bit / 44.1 kHz", "PCM"),
    (None, None, None, [], "Unknown format", "Unknown format"),
    ("garbage", "AAC", 256000, [], "Unknown format", "Unknown format"),
])
def test_quality_tags_describe_what_the_sender_delivered(bridge_page, bridge, source, codec, bitrate, expected, spec, title):
    page = bridge_page
    data = playing_status(bridge)  # the bridge always outputs FLAC
    data["audio"].update(source_format=source, codec=codec, input_bitrate_bps=bitrate)
    send(page, data)
    expect(page.locator("#tags")).to_be_visible()
    expect(page.locator("#spec")).to_have_text(spec)
    expect(page.locator("#quality-title")).to_have_text(title)
    assert tags(page) == expected
    # FLAC output never makes lossy or unknown input look lossless.
    if codec != "ALAC" and codec != "PCM":
        assert "Lossless" not in page.locator("#tags").text_content()
        expect(page.locator("#quality-copy")).not_to_contain_text("lossless")


def test_multichannel_copy_says_it_is_mixed_down(bridge_page, bridge):
    page = bridge_page
    data = playing_status(bridge)
    data["audio"].update(source_format="AAC/48000/F24/5.1", input_bitrate_bps=256000)
    send(page, data)
    expect(page.locator("#quality-copy")).to_contain_text("mixed down to two channels")
    expect(page.locator("#quality-copy")).to_contain_text("Example Speaker")
    stereo = playing_status(bridge)
    send(page, stereo)
    expect(page.locator("#quality-copy")).not_to_contain_text("mixed down")


# ------------------------------------------------------------------ states
def paused(data):
    data["recipient"]["playback"]["value"] = "PAUSED_PLAYBACK"


def connection_error(data):
    data["recipient"]["playback"].update(error="connection")


def stale(data):
    data["recipient"]["playback"].update(stale=True)


def audio_error(data):
    data["audio"]["state"] = "error"


def waiting(data):
    data["audio"]["state"] = "waiting"


def untitled(data):
    data["track"].update(title="", artist="", album="")


def unconfigured(data):
    data["recipient"]["configured"] = False


def idle(data):
    data["audio"]["state"] = "idle"
    data["track"].update(title="", artist="", album="", artwork=None)


@pytest.mark.parametrize("change,title,room,eyebrow", [
    (paused, TITLE, "Paused on Example Speaker", None),
    (connection_error, "Example Speaker isn’t responding", None, "Playback interrupted"),
    (stale, TITLE, "Example Speaker status is out of date", None),
    (audio_error, "Audio session failed", None, "Playback stopped"),
    (waiting, "Waiting for audio format", None, "Connected to AirPlay"),
    (untitled, "Track information unavailable", "Playing on Example Speaker", None),
    (unconfigured, TITLE, "Receiving AirPlay", None),
    (idle, "Ready for AirPlay", None, None),
])
def test_page_states(bridge_page, bridge, change, title, room, eyebrow):
    page = bridge_page
    data = playing_status(bridge)
    change(data)
    send(page, data)
    expect(page.locator("#title")).to_have_text(title)
    if room:
        expect(page.locator("#room-label")).to_have_text(room)
    if eyebrow:
        expect(page.locator("#eyebrow")).to_be_visible()
        expect(page.locator("#eyebrow")).to_contain_text(eyebrow)
    else:
        expect(page.locator("#eyebrow")).to_be_hidden()
    if change is unconfigured:
        expect(page.locator("#vol")).to_be_hidden()
        expect(page.locator("#volume")).to_be_empty()
    if change is idle:
        expect(page.locator("#tags")).to_be_hidden()
        expect(page.locator("#note")).to_have_text("Select the bridge from your AirPlay menu.")
    if change is untitled:
        expect(page.locator("#artist")).to_have_text("AirPlay audio")


def test_configured_speaker_shows_volume_and_stale_volume_hides_it(bridge_page, bridge):
    page = bridge_page
    data = playing_status(bridge)
    send(page, data)
    expect(page.locator("#vol")).to_be_visible()
    expect(page.locator("#volume")).to_have_text("25%")
    data["recipient"]["volume"]["stale"] = True
    send(page, data)
    expect(page.locator("#vol")).to_be_hidden()


# ------------------------------------------------------------------ pulse
PULSE = {"bpm": 120, "confidence": .6, "next_beat_in": .1, "level": .8}
BEAT = "parseFloat(getComputedStyle(document.getElementById('beat-light')).opacity)"
# Once the pulse has faded out the page clears its inline opacity entirely.
RESET = "document.getElementById('beat-light').style.opacity === ''"


def pulsing(bridge, pulse=PULSE):
    data = playing_status(bridge)
    data["audio"].update(input_bitrate_bps=256000, pulse=pulse)
    return data


def test_pulse_shows_tempo_and_lights_the_beat_then_settles(bridge_page, bridge):
    page = bridge_page
    data = pulsing(bridge)
    send(page, data)
    page.locator("#tags").click()
    expect(page.locator("#tempo-stat")).to_be_visible()
    expect(page.locator("#tempo-now")).to_have_text("120")
    page.wait_for_function(f"{BEAT} > 0", timeout=8000)
    data["audio"]["pulse"] = None
    send(page, data)
    expect(page.locator("#tempo-stat")).to_be_hidden()
    page.wait_for_function(RESET, timeout=8000)
    assert page.evaluate(BEAT) == 0


def test_pulse_stops_when_playback_pauses(bridge_page, bridge):
    page = bridge_page
    data = pulsing(bridge)
    send(page, data)
    page.wait_for_function(f"{BEAT} > 0", timeout=8000)
    paused(data)
    send(page, data)
    page.wait_for_function(RESET, timeout=8000)


def test_pulse_ignores_invalid_values(bridge_page, bridge):
    page = bridge_page
    send(page, pulsing(bridge, {"bpm": 10, "confidence": .6, "next_beat_in": .1, "level": .8}))
    page.locator("#tags").click()
    expect(page.locator("#tempo-stat")).to_be_hidden()
    next_frames(page, 20)
    assert page.evaluate(BEAT) == 0


def test_pulse_with_reduced_motion_does_not_animate(bridge_page, bridge):
    page = bridge_page
    page.emulate_media(reduced_motion="reduce")
    send(page, pulsing(bridge))
    page.locator("#tags").click()
    expect(page.locator("#tempo-stat")).to_be_visible()
    expect(page.locator("#tempo-now")).to_have_text("120")
    next_frames(page, 40)
    assert page.locator("#beat-light").evaluate("node => node.style.opacity") == ""
    assert page.evaluate(BEAT) == 0
    page.wait_for_function("document.getAnimations().length === 0")


# ------------------------------------------------------------------ untrusted text
def test_track_metadata_is_text_not_markup(bridge_page, bridge):
    page = bridge_page
    evil = "<img src=x onerror=window.__pwned=1>"
    data = playing_status(bridge, title=evil)
    data["track"].update(artist=evil.replace("x", "y"), album="<b>album</b>")
    send(page, data)
    expect(page.locator("#title")).to_have_text(evil)
    expect(page.locator("#artist")).to_have_text(evil.replace("x", "y"))
    expect(page.locator("#album")).to_contain_text("<b>album</b>")
    next_frames(page, 10)
    for selector in ("#title img", "#artist img", "#album b", "#room-label img", "img[src=x]", "img[src=y]", "[onerror]"):
        expect(page.locator(selector)).to_have_count(0)
    assert page.evaluate("window.__pwned") is None


# ------------------------------------------------------------------ logs
def emit_lines(page, start, texts):
    page.evaluate("entries => window.testLogs.emit(entries)",
                  [{"sequence": start + index, "line": text} for index, text in enumerate(texts)])
    expect(page.locator("#log-output > span").last).to_have_text(texts[-1])


SAMPLE = ["[bridge] alpha started", "[bridge] beta <b>bold</b> ready", "[bridge] gamma finished"]
SHOWN = "#log-output > span:not([hidden])"


def test_log_filter_hides_lines_counts_and_highlights_without_markup(log_page):
    page = log_page
    emit_lines(page, 1, SAMPLE)
    expect(page.locator("#log-count")).to_have_text("3 lines")
    page.locator("#log-filter").fill("BETA")
    expect(page.locator(SHOWN)).to_have_count(1)
    expect(page.locator("#log-output > span").filter(has_text="alpha")).to_be_hidden()
    expect(page.locator("#log-count")).to_have_text("1 of 3 lines")
    expect(page.locator("#log-output mark")).to_have_text("beta")
    page.locator("#log-filter").fill("<b>")
    expect(page.locator("#log-output mark")).to_have_text("<b>")
    expect(page.locator(SHOWN)).to_have_text("[bridge] beta <b>bold</b> ready")
    expect(page.locator("#log-output b")).to_have_count(0)
    page.locator("#log-filter").fill("nothing like this")
    expect(page.locator(SHOWN)).to_have_count(0)
    expect(page.locator("#log-empty")).to_be_visible()
    expect(page.locator("#log-count")).to_have_text("0 of 3 lines")
    page.locator("#log-filter").fill("")
    expect(page.locator(SHOWN)).to_have_count(3)
    expect(page.locator("#log-empty")).to_be_hidden()
    expect(page.locator("#log-count")).to_have_text("3 lines")


def test_escape_clears_the_filter_before_closing_the_panel(log_page):
    page = log_page
    emit_lines(page, 1, SAMPLE)
    page.locator("#log-filter").fill("beta")
    expect(page.locator(SHOWN)).to_have_count(1)
    page.keyboard.press("Escape")
    expect(page.locator("#log-filter")).to_have_value("")
    expect(page.locator(SHOWN)).to_have_count(3)
    expect(page.locator("#panel")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#panel")).to_be_hidden()


ISSUES = ["[bridge] renderer X failed: timed out", "[bridge] status failed=False ok", "[bridge] all good"]


def test_issue_count_ignores_failed_false_and_toggle_shows_only_issues(log_page):
    page = log_page
    emit_lines(page, 1, ISSUES)
    expect(page.locator("#log-issues")).to_have_text("Issues 1")
    expect(page.locator("#log-output > span.issue")).to_have_count(1)
    page.locator("#log-issues").click()
    expect(page.locator("#log-issues")).to_have_attribute("aria-pressed", "true")
    expect(page.locator(SHOWN)).to_have_text(ISSUES[0])
    expect(page.locator("#log-count")).to_have_text("1 of 3 lines")
    page.locator("#log-issues").click()
    expect(page.locator("#log-issues")).to_have_attribute("aria-pressed", "false")
    expect(page.locator(SHOWN)).to_have_count(3)


def test_copy_writes_only_the_filtered_lines(log_page):
    page = log_page
    page.evaluate("""() => {
      Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {writeText: async text => { window.copied = text; }}});
    }""")
    emit_lines(page, 1, ISSUES)
    page.locator("#log-filter").fill("failed")
    expect(page.locator(SHOWN)).to_have_count(2)
    page.locator("#log-copy").click()
    expect(page.locator("#log-copy")).to_have_text("Copied")
    assert page.evaluate("window.copied") == "\n".join(ISSUES[:2])
    expect(page.locator("#log-copy")).to_have_text("Copy")


def test_copy_works_on_plain_http_without_the_clipboard_api(log_page):
    page = log_page
    page.evaluate("""() => {
      Object.defineProperty(navigator, 'clipboard', {configurable: true, value: undefined});
      window.copied = null;
      const original = document.execCommand.bind(document);
      document.execCommand = (command) => { window.copied = document.activeElement.value; return command === 'copy'; };
    }""")
    emit_lines(page, 1, ISSUES)
    page.locator("#log-filter").fill("failed")
    expect(page.locator(SHOWN)).to_have_count(2)
    page.locator("#log-copy").click()
    expect(page.locator("#log-copy")).to_have_text("Copied")
    assert page.evaluate("window.copied") == "\n".join(ISSUES[:2])
    expect(page.locator("#log-copy")).to_be_focused()
    assert page.locator("textarea").count() == 0


def test_logs_open_following_the_newest_line(log_page):
    page = log_page
    emit(page, 1, 500)
    page.wait_for_function("document.querySelector('#log-output').scrollHeight - document.querySelector('#log-output').clientHeight - document.querySelector('#log-output').scrollTop <= 8")
    assert bottom_distance(page) <= 8
    expect(page.locator("#new-logs")).to_be_hidden()
    close_panel(page)
    open_panel(page, "Logs")
    page.wait_for_function("document.querySelector('#log-output').scrollHeight - document.querySelector('#log-output').clientHeight - document.querySelector('#log-output').scrollTop <= 8")
    expect(page.locator("#new-logs")).to_be_hidden()


def test_new_log_badge_counts_only_lines_that_pass_the_filter(log_page):
    page = log_page
    emit(page, 1, 400)
    page.locator("#log-filter").fill("sample")
    expect(page.locator("#log-count")).to_have_text("400 of 400 lines")
    output = page.locator("#log-output")
    output.focus()
    output.press("Home")
    page.wait_for_function("document.querySelector('#log-output').scrollTop === 0")
    emit_lines(page, 401, ["noise one", "sample two", "noise three", "sample four"])
    expect(page.locator("#new-logs")).to_have_text("2 new ↓")
    expect(page.locator("#log-count")).to_have_text("402 of 404 lines")
    # Revealing the lines that were filtered out does not retroactively count them.
    page.locator("#log-filter").fill("")
    expect(page.locator("#log-count")).to_have_text("404 lines")
    expect(page.locator("#new-logs")).to_have_text("2 new ↓")


# ------------------------------------------------------------------ resting
def start_clock(page):
    page.clock.install()
    page.clock.pause_at(datetime.now() + timedelta(seconds=1))


def play_and_settle(page, data):
    send(page, data)
    expect(page.locator("#title")).to_have_text(TITLE)
    expect(page.locator("#room-label")).to_have_text("Playing on Example Speaker")
    # No requestAnimationFrame here: the paused fake clock never fires it.
    page.evaluate("0")


def test_controls_rest_after_six_seconds_and_wake_on_input(bridge_page, bridge):
    page = bridge_page
    start_clock(page)
    play_and_settle(page, playing_status(bridge))
    stage = page.locator("#stage")
    expect(stage).not_to_have_class("stage rest")
    page.clock.run_for(5900)
    assert "rest" not in stage.get_attribute("class").split()
    page.clock.run_for(200)
    expect(stage).to_have_class("stage rest")
    page.mouse.move(120, 120)
    page.mouse.move(140, 130)
    assert "rest" not in stage.get_attribute("class").split()
    page.clock.run_for(6100)
    expect(stage).to_have_class("stage rest")


@pytest.mark.parametrize("change", [paused, stale, connection_error, audio_error, waiting])
def test_controls_never_rest_in_paused_or_problem_states(bridge_page, bridge, change):
    page = bridge_page
    start_clock(page)
    data = playing_status(bridge)
    play_and_settle(page, data)
    page.clock.run_for(6100)
    expect(page.locator("#stage")).to_have_class("stage rest")  # the control: it does rest while playing
    change(data)
    send(page, data)
    assert "rest" not in page.locator("#stage").get_attribute("class").split()
    page.clock.run_for(20000)
    assert "rest" not in page.locator("#stage").get_attribute("class").split()


def test_focus_inside_the_controls_keeps_them_awake(bridge_page, bridge):
    page = bridge_page
    start_clock(page)
    play_and_settle(page, playing_status(bridge))
    page.keyboard.press("Tab")
    expect(page.locator("#tags")).to_be_focused()
    page.clock.run_for(20000)
    assert "rest" not in page.locator("#stage").get_attribute("class").split()
    # Once focus leaves and the pointer wakes the page, it can rest again.
    page.evaluate("document.activeElement.blur()")
    page.mouse.move(200, 200)
    page.mouse.move(210, 205)
    page.clock.run_for(6100)
    expect(page.locator("#stage")).to_have_class("stage rest")


# ------------------------------------------------------------------ tabs
def pressed(page):
    return page.locator(".seg button").evaluate_all("buttons => buttons.map(b => b.getAttribute('aria-pressed'))")


def test_tabs_switch_views_and_only_logs_asks_for_log_events(bridge_page, bridge):
    page = bridge_page
    page.add_init_script("""(() => {
      const Base = window.EventSource;
      window.eventUrls = [];
      window.EventSource = class extends Base { constructor(url) { window.eventUrls.push(url); super(url); } };
    })()""")
    page.reload()
    send(page, playing_status(bridge))
    expect(page.locator("#title")).to_have_text(TITLE)
    assert page.evaluate("window.eventUrls") == ["/api/events"]
    assert pressed(page) == ["false", "false", "false"]
    expect(page.locator("#panel")).to_be_hidden()

    page.get_by_role("button", name="Show details", exact=True).click()
    expect(page.locator("#panel")).to_be_visible()
    expect(page.locator("#details-view")).to_be_visible()
    assert pressed(page) == ["false", "true", "false"]
    seg = page.locator(".seg")
    seg.get_by_role("button", name="Audio", exact=True).click()
    expect(page.locator("#audio-view")).to_be_visible()
    expect(page.locator("#details-view")).to_be_hidden()
    assert pressed(page) == ["true", "false", "false"]
    assert page.evaluate("window.eventUrls") == ["/api/events"]

    seg.get_by_role("button", name="Logs", exact=True).click()
    expect(page.locator("#logs-view")).to_be_visible()
    expect(page.locator("#audio-view")).to_be_hidden()
    assert pressed(page) == ["false", "false", "true"]
    expect(page.locator("#log-state")).to_have_text("Live")
    assert page.evaluate("window.eventUrls") == ["/api/events", "/api/events?logs=1"]

    seg.get_by_role("button", name="Connection", exact=True).click()
    expect(page.locator("#details-view")).to_be_visible()
    expect(page.locator("#logs-view")).to_be_hidden()
    assert pressed(page) == ["false", "true", "false"]
    assert page.evaluate("window.eventUrls") == ["/api/events", "/api/events?logs=1", "/api/events"]

    seg.get_by_role("button", name="Logs", exact=True).click()
    close_panel(page)
    assert page.evaluate("window.eventUrls") == ["/api/events", "/api/events?logs=1", "/api/events", "/api/events?logs=1", "/api/events"]
    # Opening again starts from the opener's own tab, not the one left behind.
    page.get_by_role("button", name="Show details", exact=True).click()
    expect(page.locator("#details-view")).to_be_visible()
    assert pressed(page) == ["false", "true", "false"]


def test_closing_returns_focus_to_whatever_opened_the_panel(bridge_page, bridge):
    page = bridge_page
    send(page, playing_status(bridge))
    expect(page.locator("#tags")).to_be_visible()
    page.locator("#tags").click()
    expect(page.locator("#audio-view")).to_be_visible()
    assert pressed(page) == ["true", "false", "false"]
    page.get_by_role("button", name="Close panel").click()
    expect(page.locator("#panel")).to_be_hidden()
    expect(page.locator("#tags")).to_be_focused()

    page.locator("#more").click()
    expect(page.locator("#details-view")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#panel")).to_be_hidden()
    expect(page.locator("#more")).to_be_focused()

    # Switching tabs inside the panel does not change where focus returns.
    page.locator("#tags").click()
    page.locator(".seg").get_by_role("button", name="Connection", exact=True).click()
    page.keyboard.press("Escape")
    expect(page.locator("#panel")).to_be_hidden()
    expect(page.locator("#tags")).to_be_focused()


# ------------------------------------------------------------------ demo
@pytest.mark.parametrize("size", [(1440, 900), (390, 844)], ids=["desktop", "phone"])
@pytest.mark.parametrize("scenario,title", [
    ("playing", "Evening Drive"),
    ("idle", "Ready for AirPlay"),
    ("waiting", "Waiting for audio format"),
    ("failure", "Living Room isn’t responding"),
])
def test_demo_scenarios_load_and_fit(bridge_page, scenario, title, size):
    page = bridge_page
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.goto(f"http://bridge.test/?demo={scenario}")
    expect(page.locator("#demo-label")).to_be_visible()
    expect(page.locator("#title")).to_have_text(title)
    settled(page)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight")
    for selector in ("#title", "#room"):
        rect = page.locator(selector).bounding_box()
        assert rect["x"] >= 0 and rect["x"] + rect["width"] <= size[0], selector
        assert rect["y"] >= 0 and rect["y"] + rect["height"] <= size[1], selector
