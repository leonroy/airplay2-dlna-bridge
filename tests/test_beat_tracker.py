"""Tempo, beat position and loudness estimated from synthetic PCM."""
import math
import struct

import pytest


def click_track(bpm, seconds, rate=44100, offset=0.0):
    """Stereo S16_LE: a short decaying tone on every beat, over quiet noise-free silence."""
    samples = [0] * int(seconds * rate)
    period, length = 60 / bpm, int(0.05 * rate)
    beat = offset
    while beat < seconds:
        start = int(beat * rate)
        for index in range(min(length, len(samples) - start)):
            samples[start + index] = int(24000 * math.exp(-index / (0.012 * rate)) * math.sin(index * 0.3))
        beat += period
    return struct.pack(f"<{len(samples) * 2}h", *[value for sample in samples for value in (sample, sample)])


def feed(tracker, data, chunk=8192):
    for start in range(0, len(data), chunk):
        tracker.feed(data[start:start + chunk])


@pytest.mark.parametrize("bpm", [75, 96, 120, 140])
def test_tempo_matches_click_track(bridge, bpm):
    tracker = bridge.BeatTracker(bridge.PCMFormat(44100, "S16_LE", 2))
    feed(tracker, click_track(bpm, 12))
    pulse = tracker.snapshot(tracker.fed_at)
    assert pulse["bpm"] == pytest.approx(bpm, rel=0.02)
    assert pulse["confidence"] > 0.3


def test_very_fast_music_settles_at_half_time(bridge):
    # Deliberate: a calm pulse suits 174 BPM better than a near-strobe at full speed.
    tracker = bridge.BeatTracker(bridge.PCMFormat(44100, "S16_LE", 2))
    feed(tracker, click_track(174, 12))
    assert tracker.snapshot(tracker.fed_at)["bpm"] == pytest.approx(87, rel=0.02)


@pytest.mark.parametrize("offset", [0.0, 0.11, 0.27, 0.41])
def test_next_beat_lands_on_the_real_beat(bridge, offset):
    bpm, seconds = 120, 12.0
    tracker = bridge.BeatTracker(bridge.PCMFormat(44100, "S16_LE", 2))
    data = click_track(bpm, seconds, offset=offset)
    feed(tracker, data)
    pulse = tracker.snapshot(tracker.fed_at)
    fed = tracker.hops * tracker.hop_seconds
    distance = (fed + pulse["next_beat_in"] - offset) % 0.5
    assert min(distance, 0.5 - distance) < 0.03


def test_next_beat_counts_down_between_feeds(bridge):
    tracker = bridge.BeatTracker(bridge.PCMFormat(44100, "S16_LE", 2))
    feed(tracker, click_track(120, 12))
    first = tracker.snapshot(tracker.fed_at)["next_beat_in"]
    later = tracker.snapshot(tracker.fed_at + 0.1)["next_beat_in"]
    assert (first - later) % 0.5 == pytest.approx(0.1, abs=0.02)


def test_playback_delay_moves_next_beat_later(bridge):
    tracker = bridge.BeatTracker(bridge.PCMFormat(44100, "S16_LE", 2))
    feed(tracker, click_track(120, 12))
    now = tracker.fed_at
    base = tracker.snapshot(now)["next_beat_in"]
    delayed = tracker.snapshot(now, delay=0.1)["next_beat_in"]
    assert (delayed - base) % 0.5 == pytest.approx(0.1, abs=0.01)
    assert bridge.PULSE_DELAY == 5.0


def test_no_pulse_until_enough_audio_or_for_silence(bridge):
    pcm = bridge.PCMFormat(44100, "S16_LE", 2)
    tracker = bridge.BeatTracker(pcm)
    assert tracker.snapshot() is None
    feed(tracker, click_track(120, 3))
    assert tracker.snapshot() is None
    silent = bridge.BeatTracker(pcm)
    feed(silent, bytes(44100 * 4 * 8))
    assert silent.snapshot() is None
    steady = bridge.BeatTracker(pcm)
    feed(steady, struct.pack("<h", 12000) * 2 * 44100 * 8)
    assert steady.snapshot() is None


def test_pulse_stops_when_music_stops(bridge):
    tracker = bridge.BeatTracker(bridge.PCMFormat(44100, "S16_LE", 2))
    feed(tracker, click_track(120, 12))
    assert tracker.snapshot(tracker.fed_at)
    feed(tracker, bytes(44100 * 4 * 12))
    assert tracker.snapshot(tracker.fed_at) is None


@pytest.mark.parametrize("name,rate,channels", [
    ("U8", 44100, 2), ("S16_LE", 48000, 1), ("S24_3LE", 44100, 2), ("S24_3LE", 96000, 1), ("S32_LE", 48000, 2),
])
def test_packed_formats_and_rates_give_the_same_tempo(bridge, name, rate, channels):
    pcm = bridge.PCMFormat(rate, name, channels)
    tracker = bridge.BeatTracker(pcm)
    source = struct.unpack(f"<{rate * 12 * 2}h", click_track(120, 12, rate=rate))[::2]
    width = pcm.bits // 8
    if width == 1:
        frames = bytes((value >> 8) + 128 for value in source for _ in range(channels))
    else:
        low = bytes(width - 2)
        frames = b"".join(low + struct.pack("<h", value) for value in source for _ in range(channels))
    feed(tracker, frames, chunk=pcm.frame * 1024)
    assert tracker.snapshot(tracker.fed_at)["bpm"] == pytest.approx(120, rel=0.02)


def test_session_exposes_pulse_and_clears_it(bridge):
    stream = bridge.AUDIO
    stream.begin()
    stream.describe(b"44100/S16_LE/2")
    assert bridge.status_snapshot()["audio"]["pulse"] is None
    feed(stream, click_track(120, 12))
    pulse = bridge.status_snapshot()["audio"]["pulse"]
    assert pulse["bpm"] == pytest.approx(120, rel=0.02)
    assert set(pulse) == {"bpm", "confidence", "next_beat_in", "level"}
    stream.end()
    assert stream.beat is None
    assert bridge.status_snapshot()["audio"]["pulse"] is None


def test_analysis_failure_cannot_interrupt_audio(bridge):
    stream = bridge.AUDIO
    stream.begin()
    stream.describe(b"44100/S16_LE/2")

    def broken(_data):
        raise RuntimeError("analysis failed")

    stream.beat.feed = broken
    stream.feed(bytes(4096))
    assert stream.beat is None
    assert stream.pcm_bytes == 4096
    stream.feed(bytes(4096))
    assert stream.pcm_bytes == 8192
    stream.end()
