"""Live encoder startup and independent lossless mono/stereo coverage."""
import pytest

from audio_helpers import FORMATS
from flac_startup import first_audio_offset, measure_startup, roundtrip


@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize("rate", (44100, 48000))
@pytest.mark.parametrize("channels", (1, 2))
@pytest.mark.integration
def test_encoder_exact_roundtrip(bridge, format, rate, channels):
    roundtrip(bridge.PCMFormat(rate, format, channels))


@pytest.mark.parametrize("rate,format", ((44100, "S16_LE"), (48000, "S32_LE")))
@pytest.mark.integration
def test_first_audio_frame_is_emitted_before_input_eof(bridge, rate, format):
    # This is a live-stream liveness check, not a performance target; older
    # FFmpeg releases can spend several seconds probing the open PCM input.
    assert measure_startup(bridge.PCMFormat(rate, format, 2), timeout=8) < 8


def test_flac_header_is_not_counted_as_audio():
    # One final metadata block with three payload bytes, then frame sync.
    stream = b"fLaC\x80\x00\x00\x03abc\xff\xf8"
    for length in range(len(stream)):
        assert first_audio_offset(stream[:length]) is None
    assert first_audio_offset(stream) == 11
