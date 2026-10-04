"""PCM descriptions, frame normalization, WAV headers and codec round trips."""
import shutil
import struct
import subprocess

import pytest

from audio_helpers import FORMATS, samples


@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize("rate", (44100, 48000))
def test_description_and_header(bridge, format, rate):
    pcm = bridge.PCMFormat.from_description(f"{rate}/{format}/2".encode())
    storage, bits = FORMATS[format]
    assert pcm.source_frame == 2 * storage
    assert pcm.frame == 2 * bits // 8
    fields = struct.unpack("<IHHIIHH", pcm.wav_header()[16:36])
    assert fields == (16, 1, 2, rate, rate * pcm.frame, pcm.frame, bits)
    with pytest.raises(AttributeError):
        pcm.rate = 96000


@pytest.mark.parametrize("description", (
    b"", b"48000/S16_LE", b"48000/S16_LE/2/extra", b"48000/S16_LE/2\n",
    b"auto/S16_LE/2", b"48000/auto/2", b"48000/F24/2", b"48000/S64_LE/2",
    b"0/S16_LE/2", b"12345/S16_LE/2", b"48000/S16_LE/0", b"48000/S16_LE/8",
    b"48000/S16_LE/2\xff", None, 48000,
))
def test_bad_description_is_rejected(bridge, description):
    with pytest.raises(ValueError):
        bridge.PCMFormat.from_description(description)


@pytest.mark.parametrize("format", FORMATS)
def test_partial_input_reads_preserve_frames_and_values(bridge, format):
    source, expected = samples(format, frames=32)
    audio = bridge.AudioStream()
    audio.begin()
    # Audio and metadata use separate pipes, so first audio can arrive before odsc.
    audio.feed(source[:7])
    assert audio.snapshot() is None
    audio.describe(f"48000/{format}/2")
    for start in range(7, len(source), 17):
        audio.feed(source[start:start + 17])
    _, received = audio.snapshot().read_from(0)
    assert received == expected
    assert not audio.pending


@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize("rate", (44100, 48000))
@pytest.mark.integration
def test_every_encoder_preserves_samples(bridge, format, rate):
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "FFmpeg is required for PCM integration tests"
    pcm = bridge.PCMFormat.from_description(f"{rate}/{format}/2")
    source, expected = samples(format)
    audio = bridge.AudioStream()
    audio.begin()
    audio.feed(source)
    audio.describe(f"{rate}/{format}/2")
    _, canonical = audio.snapshot().read_from(0)
    command = pcm.encoder_command()
    command[0] = ffmpeg
    encoded = subprocess.run(command, input=canonical, capture_output=True,
                             check=True, timeout=15)
    decoded = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", pcm.ffmpeg_format, "-c:a", "pcm_" + pcm.ffmpeg_format, "pipe:1"],
        input=encoded.stdout, capture_output=True, check=True, timeout=15,
    )
    assert decoded.stdout == expected
    # Exercise the actual WAV header and payload together, including BE/padded inputs.
    wav_decoded = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", pcm.ffmpeg_format, "-c:a", "pcm_" + pcm.ffmpeg_format, "pipe:1"],
        input=pcm.wav_header() + canonical, capture_output=True, check=True, timeout=15,
    )
    assert wav_decoded.stdout == expected
