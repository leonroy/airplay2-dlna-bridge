"""File-free FFmpeg checks: python -B tests/flac_startup.py --benchmark --roundtrip.

Timing starts just before the first PCM write and ends at the first emitted
FLAC frame sync after complete FLAC metadata. It excludes process launch before
feeding, and does not measure a complete decoded frame or audible latency.
Input stays open until a frame is observed. Probe tuning is an experimental
comparison, not a modification to the production command.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import threading
import time

from audio_helpers import FORMATS, samples


def load_bridge():
    path = Path(__file__).resolve().parents[1] / "bridge" / "server.py"
    spec = importlib.util.spec_from_file_location("startup_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def first_audio_offset(data):
    """Return an offset only after complete metadata and a frame sync exist."""
    if len(data) < 4:
        return None
    if data[:4] != b"fLaC":
        raise ValueError("Expected native FLAC stream")
    offset = 4
    while True:
        if len(data) < offset + 4:
            return None
        last = bool(data[offset] & 0x80)
        length = int.from_bytes(data[offset + 1:offset + 4], "big")
        offset += 4 + length
        if len(data) < offset:
            return None
        if last:
            if len(data) < offset + 2:
                return None
            if data[offset] != 0xff or data[offset + 1] & 0xfe != 0xf8:
                raise ValueError("Expected FLAC audio frame after metadata")
            return offset


def measure_startup(pcm, variant="current", timeout=8):
    command = pcm.encoder_command()
    command[0] = shutil.which("ffmpeg") or "ffmpeg"
    if variant not in ("current", "baseline", "probe_tuned"):
        raise ValueError(f"Unknown encoder variant: {variant}")
    if variant != "current":
        for option in ("-probesize", "-analyzeduration"):
            while option in command:
                index = command.index(option)
                del command[index:index + 2]
        if variant == "probe_tuned":
            index = command.index("-i")
            command[index:index] = ["-probesize", "32", "-analyzeduration", "0"]
    # Exactly 10ms of input per write at the tested 44.1/48k sample rates.
    source, _ = samples(pcm.format, pcm.channels, frames=pcm.rate // 100)
    chunk = pcm.normalise(source)
    process = subprocess.Popen(command, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stop = threading.Event()
    started = []
    errors = []

    def feed():
        try:
            started.append(time.monotonic())
            count = 0
            while not stop.is_set():
                process.stdin.write(chunk)
                process.stdin.flush()
                count += 1
                stop.wait(max(0, started[0] + count * .01 - time.monotonic()))
        except (BrokenPipeError, OSError) as error:
            errors.append(str(error))
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    output = bytearray()
    deadline = time.monotonic() + timeout
    elapsed = None
    try:
        while time.monotonic() < deadline:
            if not select.select([process.stdout], [], [],
                                 max(0, deadline - time.monotonic()))[0]:
                break
            data = os.read(process.stdout.fileno(), 65536)
            if not data:
                break
            output.extend(data)
            if first_audio_offset(output) is not None:
                elapsed = time.monotonic() - started[0]
                break
    finally:
        stop.set()
        # Kill before joining so a blocked stdin write cannot retain the feeder.
        if process.poll() is None:
            process.kill()
        writer.join(timeout=2)
        process.wait(timeout=2)
        stderr = process.stderr.read().decode(errors="replace")
        process.stdout.close()
        process.stderr.close()
    if writer.is_alive():
        raise RuntimeError("PCM feeder did not stop")
    if elapsed is None:
        raise AssertionError(f"No live FLAC frame within {timeout}s: {stderr}; {errors}")
    return elapsed


def roundtrip(pcm):
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    source, expected = samples(pcm.format, pcm.channels)
    command = pcm.encoder_command()
    command[0] = ffmpeg
    encoded = subprocess.run(command, input=pcm.normalise(source),
                             capture_output=True, check=True, timeout=15)
    decoded = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", pcm.ffmpeg_format, "-c:a", "pcm_" + pcm.ffmpeg_format, "pipe:1"],
        input=encoded.stdout, capture_output=True, check=True, timeout=15)
    assert decoded.stdout == expected, f"Sample mismatch: {pcm.description}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--roundtrip", action="store_true")
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    bridge = load_bridge()
    version = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                             check=True, text=True).stdout.splitlines()[0]
    result = {"ffmpeg": version}
    if args.roundtrip:
        count = 0
        for format in FORMATS:
            for rate in (44100, 48000):
                for channels in (1, 2):
                    roundtrip(bridge.PCMFormat(rate, format, channels))
                    count += 1
        result["exact_roundtrips"] = count
    if args.benchmark:
        result["paced_first_audio_frame_ms"] = {}
        for rate, format in ((44100, "S16_LE"), (48000, "S32_LE")):
            pcm = bridge.PCMFormat(rate, format, 2)
            result["paced_first_audio_frame_ms"][pcm.description] = {
                variant: [round(measure_startup(pcm, variant=variant) * 1000, 1)
                        for _ in range(args.repeats)]
                for variant in ("baseline", "probe_tuned")}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
