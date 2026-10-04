"""Run inside the image using only its runtime dependencies."""
import ast
from pathlib import Path
import runpy
import struct
import subprocess

runpy.run_path('/app/server.py', run_name='smoke_test')
tree = ast.parse(Path('/app/server.py').read_text())
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
print('Bridge import and exact PCM/FLAC round trip passed')
