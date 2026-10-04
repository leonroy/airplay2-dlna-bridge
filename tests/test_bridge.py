import ast
import math
from pathlib import Path
import shutil
import struct
import subprocess

import pytest


def test_buffer_preserves_irregular_chunks(bridge):
    ring = bridge.Ring()
    source = bytes(range(256)) * 8
    received = bytearray()
    position = 0
    for start in range(0, len(source), 17):
        ring.write(source[start : start + 17])
        position, data = ring.read_from(position)
        received.extend(data)
    assert received == source


def test_slow_reader_rejoins_on_frame_boundary(bridge):
    bridge.RING_MAX = 64
    bridge.CLIENT_BACKLOG = 16
    ring = bridge.Ring()
    source = bytes(range(128))
    ring.write(source)
    end, data = ring.read_from(0)
    assert end == len(source)
    assert data == source[-16:]
    assert (end - len(data)) % bridge.FRAME == 0


def test_new_session_does_not_replay_old_buffer(bridge):
    ring = bridge.Ring()
    ring.write(b"old!" * 4)
    ring.mark_session()
    ring.write(b"new!" * 4)
    _, data = ring.read_from(ring.join_pos())
    assert data == b"new!" * 4


def test_wav_header_matches_pcm_format(bridge):
    header = bridge.wav_header()
    assert header[:4] == b"RIFF"
    assert header[8:12] == b"WAVE"
    _, codec, channels, rate, byte_rate, frame_size, bits = struct.unpack(
        "<IHHIIHH", header[16:36]
    )
    assert (codec, channels, rate, bits) == (1, 2, 44100, 16)
    assert byte_rate == rate * frame_size
    assert frame_size == channels * bits // 8


@pytest.mark.integration
def test_actual_encoder_preserves_pcm_samples():
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "Install FFmpeg to run the integration tests"
    source_path = Path(__file__).parents[1] / "bridge" / "server.py"
    tree = ast.parse(source_path.read_text())
    # Exercise the command used by the application, rather than a parallel encoder.
    command = next(
        ast.literal_eval(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
    )
    command[0] = ffmpeg
    extremes = [-32768, -1, 0, 1, 32767]
    pcm = b"".join(struct.pack("<hh", value, -value - 1) for value in extremes)
    pcm += b"".join(
        struct.pack(
            "<hh",
            int(20000 * math.sin(2 * math.pi * 997 * index / 44100)),
            int(17000 * math.sin(2 * math.pi * 613 * index / 44100)),
        )
        for index in range(44100)
    )
    encoded = subprocess.run(command, input=pcm, capture_output=True, check=True, timeout=15)
    decoded = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1"],
        input=encoded.stdout, capture_output=True, check=True, timeout=15,
    )
    assert decoded.stdout == pcm
