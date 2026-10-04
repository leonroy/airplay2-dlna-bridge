"""Independent PCM expectations and reusable test pipes."""
import os


FORMATS = {
    "S8": (1, 8), "U8": (1, 8),
    "S16_LE": (2, 16), "S16_BE": (2, 16),
    "S24_LE": (4, 24), "S24_BE": (4, 24),
    "S24_3LE": (3, 24), "S24_3BE": (3, 24),
    "S32_LE": (4, 32), "S32_BE": (4, 32),
}


def samples(format, channels=2, frames=8192):
    """Build Shairport wire bytes and independent packed WAV/FFmpeg expectations."""
    storage, bits = FORMATS[format]
    extremes = (-(1 << (bits - 1)), -1, 0, 1, (1 << (bits - 1)) - 1)
    values = [extremes[i % len(extremes)] for i in range(frames * channels)]
    source, canonical = bytearray(), bytearray()
    for value in values:
        if bits == 8:
            source.append((value + 128) if format == "U8" else (value & 255))
            canonical.append(value + 128)
            continue
        packed = value.to_bytes(bits // 8, "little", signed=True)
        canonical.extend(packed)
        # Shairport's S24_LE/BE have a zero padding byte even for negative values.
        padded = packed + b"\x00" if bits == 24 and storage == 4 else packed
        source.extend(padded[::-1] if format.endswith("BE") else padded)
    return bytes(source), bytes(canonical)


class AudioPipe:
    """An OS pipe with explicit writer closure and idempotent cleanup."""
    def __init__(self, audio=None):
        self.read_fd, self.write_fd = os.pipe()
        self.audio = audio

    def write(self, data):
        return os.write(self.write_fd, data)

    def close_writer(self):
        if self.write_fd is not None:
            os.close(self.write_fd)
            self.write_fd = None

    def close(self):
        if self.read_fd is not None:
            if self.audio is not None:
                self.audio.detach_audio(self.read_fd)
            os.close(self.read_fd)
            self.read_fd = None
        self.close_writer()
