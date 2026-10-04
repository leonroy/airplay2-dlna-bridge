"""Run inside the image using only its runtime dependencies."""
import ast
from pathlib import Path
import runpy
import struct
import subprocess

# The same program runs inside each published architecture's image and locally.
source = Path('/app/server.py')
if not source.exists():
    source = Path(__file__).resolve().parents[2] / 'bridge' / 'server.py'
bridge = runpy.run_path(str(source), run_name='smoke_test')
# The coordinator caches this program before checking out older releases.
# Keep their original import/round-trip check; do not require the new API.
if 'PCMFormat' not in bridge and 'AudioStream' not in bridge:
    tree = ast.parse(source.read_text())
    command = next(
        ast.literal_eval(node.args[0]) for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'Popen'
    )
    pcm = b''.join(struct.pack('<hh', n, -n - 1) for n in [-32768, -1, 0, 1, 32767]) * 4096
    encoded = subprocess.run(command, input=pcm, capture_output=True, check=True, timeout=30)
    decoded = subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', 'pipe:0',
         '-f', 's16le', '-c:a', 'pcm_s16le', 'pipe:1'],
        input=encoded.stdout, capture_output=True, check=True, timeout=30,
    )
    assert decoded.stdout == pcm, 'FLAC round trip changed PCM samples'
    print('Legacy bridge import and exact PCM/FLAC round trip passed')
    raise SystemExit(0)
PCMFormat, AudioStream = bridge['PCMFormat'], bridge['AudioStream']

for format, (storage, bits, byte_order) in PCMFormat.FORMATS.items():
    extremes = [-(1 << (bits - 1)), -1, 0, 1, (1 << (bits - 1)) - 1]
    raw, expected = bytearray(), bytearray()
    for value in extremes * 2048:
        if bits == 8:
            raw.append(value + 128 if format == 'U8' else value & 255)
            expected.append(value + 128)
        else:
            sample = value.to_bytes(bits // 8, 'little', signed=True)
            expected.extend(sample)
            if bits == 24 and storage == 4:
                sample += b'\x00'
            raw.extend(sample[::-1] if byte_order == 'big' else sample)
    for rate in (44100, 48000):
        audio = AudioStream()
        audio.begin()
        audio.feed(bytes(raw[:7]))
        assert audio.snapshot() is None, 'Must wait for output description'
        audio.describe(f'{rate}/{format}/2')
        audio.feed(bytes(raw[7:]))
        ring = audio.snapshot()
        _, canonical = ring.read_from(0)
        encoded = subprocess.run(ring.pcm.encoder_command(), input=canonical,
                                 capture_output=True, check=True, timeout=30)
        decoded = subprocess.run(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', 'pipe:0',
             '-f', ring.pcm.ffmpeg_format, '-c:a', 'pcm_' + ring.pcm.ffmpeg_format, 'pipe:1'],
            input=encoded.stdout, capture_output=True, check=True, timeout=30,
        )
        assert decoded.stdout == bytes(expected), f'FLAC round trip changed {rate}/{format} samples'
        audio.end()
        assert ring.closed and audio.snapshot() is None, 'Session must close its old stream'
print('Bridge import, metadata gating, and all PCM/FLAC round trips passed')
