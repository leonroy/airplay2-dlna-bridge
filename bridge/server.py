#!/usr/bin/env python3
"""AirPlay 2 -> UPnP/DLNA renderer bridge:
- raw PCM from shairport-sync is described by its ssnc/odsc metadata
  and normalised to little-endian PCM before entering the ring buffer
  (a slow/stalled player can never back-pressure shairport into dropping audio)
- /stream.flac : per-client ffmpeg encodes PCM->FLAC from the join point,
  served with ICY metadata
- /stream.wav  : plain WAV, no ICY ever (diagnostic/fallback)
- the shairport metadata pipe drives UPnP Play/Stop, hardware volume, and
  now-playing DIDL pushes on the renderer
"""
import base64, collections, hashlib, json, os, plistlib, re, select, socket, struct, subprocess, threading, time, http.client, ipaddress, urllib.parse
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape
from xml.etree import ElementTree

AUDIO_PIPE = "/shared/audio"
META_PIPE = "/shared/metadata"
PORT = int(os.environ.get("STREAM_PORT", "8000"))
STREAM_URL = os.environ.get("STREAM_URL", "")
RENDERER_IP = os.environ.get("RENDERER_IP") or os.environ.get("WIIM_IP", "")
RENDERER_PORT = 49152
MAX_VOLUME = int(os.environ.get("MAX_VOLUME", "100"))
HTTP_MAX_CONNECTIONS = int(os.environ.get("HTTP_MAX_CONNECTIONS", "16"))
FLAC_MAX_ENCODERS = int(os.environ.get("FLAC_MAX_ENCODERS", "4"))
HTTP_HEADER_TIMEOUT = int(os.environ.get("HTTP_HEADER_TIMEOUT", "5"))
HTTP_HEADER_DEADLINE = int(os.environ.get("HTTP_HEADER_DEADLINE", "10"))
if min(HTTP_MAX_CONNECTIONS, FLAC_MAX_ENCODERS,
       HTTP_HEADER_TIMEOUT, HTTP_HEADER_DEADLINE) <= 0:
    raise ValueError("HTTP/encoder limits and header timeouts must be positive integers")
ENCODER_SLOTS = threading.BoundedSemaphore(FLAC_MAX_ENCODERS)

ICY_META_INT = 131072               # same interval airupnp uses
RING_SECONDS = 12
BACKLOG_SECONDS = 1.5
PENDING_MAX = 4 * 1024 * 1024       # bound audio waiting for output-description metadata
META_PAYLOAD_MAX = 8 * 1024 * 1024
META_ITEM_MAX = 12 * 1024 * 1024   # base64, line breaks and XML included
META_TEXT_MAX = 4096               # UTF-8 bytes per track field
BOOT_TIME = time.monotonic()
INSTANCE_ID = str(time.time_ns())
WEB_ROOT = Path(__file__).with_name("web")
WEB_ICON_PATHS = {"/favicon.svg", "/favicon.ico", "/apple-touch-icon.png",
                  "/icon-192.png", "/icon-512.png", "/site.webmanifest"}
WEB_ASSET_FILES = {"/": "index.html", "/app.css": "app.css", "/app.js": "app.js",
                   "/placeholder.svg": "placeholder.svg", "/demo.svg": "demo.svg"}
WEB_ASSET_FILES.update({path: path[1:] for path in WEB_ICON_PATHS})


class LogHistory:
    """One bounded history shared by all viewers; no network work under its lock."""
    def __init__(self, max_entries=500, max_bytes=512 * 1024):
        self.lock = threading.Lock()
        self.entries = collections.deque()
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.bytes = self.sequence = 0

    def append(self, line):
        raw = line.encode("utf-8", errors="replace")[:4096]
        line = raw.decode("utf-8", errors="ignore")
        with self.lock:
            self.sequence += 1
            self.entries.append((self.sequence, line, len(raw)))
            self.bytes += len(raw)
            while self.entries and (len(self.entries) > self.max_entries or self.bytes > self.max_bytes):
                self.bytes -= self.entries.popleft()[2]

    def read(self, cursor):
        with self.lock:
            gap = bool(self.entries and cursor is not None and cursor < self.entries[0][0] - 1)
            # At most ~64 KiB per batch; a slow viewer cannot create a private queue.
            records = [(seq, line) for seq, line, _ in self.entries
                       if cursor is None or seq > cursor][:16]
            return records, gap


LOG_HISTORY = LogHistory()
VIEWERS = threading.Condition()
VIEWER_COUNT = 0
MAX_VIEWERS = 8
OBSERVER = None
COMMAND_LOCK = threading.Lock()
LAST_COMMAND = None


class PCMFormat(collections.namedtuple("PCMFormatBase", "rate format channels")):
    """Validated Shairport pipe format; storage width can differ from bit depth."""
    __slots__ = ()
    FORMATS = {
        "S8": (1, 8, "little"), "U8": (1, 8, "little"),
        "S16_LE": (2, 16, "little"), "S16_BE": (2, 16, "big"),
        "S24_LE": (4, 24, "little"), "S24_BE": (4, 24, "big"),
        "S24_3LE": (3, 24, "little"), "S24_3BE": (3, 24, "big"),
        "S32_LE": (4, 32, "little"), "S32_BE": (4, 32, "big"),
    }
    RATES = frozenset((5512, 8000, 11025, 16000, 22050, 32000, 44100,
                       48000, 64000, 88200, 96000, 176400, 192000, 352800, 384000))

    def __new__(cls, rate, format, channels):
        if type(rate) is not int or rate not in cls.RATES:
            raise ValueError("unsupported output sample rate")
        if not isinstance(format, str) or format not in cls.FORMATS:
            raise ValueError("unsupported output sample format")
        # odsc supplies no channel layout. Keep the renderer bridge mono/stereo.
        if type(channels) is not int or channels not in (1, 2):
            raise ValueError("output must be mono or stereo")
        return super().__new__(cls, rate, format, channels)

    @classmethod
    def from_description(cls, data):
        try:
            description = data.decode("ascii") if isinstance(data, bytes) else data
            match = re.fullmatch(r"([0-9]{4,6})/([A-Z0-9_]{2,12})/([1-8])", description)
            if match is None:
                raise ValueError("expected rate/format/channels in odsc")
            return cls(int(match[1]), match[2], int(match[3]))
        except (UnicodeError, TypeError):
            raise ValueError("expected ASCII rate/format/channels in odsc") from None

    @property
    def bits(self):
        return self.FORMATS[self.format][1]

    @property
    def description(self):
        return f"{self.rate}/{self.format}/{self.channels}"

    @property
    def source_frame(self):
        return self.FORMATS[self.format][0] * self.channels

    @property
    def frame(self):
        return self.bits // 8 * self.channels

    @property
    def ffmpeg_format(self):
        return "u8" if self.bits == 8 else f"s{self.bits}le"

    def normalise(self, data):
        """Accept complete source frames; produce packed PCM suitable for WAV."""
        if len(data) % self.source_frame:
            raise ValueError("incomplete PCM frame")
        storage, bits, byte_order = self.FORMATS[self.format]
        if self.format == "S8":
            return bytes(value ^ 128 for value in data)
        if bits == 24 and storage == 4:
            if byte_order == "little":
                return b"".join(data[i:i + 3] for i in range(0, len(data), 4))
            return b"".join(data[i + 1:i + 4][::-1] for i in range(0, len(data), 4))
        if byte_order == "big":
            return b"".join(data[i:i + storage][::-1] for i in range(0, len(data), storage))
        return data

    @property
    def flac_bits(self):
        # FLAC stores 8-bit input losslessly as 16-bit samples.
        return max(16, self.bits)

    def encoder_command(self):
        bits = self.flac_bits
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                   "-f", self.ffmpeg_format, "-ar", str(self.rate),
                   "-ac", str(self.channels), "-i", "pipe:0",
                   "-c:a", "flac", "-sample_fmt", "s16" if bits == 16 else "s32",
                   "-bits_per_raw_sample", str(bits)]
        if bits == 32:
            # FFmpeg otherwise silently truncates 32-bit input to 24-bit FLAC.
            command += ["-strict", "experimental"]
        return command + ["-compression_level", "0", "-flush_packets", "1",
                          "-f", "flac", "pipe:1"]

    def wav_header(self):
        return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
                + b"fmt " + struct.pack("<IHHIIHH", 16, 1, self.channels, self.rate,
                                       self.rate * self.frame, self.frame, self.bits)
                + b"data" + struct.pack("<I", 0xFFFFFFFF))


class StreamEnded(Exception):
    """A reader must reconnect rather than consume a different session's PCM."""


def log(msg):
    now = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
    line = f"[bridge] {stamp}.{int(now % 1 * 1000):03d}Z {msg}"
    print(line, flush=True)
    LOG_HISTORY.append(line)


def description_for_log(data):
    """Bound and escape format descriptions, including malformed payloads."""
    if isinstance(data, (bytes, str)):
        return f"{data[:96]!r} length={len(data)}"
    return f"<{type(data).__name__}>"


class Ring:
    def __init__(self, pcm, session_id=None):
        self.pcm = pcm
        self.session_id = session_id
        self.frame = pcm.frame
        self.capacity = pcm.rate * RING_SECONDS * self.frame
        self.backlog = int(pcm.rate * BACKLOG_SECONDS) * self.frame
        self.buf = bytearray()
        self.head = 0               # absolute offset of buf[0]
        self.session_start = 0      # absolute offset where the current session began
        self.cond = threading.Condition()
        self.closed = False

    def write(self, data):
        with self.cond:
            if self.closed:
                return
            self.buf += data
            if len(self.buf) > self.capacity:
                cut = len(self.buf) - self.capacity
                cut -= cut % self.frame
                del self.buf[:cut]
                self.head += cut
            self.cond.notify_all()

    def end(self):
        with self.cond:
            return self.head + len(self.buf)

    def mark_session(self):
        with self.cond:
            self.session_start = self.head + len(self.buf)

    def join_pos(self):
        with self.cond:
            end = self.head + len(self.buf)
            pos = max(self.session_start, self.head, end - self.backlog)
            return pos + (-pos) % self.frame

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def read_from(self, pos, timeout=5, cancel=None):
        """return (newpos, bytes) at absolute pos, waiting for data if at the end"""
        with self.cond:
            if self.closed or (cancel is not None and cancel.is_set()):
                raise StreamEnded()
            end = self.head + len(self.buf)
            if pos >= end:
                self.cond.wait(timeout)
                if self.closed or (cancel is not None and cancel.is_set()):
                    raise StreamEnded()
                end = self.head + len(self.buf)
                if pos >= end:
                    return pos, b""
            if pos < self.head:     # fell behind: jump near the live edge
                pos = max(self.head, end - self.backlog)
                pos += (-pos) % self.frame
            return end, bytes(self.buf[pos - self.head:])


class AudioStream:
    """Serialise format announcements, raw input, and playback session changes."""
    def __init__(self):
        self.lock = threading.RLock()
        self.ring = None
        self.pcm = None
        self.source_codec = self.stream_type = None
        self.source_description = self.aac_bitrate_bps = None
        self.receiver_stats = None
        self.pending = bytearray()
        self.active = False
        self.failed = False
        self.accepting = True      # allow first-session audio to precede metadata
        self.audio_fd = None
        self.audio_generation = None
        self.eof_generation = None
        self.drain_on_attach = False
        self.idle_bytes = 0
        self.session_counter = 0
        self.session_id = None
        self.started = None
        self.first_audio = None
        self.wait_logged = None
        self.raw_bytes = self.pcm_bytes = self.discarded_bytes = self.odsc_count = 0

    def _ensure_session(self):
        # A local ID, not an ID supplied by Shairport. Audio/odsc may precede pbeg.
        if self.session_id is None:
            if self.idle_bytes:
                log(f"audio discarded between sessions bytes={self.idle_bytes}")
                self.idle_bytes = 0
            self.session_counter += 1
            self.session_id = self.session_counter
            self.started = time.monotonic()

    def status(self):
        with self.lock:
            age = int((time.monotonic() - self.started) * 1000) if self.started is not None else 0
            pcm = self.pcm.description if self.pcm else "unknown"
            return (f"session={self.session_id or 'none'} active={self.active} failed={self.failed} "
                    f"pcm={pcm} odsc_count={self.odsc_count} age_ms={age} "
                    f"raw_bytes={self.raw_bytes} pcm_bytes={self.pcm_bytes} "
                    f"pending_bytes={len(self.pending)} discarded_bytes={self.discarded_bytes}")

    def begin(self):
        with self.lock:
            self._ensure_session()
            self.accepting = True
            self.active = True
            # odsc can arrive before pbeg: the player thread is started first.
            log(f"pbeg received; {'format ready' if self.pcm else 'waiting for odsc'}; {self.status()}")

    def _drain_audio(self):
        """Discard the ended writer's queued bytes, under the same lock as reads."""
        if self.audio_fd is None:
            # A completed EOF already establishes that this session was drained.
            self.drain_on_attach = self.session_id != self.eof_generation
            return
        drained = 0
        while True:
            try:
                data = os.read(self.audio_fd, 65536)
            except BlockingIOError:
                break
            if not data:
                self.eof_generation = self.session_id
                break
            drained += len(data)
        self.drain_on_attach = False
        if drained:
            log(f"drained queued audio at session boundary bytes={drained}; {self.status()}")

    def attach_audio(self, fd):
        with self.lock:
            os.set_blocking(fd, False)
            self.audio_fd = fd
            self.audio_generation = None
            if self.drain_on_attach:
                self._drain_audio()

    def detach_audio(self, fd):
        with self.lock:
            if self.audio_fd == fd:
                self.audio_fd = None

    def read_audio(self, fd):
        with self.lock:
            # Read and feed atomically: no read can straddle pend's drain/reset.
            try:
                data = os.read(fd, 8192)
            except BlockingIOError:
                return None, self.audio_generation
            if data:
                self.eof_generation = None
                accepting = self.accepting
                self.feed(data)
                if accepting:
                    self.audio_generation = self.session_id
            else:
                self.eof_generation = self.audio_generation
            return data, self.audio_generation

    def end(self, reason="pend", *, drain=True):
        with self.lock:
            self.accepting = False
            if drain:
                # Shairport emits pend after the old player has stopped writing.
                self._drain_audio()
            log(f"session ended reason={reason}; {self.status()}")
            if self.ring is not None:
                self.ring.close()
            self.ring = self.pcm = None
            self.source_codec = self.stream_type = None
            self.source_description = self.aac_bitrate_bps = None
            self.receiver_stats = None
            self.pending.clear()
            self.active = self.failed = False
            self.session_id = self.started = self.first_audio = self.wait_logged = None
            self.raw_bytes = self.pcm_bytes = self.discarded_bytes = self.odsc_count = 0
            self.idle_bytes = 0

    def fail(self, reason="invalid output description"):
        with self.lock:
            self._ensure_session()
            if not self.failed:
                log(f"audio session rejected reason={reason}; {self.status()}")
            if self.ring is not None:
                self.ring.close()
            self.ring = self.pcm = None
            self.discarded_bytes += len(self.pending)
            self.pending.clear()
            self.failed = True
            self.aac_bitrate_bps = None
            self.receiver_stats = None

    def describe(self, data):
        with self.lock:
            self._ensure_session()
            self.odsc_count += 1
            log(f"odsc received value={description_for_log(data)}; {self.status()}")
            pcm = PCMFormat.from_description(data)
            if self.failed:
                raise ValueError("audio session rejected; start a new playback session")
            if self.pcm is not None:
                if pcm == self.pcm:
                    log(f"odsc unchanged; {self.status()}")
                    return False
                # There is no byte offset in odsc: never relabel already buffered audio.
                self.fail(f"output format changed {self.pcm.description} -> {pcm.description} "
                          "without pend/pbeg boundary")
                raise ValueError("output format changed during playback; start a new session")
            self.pcm = pcm
            self.accepting = True
            self.ring = Ring(pcm, self.session_id)
            buffered = len(self.pending)
            self._write_pending()
            delay = (f"{int((time.monotonic() - self.first_audio) * 1000)}ms after first audio"
                     if self.first_audio is not None else "before first audio")
            log(f"odsc accepted source={pcm.description} normalised={pcm.ffmpeg_format} "
                f"source_bytes_per_frame={pcm.source_frame} pcm_bytes_per_frame={pcm.frame} "
                f"ring_bytes={self.ring.capacity} backlog_bytes={self.ring.backlog} "
                f"buffered_before_odsc={buffered} odsc_timing={delay}; {self.status()}")
            return True

    def feed(self, data):
        with self.lock:
            if not data:
                return
            if not self.accepting:
                # Between pend and the next metadata, bytes still belong to the old session.
                self.idle_bytes += len(data)
                if self.idle_bytes == len(data):
                    log(f"discarding audio between sessions chunk_bytes={len(data)}; {self.status()}")
                return
            self._ensure_session()
            self.raw_bytes += len(data)
            if self.first_audio is None:
                self.first_audio = time.monotonic()
                log(f"first audio received chunk_bytes={len(data)}; {self.status()}")
            if self.failed:
                self.discarded_bytes += len(data)
                return
            self.pending.extend(data)
            if self.pcm is None:
                now = time.monotonic()
                if len(self.pending) > PENDING_MAX:
                    self.fail(f"odsc missing or delayed; pending-buffer limit={PENDING_MAX} exceeded")
                elif self.wait_logged is None or now - self.wait_logged >= 5:
                    log(f"audio waiting for odsc; pending_limit={PENDING_MAX}; {self.status()}")
                    self.wait_logged = now
                return
            self._write_pending()

    def _write_pending(self):
        complete = len(self.pending) // self.pcm.source_frame * self.pcm.source_frame
        if complete:
            normalised = self.pcm.normalise(bytes(self.pending[:complete]))
            self.ring.write(normalised)
            self.pcm_bytes += len(normalised)
            del self.pending[:complete]

    def snapshot(self):
        with self.lock:
            return self.ring if self.active and not self.failed else None


AUDIO = AudioStream()
NOW_PLAYING = {"title": "", "artist": "", "album": "", "artwork": ""}
# Content IDs keep a cached URL tied to the same bytes across restarts.
ART = {"id": None, "bytes": b"", "mime": "image/jpeg"}
# recent artworks kept by id: a renderer lagging behind rapid track changes may
# fetch an older /art-<id>.jpg after ART has moved on - serve THAT push's image,
# not whatever is current, so it never caches the wrong cover for a track
ART_CACHE = collections.OrderedDict()
ART_CACHE_MAX = 16
ART_ITEM_MAX = 4 * 1024 * 1024
ART_CACHE_BYTES_MAX = 16 * 1024 * 1024


def cache_art(art_id, data, mime):
    if len(data) > ART_ITEM_MAX:
        return False
    with AUDIO.lock:
        ART_CACHE[art_id] = (data, mime)
        ART_CACHE.move_to_end(art_id)
        while (len(ART_CACHE) > ART_CACHE_MAX or
               sum(len(entry[0]) for entry in ART_CACHE.values()) > ART_CACHE_BYTES_MAX):
            ART_CACHE.popitem(last=False)
    return True


def artwork_content_type(data):
    """Use image signatures instead of untrusted metadata for HTTP headers."""
    if not isinstance(data, bytes) or not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def update_artwork(data, mime=None):
    """Publish supported artwork from either metadata protocol under the audio lock."""
    if not isinstance(data, bytes) or not data or len(data) > ART_ITEM_MAX:
        return False
    mime = artwork_content_type(data)
    if mime is None:
        return False
    art_id = hashlib.sha256(data).hexdigest()
    with AUDIO.lock:
        if ART["id"] == art_id and ART["bytes"] == data:
            return False
        ART.update(id=art_id, bytes=data, mime=mime)
        cache_art(art_id, data, mime)
        NOW_PLAYING["artwork"] = f"{STREAM_URL.rsplit('/', 1)[0]}/art-{art_id}.jpg"
        mark_metadata_dirty()
        log(f"artwork updated ({len(data)} bytes, {mime})")
        return True


# WiiM's display only reads DIDL (it ignores ICY), so a track change needs a
# SetAVTransportURI push; the player restarts the stream on it (~2-4s gap).
DIDL_PUSH = os.environ.get("DIDL_PUSH", "1") == "1"
PUSH_SETTLE = 2.0
INITIAL_PUSH_SETTLE = 0.5           # initial playback with a real title
STATE = {"active": False, "dirty": 0.0, "pushed": None, "revision": 0}


def mark_metadata_dirty():
    """Call under AUDIO.lock; revisions prevent stale completions losing updates."""
    STATE["revision"] += 1
    STATE["dirty"] = time.monotonic()


def log_timing(event, session, revision, **fields):
    """One bounded event record; never include metadata or audio payloads."""
    detail = " ".join(f"{key}={value}" for key, value in fields.items())
    log(f"timing event={event} session={session} revision={revision} {detail}")


class FlacFrameProbe:
    """Find the first frame sync after metadata, retaining at most four bytes."""
    def __init__(self):
        self.phase, self.remaining, self.last = "marker", 0, False
        self.header = bytearray()

    def feed(self, data):
        offset = 0
        while offset < len(data) and self.phase not in ("done", "invalid"):
            if self.phase == "body":
                count = min(self.remaining, len(data) - offset)
                self.remaining -= count
                offset += count
                if self.remaining == 0:
                    self.phase = "frame" if self.last else "block"
                continue
            size = 2 if self.phase == "frame" else 4
            count = min(size - len(self.header), len(data) - offset)
            self.header.extend(data[offset:offset + count])
            offset += count
            if len(self.header) < size:
                continue
            if self.phase == "marker":
                self.phase = "block" if self.header == b"fLaC" else "invalid"
            elif self.phase == "block":
                self.last = bool(self.header[0] & 0x80)
                self.remaining = int.from_bytes(self.header[1:], "big")
                self.phase = ("body" if self.remaining else
                              "frame" if self.last else "block")
            else:
                valid = self.header[0] == 0xff and self.header[1] & 0xfe == 0xf8
                self.phase = "done" if valid else "invalid"
                self.header.clear()
                return valid
            self.header.clear()
        return False


CLIENTS = set()          # StreamClient records, including cleanup in progress
CLIENTS_LOCK = threading.Lock()
STREAM_POLL_SECONDS = 0.1
STREAM_WRITE_TIMEOUT = 30


class StreamClient:
    """Own one response's cancellation and encoder lifetime.

    A receive-side FIN is not cancellation: HTTP clients can half-close their
    request side and continue consuming the response, including through pauses.
    """
    def __init__(self, ring, connection):
        self.ring = ring
        self.session_id = ring.session_id
        self.connection = connection
        self.cancelled = threading.Event()
        self.encoder = None
        self.feeder = None

    def wake(self):
        self.cancelled.set()
        with self.ring.cond:
            self.ring.cond.notify_all()

    def interrupt(self):
        self.wake()
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def check(self):
        if self.cancelled.is_set() or self.ring.closed:
            raise StreamEnded()
        # A reset is definitive response failure, unlike request-side FIN.
        # Checking SO_ERROR avoids polling a permanently readable EOF socket.
        error = self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if error:
            raise OSError(error, os.strerror(error))


def register_client(client):
    with CLIENTS_LOCK:
        if client.ring.closed:
            client.cancelled.set()
        CLIENTS.add(client)


def unregister_client(client):
    with CLIENTS_LOCK:
        CLIENTS.discard(client)


def has_active_clients(session_id):
    with CLIENTS_LOCK:
        return any(c.session_id == session_id and not c.cancelled.is_set()
                   and not c.ring.closed for c in CLIENTS)


def _cancel_clients(session_id, reason, *, all_sessions=False):
    with CLIENTS_LOCK:
        clients = [c for c in CLIENTS if (all_sessions or c.session_id == session_id)
                   and not c.cancelled.is_set()]
        # Exclude these streams from playback decisions before interrupting I/O.
        for client in clients:
            client.cancelled.set()
    for client in clients:
        client.interrupt()
    if clients:
        log(f"{reason}: cancelled {len(clients)} stream client(s)")


def cancel_session_clients(session_id, reason):
    _cancel_clients(session_id, reason)


def cancel_all_clients(reason):
    _cancel_clients(None, reason, all_sessions=True)


def drop_clients(reason):
    ring = AUDIO.snapshot()
    if ring is not None:
        ring.mark_session()
    cancel_all_clients(reason)


def wav_header(pcm):
    return pcm.wav_header()


class BoundedHTTPServer(ThreadingHTTPServer):
    """Reserve accepted-connection capacity before creating a handler thread."""
    def __init__(self, address, handler, *, max_connections=HTTP_MAX_CONNECTIONS):
        if max_connections <= 0:
            raise ValueError("max_connections must be positive")
        self.connection_slots = threading.BoundedSemaphore(max_connections)
        # Long-lived status streams must leave at least one slot for audio.
        self.status_viewer_limit = min(MAX_VIEWERS, max_connections - 1)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.connection_slots.acquire(blocking=False):
            # No handler thread or parsing for overload; bound this small write
            # so a peer refusing to read cannot stall the accept loop.
            try:
                request.settimeout(STREAM_POLL_SECONDS)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\n"
                                b"Connection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()


class StreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(HTTP_HEADER_TIMEOUT)

    def _expire_headers(self):
        # An inactivity timeout alone lets a trickling request retain a thread
        # indefinitely. Shutdown interrupts even a buffered readline in progress.
        self._headers_expired.set()
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _finish_headers(self):
        self._header_timer.cancel()
        self._header_timer.join()
        self.connection.settimeout(STREAM_WRITE_TIMEOUT)

    def handle_one_request(self):
        self.connection.settimeout(HTTP_HEADER_TIMEOUT)
        self._headers_expired = threading.Event()
        self._header_timer = threading.Timer(HTTP_HEADER_DEADLINE, self._expire_headers)
        self._header_timer.daemon = True
        self._header_timer.start()
        try:
            super().handle_one_request()
        except OSError:
            self.close_connection = True
        finally:
            self._finish_headers()

    def parse_request(self):
        if self._headers_expired.is_set():
            self.close_connection = True
            return False
        try:
            parsed = super().parse_request()
        finally:
            # Cancel and join before do_GET: the header deadline must never
            # terminate an otherwise healthy stream or a long playback pause.
            self._finish_headers()
        # EOF produced by shutdown can look like a complete HTTP/0.9 request
        # or end-of-headers to the stdlib parser. Never dispatch expired input.
        if self._headers_expired.is_set():
            self.close_connection = True
            return False
        return parsed

    def log_message(self, fmt, *args):
        # Refreshes and SSE reconnects must not flood the live log history.
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/api/status", "/api/events") and len(args) > 1 and str(args[1]) == "200":
            return
        log(f"http {self.address_string()} {fmt % args}")

    def write_stream(self, client, data):
        """Bound response writes too: shutdown alone need not wake a timed send."""
        remaining = memoryview(data)
        deadline = time.monotonic() + STREAM_WRITE_TIMEOUT
        while remaining:
            client.check()
            try:
                sent = self.connection.send(remaining)
            except socket.timeout:
                if time.monotonic() >= deadline:
                    raise
                continue
            if sent == 0:
                raise StreamEnded()
            remaining = remaining[sent:]
            deadline = time.monotonic() + STREAM_WRITE_TIMEOUT

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/status":
            self.send_body(json.dumps(status_snapshot()).encode(), "application/json; charset=utf-8")
        elif path == "/api/events":
            self.serve_events()
        elif path in WEB_ASSET_FILES:
            self.serve_page(path)
        elif self.path.startswith("/stream.flac"):
            self.serve_flac()
        elif self.path.startswith("/stream.wav"):
            self.serve_wav()
        elif self.path.startswith("/art-"):
            self.serve_art()
        else:
            self.send_error(404)

    def send_body(self, body, content_type):
        self.connection.settimeout(5)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (OSError, socket.timeout):
            pass

    def serve_page(self, path):
        # Explicit routes: arbitrary paths can never read files from the container.
        name = WEB_ASSET_FILES.get(path)
        if name is None:
            self.send_error(404)
            return
        types = {".html": "text/html", ".css": "text/css", ".js": "text/javascript",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/vnd.microsoft.icon",
                 ".webmanifest": "application/manifest+json"}
        try:
            body = (WEB_ROOT / name).read_bytes()
        except OSError:
            self.send_error(404)
            return
        suffix = Path(name).suffix
        self.send_body(body, types[suffix] + ("" if suffix in (".png", ".ico") else "; charset=utf-8"))

    def send_event(self, name, value):
        payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        self.wfile.write(f"event: {name}\ndata: {payload}\n\n".encode())
        self.wfile.flush()

    def serve_events(self):
        global VIEWER_COUNT
        logs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("logs") == ["1"]
        with VIEWERS:
            if VIEWER_COUNT >= getattr(self.server, "status_viewer_limit", MAX_VIEWERS):
                self.send_error(503, "Too many status viewers")
                return
            VIEWER_COUNT += 1
            VIEWERS.notify_all()
        try:
            self.connection.settimeout(5)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")
            cursor = None
            next_status = 0.0
            while True:
                now = time.monotonic()
                if now >= next_status:
                    self.send_event("status", status_snapshot())
                    next_status = now + 2
                if logs:
                    entries, gap = LOG_HISTORY.read(cursor)
                    if gap:
                        self.send_event("gap", {"message": "Some older log lines expired."})
                    for seq, line in entries:
                        self.send_event("log", {"sequence": seq, "line": line})
                        cursor = seq
                # Observe FIN as well as RST, including while no log lines arrive.
                readable, _, _ = select.select([self.connection], [], [], 0.25)
                if readable:
                    break
        except (OSError, socket.timeout, ValueError):
            pass
        finally:
            with VIEWERS:
                VIEWER_COUNT -= 1
                VIEWERS.notify_all()

    def serve_art(self):
        m = re.fullmatch(r"/art-([0-9a-f]{64})\.jpg", self.path)
        entry = ART_CACHE.get(m.group(1)) if m else None
        # An expired URL must never serve a different track's current cover.
        if entry is None:
            self.send_error(404)
            return
        data, _declared_mime = entry
        mime = artwork_content_type(data)
        if mime is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- diagnostic endpoint: pure PCM in a WAV container, never any ICY ----
    def serve_wav(self):
        ring = AUDIO.snapshot()
        if ring is None:
            log(f"wav request rejected HTTP=503; {AUDIO.status()}")
            self.send_error(503, "Waiting for a valid Shairport output description")
            return
        client = StreamClient(ring, self.connection)
        register_client(client)
        try:
            if client.cancelled.is_set():
                return
            self.connection.settimeout(30)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.end_headers()
            self.connection.settimeout(STREAM_POLL_SECONDS)
            log(f"wav client {self.address_string()} connected session={ring.session_id} pcm={ring.pcm.description}")
            pos = ring.join_pos()
            self.write_stream(client, wav_header(ring.pcm))
            while True:
                client.check()
                pos, data = ring.read_from(pos, timeout=STREAM_POLL_SECONDS,
                                           cancel=client.cancelled)
                if data:
                    self.write_stream(client, data)
        except (StreamEnded, BrokenPipeError, ConnectionResetError, socket.timeout, TimeoutError, OSError):
            log(f"wav client {self.address_string()} disconnected session={ring.session_id} ring_closed={ring.closed}")
        finally:
            client.wake()
            unregister_client(client)

    # ---- main endpoint: FLAC + ICY, the combo airupnp uses on the WiiM ----
    def serve_flac(self):
        requested_at = time.monotonic()
        with AUDIO.lock:
            ring, revision = AUDIO.snapshot(), STATE["revision"]
        if ring is None:
            log(f"flac request rejected HTTP=503; {AUDIO.status()}")
            self.send_error(503, "Waiting for a valid Shairport output description")
            return
        client = StreamClient(ring, self.connection)
        encoder_slots = ENCODER_SLOTS
        if not encoder_slots.acquire(blocking=False):
            log(f"flac request rejected HTTP=503; encoder capacity={FLAC_MAX_ENCODERS}")
            self.send_error(503, "FLAC encoder capacity exhausted")
            return
        first_pcm_at = None
        def feeder():
            nonlocal first_pcm_at
            pos = ring.join_pos()
            try:
                while not client.cancelled.is_set():
                    pos, data = ring.read_from(pos, cancel=client.cancelled)
                    if data:
                        if first_pcm_at is None:
                            first_pcm_at = time.monotonic()
                            log_timing("flac_pcm", ring.session_id, revision,
                                       request_ms=round((first_pcm_at - requested_at) * 1000, 1))
                        client.encoder.stdin.write(data)
                        client.encoder.stdin.flush()
            except (StreamEnded, BrokenPipeError, OSError):
                pass
            finally:
                try:
                    client.encoder.stdin.close()
                except OSError:
                    pass
        try:
            register_client(client)
            if client.cancelled.is_set():
                return
            # Spawn before acknowledging success, and own it even if headers or
            # feeder setup fail. No output probing/read can trap cleanup forever.
            client.encoder = subprocess.Popen(
                ring.pcm.encoder_command(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            icy = self.headers.get("Icy-MetaData") == "1"
            self.connection.settimeout(30)
            self.send_response(200)
            self.send_header("Content-Type", "audio/flac")
            self.send_header("icy-name", "AirPlay 2")
            if icy:
                self.send_header("icy-metaint", str(ICY_META_INT))
            self.end_headers()
            self.connection.settimeout(STREAM_POLL_SECONDS)
            log(f"flac client {self.address_string()} connected (icy={icy}) "
                f"session={ring.session_id} pcm={ring.pcm.description}")
            log_timing("flac_connected", ring.session_id, revision,
                       request_ms=round((time.monotonic() - requested_at) * 1000, 1))
            fd = client.encoder.stdout.fileno()
            os.set_blocking(fd, False)
            client.feeder = threading.Thread(target=feeder, daemon=True)
            client.feeder.start()
            sent_since_meta, last_meta = 0, None
            frame_probe = FlacFrameProbe()
            while not client.cancelled.is_set() and not ring.closed:
                client.check()
                ready, _, _ = select.select([fd], [], [], STREAM_POLL_SECONDS)
                if not ready:
                    continue
                try:
                    data = os.read(fd, 8192)
                except BlockingIOError:
                    continue
                if not data:
                    break
                if frame_probe.feed(data):
                    emitted_at = time.monotonic()
                    log_timing("flac_frame", ring.session_id, revision,
                               request_ms=round((emitted_at - requested_at) * 1000, 1),
                               pcm_ms=(round((emitted_at - first_pcm_at) * 1000, 1)
                                       if first_pcm_at is not None else "none"))
                if not icy:
                    self.write_stream(client, data)
                    continue
                while data:
                    room = ICY_META_INT - sent_since_meta
                    self.write_stream(client, data[:room])
                    sent_since_meta += min(room, len(data))
                    data = data[room:]
                    if sent_since_meta == ICY_META_INT:
                        block, last_meta = self.icy_block(last_meta)
                        self.write_stream(client, block)
                        sent_since_meta = 0
        except (StreamEnded, BrokenPipeError, ConnectionResetError, socket.timeout, TimeoutError, OSError):
            log(f"flac client {self.address_string()} disconnected session={ring.session_id}")
        finally:
            try:
                client.wake()
                enc = client.encoder
                if enc is not None:
                    result = enc.poll()
                    if result is not None and result != 0:
                        log(f"flac encoder failed exit={result} session={ring.session_id} pcm={ring.pcm.description}")
                    # Killing first releases a feeder blocked on a full stdin
                    # pipe. Only the feeder closes its buffered stdin wrapper.
                    if result is None:
                        enc.kill()
                    enc.wait()
                    if client.feeder is not None and client.feeder.ident is not None:
                        client.feeder.join()
                    else:
                        enc.stdin.close()
                    enc.stdout.close()
                log(f"flac stream closed session={ring.session_id} ring_closed={ring.closed}")
            finally:
                try:
                    unregister_client(client)
                finally:
                    encoder_slots.release()

    @staticmethod
    def icy_block(last):
        with AUDIO.lock:
            cur = (NOW_PLAYING["title"], NOW_PLAYING["artist"], NOW_PLAYING["artwork"])
        if cur == last or (not cur[0] and last is None):
            return b"\x00", last
        title, artist, art = cur
        song = f"{artist} - {title}" if title and artist else title

        def field(value):
            # ICY readers disagree about backslash escapes. Keep delimiters out
            # of values instead, and replace controls without joining words.
            return "".join(" " if ord(char) < 32 or 127 <= ord(char) <= 159
                           else "’" if char == "'" else "/" if char == "\\" else char
                           for char in value)

        prefix, suffix = b"StreamTitle='", b"';"
        limit = 255 * 16
        raw_song = field(song).encode("utf-8")
        raw_song = raw_song[:limit - len(prefix) - len(suffix)].decode("utf-8", errors="ignore").encode("utf-8")
        raw = prefix + raw_song + suffix
        if art:
            artwork = f"StreamUrl='{field(art)}';".encode("utf-8")
            # Never truncate the URL into an unusable address.
            if len(raw) + len(artwork) <= limit:
                raw += artwork
        pad = (-len(raw)) % 16
        return bytes([(len(raw) + pad) // 16]) + raw + b"\x00" * pad, cur


def handle_audio_eof(generation, wiim=None):
    with AUDIO.lock:
        if generation is None or generation != AUDIO.session_id:
            log(f"audio EOF ignored ended_generation={generation}; {AUDIO.status()}")
            return False
        AUDIO.end("audio pipe EOF", drain=False)
        STATE.update(active=False, dirty=0.0, pushed=None)
        if wiim is not None:
            wiim.stop(generation)
    cancel_session_clients(generation, "audio pipe closed")
    return True


def audio_reader(wiim):
    last = 0.0
    while True:
        try:
            # unbuffered: a fifo read must return whatever is available
            log(f"waiting for audio pipe writer path={AUDIO_PIPE}; {AUDIO.status()}")
            with open(AUDIO_PIPE, "rb", buffering=0) as f:
                log(f"audio pipe open; {AUDIO.status()}")
                fd = f.fileno()
                AUDIO.attach_audio(fd)
                try:
                    while True:
                        select.select([fd], [], [])
                        data, generation = AUDIO.read_audio(fd)
                        if data is None:
                            continue
                        if not data:
                            break
                        now = time.monotonic()
                        # Only resume a player with a valid output description.
                        if (last and now - last > 2 and STATE["active"] and not has_active_clients(generation)
                                and AUDIO.snapshot() is not None):
                            log(f"audio resumed, kicking player; {AUDIO.status()}")
                            wiim.resume(generation)
                        last = now
                finally:
                    AUDIO.detach_audio(fd)
            handle_audio_eof(generation, wiim)
        except Exception as e:
            log(f"audio pipe error: {e}; {AUDIO.status()}")
            time.sleep(1)


# ------------------------------------------------------------ renderer control
HTTP_TIMEOUT = 5.0
HTTP_BODY_MAX = 1024 * 1024
SOAP_BODY_MAX = 64 * 1024


def http_req(url, data=None, headers=None, timeout=HTTP_TIMEOUT, *, deadline=None,
             max_bytes=HTTP_BODY_MAX, allowed_statuses=()):
    """Bound the entire numeric-IP HTTP exchange, including trickling responses.

    The watchdog shuts down the actual socket: no orphan request can keep the
    serialized dispatcher busy after its deadline. DNS is deliberately excluded
    by requiring the documented RENDERER_IP/WIIM_IP to be a numeric address.
    """
    deadline = deadline if deadline is not None else time.monotonic() + timeout
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or parts.username or parts.password:
        raise ValueError("renderer control requires a plain HTTP numeric-IP URL")
    address = ipaddress.ip_address(parts.hostname)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("renderer command deadline exceeded")
    sock = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET,
                         socket.SOCK_STREAM)
    expired = threading.Event()

    def abort():
        expired.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    watchdog = threading.Timer(remaining, abort)
    watchdog.daemon = True
    connection = http.client.HTTPConnection(parts.hostname, parts.port or 80,
                                             timeout=remaining)
    response = None
    watchdog.start()
    try:
        sock.settimeout(remaining)
        sock.connect((str(address), parts.port or 80))
        connection.sock = sock
        target = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
        connection.request("POST" if data is not None else "GET", target,
                           body=data, headers=headers or {})
        response = connection.getresponse()
        body = response.read(max_bytes + 1)
        if expired.is_set() or time.monotonic() >= deadline:
            raise TimeoutError("renderer command deadline exceeded")
        if len(body) > max_bytes:
            raise ValueError("renderer response exceeds size limit")
        if not 200 <= response.status < 300 and response.status not in allowed_statuses:
            raise OSError(f"renderer HTTP status {response.status}")
        return body
    finally:
        watchdog.cancel()
        watchdog.join()
        if response is not None:
            response.close()
        connection.close()
        sock.close()


PlaybackSnapshot = collections.namedtuple(
    "PlaybackSnapshot", "session revision title artist album art_id artwork url")


def renderer_base_url(ip):
    host = f"[{ip}]" if ":" in ip else ip
    return f"http://{host}:{RENDERER_PORT}"


def description_control(description, ip, service):
    """Parse a service endpoint without allowing discovery to change the target."""
    root = ElementTree.fromstring(description)
    device = next((item for item in root.iter()
                   if item.tag.rsplit("}", 1)[-1] == "device"), None)
    speaker_name = None
    if device is not None:
        name = next((item.text for item in device
                     if item.tag.rsplit("}", 1)[-1] == "friendlyName"), None)
        speaker_name = name.strip()[:256] if name and name.strip() else None
    # Newer service versions include version-one functionality (UPnP UDA 2.0 §1.2.2).
    expected = re.compile(rf"urn:schemas-upnp-org:service:{re.escape(service)}:0*[1-9][0-9]*")
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] != "service":
            continue
        values = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in item}
        if not expected.fullmatch(values.get("serviceType", "")) or not values.get("controlURL"):
            continue
        control = urllib.parse.urljoin(renderer_base_url(ip) + "/", values["controlURL"])
        parts = urllib.parse.urlsplit(control)
        try:
            same_target = (parts.scheme == "http" and parts.username is None
                           and parts.password is None and parts.hostname is not None
                           and ipaddress.ip_address(parts.hostname) == ipaddress.ip_address(ip)
                           and parts.port == RENDERER_PORT)
        except ValueError:
            same_target = False
        if not same_target:
            raise ValueError("Renderer control URL changed the target")
        return control, speaker_name
    return None, speaker_name


def soap_request(name, args, service):
    """Build the shared SOAP request; each worker handles its own response."""
    srv = f"urn:schemas-upnp-org:service:{service}:1"
    body = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{name} xmlns:u="{srv}"><InstanceID>0</InstanceID>{args}'
            f'</u:{name}></s:Body></s:Envelope>')
    return body.encode(), {"Content-Type": 'text/xml; charset="utf-8"',
                           "SOAPACTION": f'"{srv}#{name}"'}


class Renderer:
    """Nonblocking publishers plus one worker that owns all renderer I/O."""
    RETRY_DELAYS = (0.25, 0.5, 1.0)

    def __init__(self, ip):
        self.ip, self.controls = ip, {}
        self.stop_revision = self.stopped_revision = 0
        self.volume = None
        self.volume_revision = self.resume_revision = 0
        self.resume_session = None
        self.job = None
        self.retries = {}
        self.stop_session = None

    def resolve(self, service, deadline):
        if not self.ip:
            return None
        if service not in self.controls:
            desc = http_req(renderer_base_url(self.ip) + "/description.xml", deadline=deadline)
            control, _ = description_control(desc, self.ip, service)
            if control is None:
                raise ValueError(f"renderer has no {service} control URL")
            self.controls[service] = control
        return self.controls.get(service)

    def action(self, name, args, service="AVTransport"):
        """Worker-only I/O. Return failure explicitly, including SOAP faults."""
        global LAST_COMMAND
        success = False
        deadline = time.monotonic() + HTTP_TIMEOUT
        try:
            control = self.resolve(service, deadline)
            if not control:
                return False
            srv = f"urn:schemas-upnp-org:service:{service}:1"
            body, headers = soap_request(name, args, service)
            response = http_req(control, data=body, headers=headers, deadline=deadline,
                max_bytes=SOAP_BODY_MAX)
            document = ElementTree.fromstring(response)
            if any(element.tag.rsplit("}", 1)[-1] == "Fault" for element in document.iter()):
                raise ValueError("renderer returned a SOAP fault")
            expected = f"{{{srv}}}{name}Response"
            if not any(element.tag == expected for element in document.iter()):
                raise ValueError(f"renderer omitted {name}Response")
            success = True
            return True
        except Exception as error:
            log(f"renderer {name} failed: {error}")
            return False
        finally:
            with COMMAND_LOCK:
                LAST_COMMAND = {"name": name, "accepted": success, "at": time.time()}

    @staticmethod
    def uri_args(snapshot):
        title = escape(snapshot.title or "AirPlay 2")
        artist, album = escape(snapshot.artist), escape(snapshot.album)
        art = ""
        if snapshot.artwork:
            art_url = snapshot.url.rsplit("/", 1)[0] + f"/art-{snapshot.art_id}.jpg"
            art = f'<upnp:albumArtURI>{escape(art_url)}</upnp:albumArtURI>'
        didl = escape(
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            f'<item id="1" parentID="0" restricted="1"><dc:title>{title}</dc:title>'
            f'<dc:creator>{artist}</dc:creator><upnp:artist>{artist}</upnp:artist>'
            f'<upnp:album>{album}</upnp:album>{art}'
            '<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>'
            f'<res protocolInfo="http-get:*:audio/flac:*">{escape(snapshot.url)}</res></item></DIDL-Lite>')
        return (f"<CurrentURI>{escape(snapshot.url)}</CurrentURI>"
                f"<CurrentURIMetaData>{didl}</CurrentURIMetaData>")

    def set_volume(self, vol):
        with AUDIO.lock:
            self.volume_revision += 1
            self.volume = (AUDIO.session_id, vol, self.volume_revision)

    def resume(self, session_id):
        with AUDIO.lock:
            if session_id != AUDIO.session_id or not STATE["active"]:
                return
            self.resume_revision += 1
            self.resume_session = (session_id, self.resume_revision)

    def stop(self, session_id=None):
        with AUDIO.lock:
            self.stop_revision += 1
            self.stop_session = AUDIO.session_id if session_id is None else session_id
            # Clear only commands belonging to the ending/rejected session.
            if self.resume_session and self.resume_session[0] == self.stop_session:
                self.resume_session = None
            if self.volume and self.volume[0] == self.stop_session:
                self.volume = None

    def _current(self, snapshot):
        return (STATE["active"] and AUDIO.snapshot() is not None
                and AUDIO.session_id == snapshot.session
                and STATE["revision"] == snapshot.revision)

    def _eligible(self, kind, value, now):
        retry = self.retries.get(kind)
        if retry is None or retry["value"] != value:
            retry = self.retries[kind] = dict(value=value, failures=0, at=0.0, exhausted=False)
        return not retry["exhausted"] and now >= retry["at"]

    @staticmethod
    def _settle():
        if STATE["pushed"] is None and NOW_PLAYING["title"]:
            return INITIAL_PUSH_SETTLE
        return PUSH_SETTLE

    def _select(self, now):
        """Coalesce work under AUDIO.lock; independent backoffs cannot monopolize it."""
        if self.stop_revision != self.stopped_revision:
            stop = self.stop_revision
            retry = self.retries.get("stop")
            replacement = (STATE["active"] and AUDIO.snapshot() is not None
                           and AUDIO.session_id != self.stop_session)
            if replacement and retry and retry["value"] == stop and retry["failures"]:
                # A valid replacement URI reconciles uncertain effects of the old Stop.
                self.stopped_revision = stop
            elif self._eligible("stop", stop, now):
                return ("stop", stop)
        if self.job and not self._current(self.job[0]):
            self.job = None
        if not self.job and (STATE["active"] and AUDIO.snapshot() is not None and STATE["dirty"]
                             and now - STATE["dirty"] >= self._settle()):
            snapshot = PlaybackSnapshot(AUDIO.session_id, STATE["revision"],
                                       NOW_PLAYING["title"], NOW_PLAYING["artist"],
                                       NOW_PLAYING["album"], ART["id"], bool(ART["bytes"]),
                                       STREAM_URL)
            display = (snapshot.title, snapshot.artist, snapshot.album, snapshot.art_id)
            if display == STATE["pushed"] or (STATE["pushed"] is not None and not DIDL_PUSH):
                STATE["dirty"] = 0.0
            else:
                self.job = (snapshot, "uri")
                log_timing("playback_ready", snapshot.session, snapshot.revision,
                           reason="initial" if STATE["pushed"] is None else "refresh",
                           settle_ms=round(self._settle() * 1000, 1),
                           dirty_ms=round((now - STATE["dirty"]) * 1000, 1))
        if self.job and self._eligible("playback", (self.job[0], self.resume_revision), now):
            return ("playback", self.job[0])
        if self.resume_session is not None:
            session = self.resume_session[0]
            if not STATE["active"] or AUDIO.snapshot() is None or session != AUDIO.session_id:
                self.resume_session = None
            elif (STATE["pushed"] is not None or (self.job and self.job[1] == "play")):
                # A bare Play is safe only after this session's URI was installed.
                if self._eligible("resume", self.resume_session, now):
                    return ("resume", self.resume_session)
        if self.volume is not None:
            if self.volume[0] != AUDIO.session_id:
                self.volume = None
            elif self._eligible("volume", self.volume, now):
                return ("volume", self.volume)
        return None

    def _ack_playback(self, snapshot):
        STATE["pushed"] = (snapshot.title, snapshot.artist, snapshot.album, snapshot.art_id)
        STATE["dirty"] = 0.0
        self.job = None
        if self.resume_session and self.resume_session[0] == snapshot.session:
            self.resume_session = None

    def dispatch_once(self, now=None):
        """Single worker iteration; separate method enables deterministic races."""
        now = time.monotonic() if now is None else now
        with AUDIO.lock:
            work = self._select(now)
            if work is None:
                return False
            kind, value = work
            if kind == "playback":
                stage = self.job[1]
                # Cancellation/replacement can change selection between actions.
                if not self._current(value):
                    return False
                name, args = (("SetAVTransportURI", self.uri_args(value)) if stage == "uri"
                              else ("Play", "<Speed>1</Speed>"))
            elif kind == "stop":
                name, args = "Stop", ""
            elif kind == "resume":
                name, args = "Play", "<Speed>1</Speed>"
            else:
                name, args = "SetVolume", ("<Channel>Master</Channel>"
                                           f"<DesiredVolume>{value[1]}</DesiredVolume>")
            session = value.session if kind == "playback" else (
                self.stop_session if kind == "stop" else value[0])
            revision = value.revision if kind == "playback" else (
                value if kind == "stop" else value[-1])
            attempt = self.retries[kind]["failures"] + 1
        started_at = time.monotonic()
        log_timing("command_start", session, revision, kind=kind, action=name, attempt=attempt)
        success = self.action(name, args, "RenderingControl" if kind == "volume" else "AVTransport")
        log_timing("command_end", session, revision, kind=kind, action=name, attempt=attempt,
                   result="success" if success else "failure",
                   duration_ms=round((time.monotonic() - started_at) * 1000, 1))
        with AUDIO.lock:
            retry = self.retries[kind]
            if not success:
                if retry["failures"] < len(self.RETRY_DELAYS):
                    retry["at"] = time.monotonic() + self.RETRY_DELAYS[retry["failures"]]
                else:
                    retry["exhausted"] = True
                    log(f"renderer {name} retries exhausted; waiting for changed desired state")
                retry["failures"] += 1
                return True
            retry.update(failures=0, at=0.0, exhausted=False)
            if kind == "stop":
                self.stopped_revision = value
            elif kind == "volume" and self.volume == value:
                self.volume = None
            elif kind == "resume" and self.resume_session == value:
                self.resume_session = None
                if self.job and self.job[1] == "play" and self._current(self.job[0]):
                    self._ack_playback(self.job[0])
            elif kind == "playback" and self.job and self.job[0] == value:
                if not self._current(value):
                    self.job = None
                elif stage == "uri":
                    self.job = (value, "play")
                else:
                    self._ack_playback(value)
        return True

    def run(self):
        while True:
            self.dispatch_once()
            time.sleep(0.05)


class UnsupportedObservation(Exception):
    pass


class RecipientObserver:
    """Read-only device I/O, isolated from the playback dispatcher and its cache."""
    def __init__(self, ip):
        self.ip = ip
        self.controls = {}
        self.lock = threading.Lock()
        self.name = None
        self.fields = {name: {"value": None, "at": None, "error": None}
                       for name in ("playback", "volume")}
        self.stopping = threading.Event()

    def read(self, name, args, service, field, deadline):
        if service not in self.controls:
            desc = http_req(renderer_base_url(self.ip) + "/description.xml", deadline=deadline)
            control, speaker_name = description_control(desc, self.ip, service)
            with self.lock:
                self.name = speaker_name
            if control is None:
                raise UnsupportedObservation("Service unavailable")
            self.controls[service] = control
        srv = f"urn:schemas-upnp-org:service:{service}:1"
        body, headers = soap_request(name, args, service)
        raw = http_req(self.controls[service], data=body, headers=headers,
                       deadline=deadline, max_bytes=SOAP_BODY_MAX, allowed_statuses=(500,))
        root = ElementTree.fromstring(raw)
        elements = list(root.iter())
        fault = next((item for item in elements if item.tag.rsplit("}", 1)[-1] == "Fault"), None)
        if fault is not None:
            codes = [item.text for item in fault.iter() if item.tag.rsplit("}", 1)[-1] == "errorCode"]
            if "401" in codes:
                raise UnsupportedObservation("Action unavailable")
            raise ValueError("Recipient query failed")
        response = next((item for item in elements if item.tag == f"{{{srv}}}{name}Response"), None)
        if response is None:
            raise ValueError("Invalid recipient response")
        value = next((item.text for item in response.iter()
                      if item.tag.rsplit("}", 1)[-1] == field), None)
        if value is None:
            raise ValueError("Missing recipient state")
        if name == "GetVolume":
            volume = int(value)
            if not 0 <= volume <= 100:
                raise ValueError("Invalid volume")
            return volume
        allowed = {"PLAYING", "PAUSED_PLAYBACK", "STOPPED", "TRANSITIONING", "NO_MEDIA_PRESENT"}
        if value not in allowed:
            raise UnsupportedObservation("Playback state unavailable")
        return value

    def observe_once(self):
        if not self.ip:
            return
        cycle_start = time.monotonic()
        for key, name, args, service, field in (
            ("playback", "GetTransportInfo", "", "AVTransport", "CurrentTransportState"),
            ("volume", "GetVolume", "<Channel>Master</Channel>", "RenderingControl", "CurrentVolume")
        ):
            # Give playback at most half the cycle; volume keeps the remaining
            # budget even if the playback endpoint times out.
            deadline = cycle_start + (1 if key == "playback" else 2)
            try:
                value = self.read(name, args, service, field, deadline)
            except UnsupportedObservation:
                with self.lock:
                    self.fields[key] = {"value": None, "at": time.time(), "error": "unavailable"}
            except Exception:
                with self.lock:
                    self.fields[key]["error"] = "connection"
                # Re-discover endpoints next time after failures or device restarts.
                self.controls.pop(service, None)
            else:
                with self.lock:
                    self.fields[key] = {"value": value, "at": time.time(), "error": None}

    def snapshot(self):
        now = time.time()
        with self.lock:
            result = {name: dict(item, stale=item["at"] is not None and now - item["at"] > 15)
                      for name, item in self.fields.items()}
            result["name"] = self.name
        result["configured"] = bool(self.ip)
        return result

    def run(self):
        next_check = 0.0
        while not self.stopping.is_set():
            with VIEWERS:
                if not VIEWER_COUNT:
                    VIEWERS.wait(1)
                    next_check = 0.0
                    continue
            now = time.monotonic()
            if now >= next_check:
                self.observe_once()
                next_check = time.monotonic() + 5
            self.stopping.wait(0.25)


def status_snapshot():
    """Only small in-memory reads; never access the recipient or start an encoder."""
    now = time.monotonic()
    with AUDIO.lock:
        state = ("error" if AUDIO.failed else
                 "waiting" if AUDIO.session_id is not None and AUDIO.pcm is None else
                 "receiving" if AUDIO.active and AUDIO.first_audio is not None else "idle")
        pcm = ({"rate": AUDIO.pcm.rate, "bits": AUDIO.pcm.bits, "channels": AUDIO.pcm.channels}
               if AUDIO.pcm else None)
        output_stream = ({"codec": "FLAC", "rate": AUDIO.pcm.rate,
                          "bits": AUDIO.pcm.flac_bits, "channels": AUDIO.pcm.channels}
                         if AUDIO.pcm else None)
        audio = {"state": state, "session": AUDIO.session_id,
                 "codec": AUDIO.source_codec, "stream_type": AUDIO.stream_type,
                 "source_format": AUDIO.source_description, "output_stream": output_stream,
                 "aac_bitrate_bps": AUDIO.aac_bitrate_bps,
                 "receiver_stats": dict(AUDIO.receiver_stats) if AUDIO.receiver_stats is not None else None,
                 "airplay_version": {"Classic": 1, "Realtime": 2, "Buffered": 2}.get(AUDIO.stream_type),
                 "age_seconds": round(now - AUDIO.started, 1) if AUDIO.started is not None else None,
                 "format": pcm, "pending_bytes": len(AUDIO.pending), "buffered_bytes": 0,
                 "raw_bytes": AUDIO.raw_bytes, "discarded_bytes": AUDIO.discarded_bytes}
        if AUDIO.ring is not None:
            with AUDIO.ring.cond:
                audio["buffered_bytes"] = len(AUDIO.ring.buf)
        track = {key: NOW_PLAYING[key][:4096] for key in ("title", "artist", "album")}
        track["artwork"] = f'/art-{ART["id"]}.jpg' if ART["bytes"] and AUDIO.active else None
        if not AUDIO.active:
            track.update(title="", artist="", album="", artwork=None)
    with CLIENTS_LOCK:
        connections = sum(not item.cancelled.is_set() and not item.ring.closed for item in CLIENTS)
    with COMMAND_LOCK:
        command = dict(LAST_COMMAND) if LAST_COMMAND else None
    recipient = OBSERVER.snapshot() if OBSERVER else {
        "configured": bool(RENDERER_IP),
        "playback": {"value": None, "at": None, "error": None, "stale": False},
        "volume": {"value": None, "at": None, "error": None, "stale": False}}
    return {"instance": INSTANCE_ID, "at": time.time(), "uptime_seconds": int(now - BOOT_TIME), "audio": audio,
            "track": track, "connections": connections, "command": command, "recipient": recipient}


# ---------------------------------------------------------- shairport metadata
def _walk_dicts(obj):
    # Binary plists can contain shared references or cycles.
    stack, seen, exhausted = [iter((obj,))], set(), object()
    while stack:
        value = next(stack[-1], exhausted)
        if value is exhausted:
            stack.pop()
        elif isinstance(value, (dict, list, tuple)) and id(value) not in seen:
            seen.add(id(value))
            if isinstance(value, dict):
                yield value
                stack.append(iter(value.values()))
            else:
                stack.append(iter(value))


def metadata_text(value):
    """Accept one bounded UTF-8 track field without truncating it."""
    if not isinstance(value, str) or len(value) > META_TEXT_MAX:
        return None
    try:
        return value if len(value.encode("utf-8")) <= META_TEXT_MAX else None
    except UnicodeError:
        return None


def handle_copl(data):
    if not isinstance(data, bytes) or len(data) > META_PAYLOAD_MAX:
        return
    with AUDIO.lock:
        _handle_copl_locked(data)


def _handle_copl_locked(data):
    """AirPlay 2 sends now-playing info as 'copl' items: a binary plist with
    kMRMediaRemoteNowPlayingInfo* keys (title/artist/album/artwork). Frequent
    partial updates change only the fields that they include."""
    try:
        pl = plistlib.loads(data)
    except Exception:
        return
    fields = {}
    art = mime = None
    for d in _walk_dicts(pl):
        for k, v in d.items():
            if not isinstance(k, str):
                continue
            if k.endswith("NowPlayingInfoTitle"):
                fields["title"] = metadata_text(v)
            elif k.endswith("NowPlayingInfoArtist"):
                fields["artist"] = metadata_text(v)
            elif k.endswith("NowPlayingInfoAlbum"):
                fields["album"] = metadata_text(v)
            elif k.endswith("NowPlayingInfoArtworkData"):
                art = v
            elif k.endswith("NowPlayingInfoArtworkMIMEType"):
                mime = v

    update_artwork(art, mime)

    update_track_fields(fields)


def update_track_fields(fields):
    """Merge valid fields under AUDIO.lock, including explicit empty values."""
    changed = {}
    for key, candidate in fields.items():
        if key not in ("title", "artist", "album"):
            continue
        value = metadata_text(candidate)
        if value is not None and value != NOW_PLAYING[key]:
            changed[key] = value
    if changed:
        NOW_PLAYING.update(changed)
        mark_metadata_dirty()
        log(f"now playing: {NOW_PLAYING['artist']} - {NOW_PLAYING['title']}")


def handle_classic_metadata(typ, code, data, pending):
    """Start with empty pending fields and publish only the completed update."""
    if typ == "ssnc" and code == "mdst":
        pending.clear()
    elif typ == "core" and code in ("minm", "asar", "asal"):
        value = metadata_text(data.decode("utf-8", errors="replace"))
        if value is not None:
            pending[code] = value
    elif typ == "ssnc" and code == "mden":
        with AUDIO.lock:
            update_track_fields({field: pending[code]
                                 for code, field in (("minm", "title"), ("asar", "artist"), ("asal", "album"))
                                 if code in pending})
            pending.clear()
    else:
        return False
    return True


ITEM = re.compile(
    rb'<item><type>([0-9a-f]{8})</type><code>([0-9a-f]{8})</code>'
    rb'<length>([0-9]{1,8})</length>(?:\s*<data encoding="base64">(.*?)</data>)?\s*</item>',
    re.S)
ITEM_HEADER = re.compile(
    rb'<item><type>([0-9a-f]{8})</type><code>([0-9a-f]{8})</code>'
    rb'<length>([0-9]{1,8})</length>')


class MetadataParser:
    """Bound incomplete items and resume at the next item after invalid input."""
    def __init__(self):
        self.buffer = bytearray()
        self.header = None
        self.scan = 0

    def _discard(self, count):
        del self.buffer[:count]
        self.header = None
        self.scan = 0

    def feed(self, chunk):
        # Keep the same bound even if a caller supplies a large read.
        for offset in range(0, len(chunk), 4096):
            self.buffer.extend(chunk[offset:offset + 4096])
            yield from self._items()

    def _items(self):
        while self.buffer:
            start = self.buffer.find(b"<item>")
            if start < 0:
                # Retain only a possible opening tag split across reads.
                keep = next((n for n in range(5, 0, -1)
                             if self.buffer.endswith(b"<item>"[:n])), 0)
                self._discard(len(self.buffer) - keep)
                return
            if start:
                self._discard(start)
            if self.header is None:
                end = self.buffer.find(b"</length>", 0, 256)
                if end < 0:
                    if len(self.buffer) < 256:
                        return
                    self._discard(6)
                    continue
                match = ITEM_HEADER.fullmatch(self.buffer[:end + 9])
                if match is None:
                    self._discard(6)
                    continue
                typ, code = (bytes.fromhex(match[n].decode()).decode(errors="replace")
                             for n in (1, 2))
                length = int(match[3])
                limit = (META_TEXT_MAX if typ == "core" and code in ("minm", "asar", "asal")
                         else ART_ITEM_MAX if typ == "ssnc" and code == "PICT"
                         else META_PAYLOAD_MAX)
                if length > limit:
                    self._discard(6)
                    continue
                self.header = (typ, code, length)
                self.scan = end + 9
            end = self.buffer.find(b"</item>", self.scan)
            nested = self.buffer.find(b"<item>", self.scan)
            if nested >= 0 and (end < 0 or nested < end):
                self._discard(nested)
                continue
            if end < 0:
                if len(self.buffer) >= META_ITEM_MAX:
                    self._discard(6)
                    continue
                self.scan = max(self.scan, len(self.buffer) - 6)
                return
            typ, code, length = self.header
            match = ITEM.fullmatch(self.buffer[:end + 7]) if end + 7 <= META_ITEM_MAX else None
            self._discard(end + 7)
            if match is None:
                continue
            encoded = re.sub(rb"[ \t\r\n]", b"", match[4] or b"")
            if len(encoded) != 4 * ((length + 2) // 3):
                continue
            try:
                data = base64.b64decode(encoded, validate=True)
            except ValueError:
                continue
            if len(data) == length:
                yield typ, code, data


def handle_output_description(data, wiim):
    try:
        with AUDIO.lock:
            changed = AUDIO.describe(data)
            if changed:
                STATE["pushed"] = None
                mark_metadata_dirty()
    except ValueError as error:
        with AUDIO.lock:
            AUDIO.fail(str(error))
            wiim.stop(AUDIO.session_id)
        drop_clients("invalid output description")
        log(f"odsc rejected: {error}; stopping renderer; {AUDIO.status()}")


def handle_playback_metadata(code, data, wiim, pending):
    """Handle the real metadata events as well as direct unit-test sequences."""
    if code in ("sdsc", "styp"):
        try:
            value = data.decode("ascii")
        except UnicodeError:
            return True
        with AUDIO.lock:
            if code == "styp" and value in ("Classic", "Realtime", "Buffered"):
                AUDIO._ensure_session()
                if AUDIO.stream_type != value:
                    AUDIO.aac_bitrate_bps = None
                AUDIO.stream_type = value
            elif code == "sdsc" and re.fullmatch(r"(ALAC|AAC|PCM)/[0-9]{4,6}/[A-Z0-9_]+/(?:[1-8]|5\.1|7\.1)", value):
                AUDIO._ensure_session()
                if AUDIO.source_description != value:
                    AUDIO.aac_bitrate_bps = None
                AUDIO.source_description = value
                AUDIO.source_codec = value.split("/", 1)[0]
        return True
    elif code == "arst":
        # Player counters are session totals, not network packet-loss estimates.
        if not re.fullmatch(rb"[0-9]{1,20}/[0-9]{1,20}/[0-9]{1,20}", data):
            return True
        counts = tuple(map(int, data.split(b"/")))
        with AUDIO.lock:
            if (AUDIO.session_id is not None and AUDIO.accepting and not AUDIO.failed
                    and all(value <= (1 << 53) - 1 for value in counts)):
                AUDIO.receiver_stats = dict(zip(
                    ("missing_audio_blocks", "too_late_audio_blocks", "retry_requests"), counts))
        return True
    elif code == "abrt":
        # Custom receiver counters describe compressed AAC, never decoded PCM.
        if not re.fullmatch(rb"[0-9]{1,20}/[0-9]{1,20}/[0-9]{1,5}", data):
            return True
        payload_bytes, samples, rate = map(int, data.split(b"/"))
        with AUDIO.lock:
            if (payload_bytes, samples, rate) == (0, 0, 0):
                AUDIO.aac_bitrate_bps = None
            elif (AUDIO.source_codec == "AAC" and AUDIO.session_id is not None
                  and AUDIO.accepting and not AUDIO.failed and rate in (44100, 48000)
                  and rate == int(AUDIO.source_description.split("/")[1])
                  and 0 < payload_bytes <= (1 << 64) - 1
                  and 2 * rate <= samples <= (1 << 64) - 1):
                bps = (payload_bytes * 8 * rate + samples // 2) // samples
                if 0 < bps <= 10_000_000:
                    AUDIO.aac_bitrate_bps = bps
        return True
    elif code == "pfls":
        with AUDIO.lock:
            AUDIO.aac_bitrate_bps = None
    elif code == "odsc":
        handle_output_description(data, wiim)
    elif code == "pbeg":
        with AUDIO.lock:
            if not STATE["active"]:
                AUDIO.begin()
                pending.clear()
                NOW_PLAYING.update(title="", artist="", album="", artwork="")
                ART.update(id=None, bytes=b"")
                STATE.update(active=True, pushed=None)
                mark_metadata_dirty()
            else:
                log(f"duplicate pbeg ignored; {AUDIO.status()}")
    elif code == "pend":
        with AUDIO.lock:
            session_id = AUDIO.session_id
            AUDIO.end("pend received")
            pending.clear()
            NOW_PLAYING.update(title="", artist="", album="", artwork="")
            ART.update(id=None, bytes=b"")
            STATE.update(active=False, dirty=0.0, pushed=None)
            wiim.stop(session_id)
        cancel_session_clients(session_id, "session end")
    else:
        return False
    return True


def didl_pusher(wiim):
    """One renderer worker; metadata threads only publish desired state."""
    wiim.run()


def metadata_reader(wiim):
    threading.Thread(target=didl_pusher, args=(wiim,), daemon=True).start()
    pending = {}
    sequence = 0

    while True:
        try:
            # unbuffered: small metadata items must not wait for a full buffer
            log(f"waiting for metadata pipe writer path={META_PIPE}; {AUDIO.status()}")
            with open(META_PIPE, "rb", buffering=0) as f:
                log(f"metadata pipe open; {AUDIO.status()}")
                parser = MetadataParser()
                while True:
                    chunk = f.read(4096)
                    if not chunk:
                        break
                    for typ, code, data in parser.feed(chunk):
                        sequence += 1
                        if typ == "ssnc" and code in ("odsc", "sdsc", "pbeg", "pend", "pfls", "paus", "prsm", "styp"):
                            value = f" value={description_for_log(data)}" if code in ("sdsc", "styp") else ""
                            log(f"metadata event seq={sequence} code={code} bytes={len(data)}{value}; {AUDIO.status()}")

                        if typ == "ssnc" and handle_playback_metadata(code, data, wiim, pending):
                            continue
                        if handle_classic_metadata(typ, code, data, pending):
                            continue
                        if typ == "ssnc" and code == "copl" and data:
                            handle_copl(data)
                        elif typ == "ssnc" and code == "pvol":
                            # "airplay_volume,attenuation,low,high"; airplay_volume
                            # is -30..0 dB, -144 = mute -> renderer hardware volume
                            try:
                                av = float(data.decode().split(",")[0])
                                vol = 0 if av <= -144 else round((av + 30) / 30 * MAX_VOLUME)
                                wiim.set_volume(max(0, min(MAX_VOLUME, vol)))
                            except (ValueError, IndexError):
                                pass
                        elif typ == "ssnc" and code == "PICT" and data:
                            update_artwork(data)
            log(f"metadata pipe EOF unparsed_bytes={len(parser.buffer)} last_event_seq={sequence}; {AUDIO.status()}")
            with AUDIO.lock:
                session_id, was_active = AUDIO.session_id, STATE["active"]
                AUDIO.end("metadata pipe EOF")
                pending.clear()
                NOW_PLAYING.update(title="", artist="", album="", artwork="")
                ART.update(id=None, bytes=b"")
                STATE.update(active=False, dirty=0.0, pushed=None)
                if was_active:
                    wiim.stop(session_id)
            cancel_session_clients(session_id, "metadata pipe closed")
        except Exception as e:
            log(f"metadata pipe error: {e}; last_event_seq={sequence}; {AUDIO.status()}")
            with AUDIO.lock:
                AUDIO.fail("metadata pipe lost")
                pending.clear()
                NOW_PLAYING.update(title="", artist="", album="", artwork="")
                ART.update(id=None, bytes=b"")
                STATE.update(active=False, dirty=0.0, pushed=None)
                wiim.stop(AUDIO.session_id)
            drop_clients("metadata pipe lost")
            time.sleep(1)


def main():
    global OBSERVER
    log(f"PCM requires Shairport 5+ ssnc/odsc metadata; formats={','.join(PCMFormat.FORMATS)} "
        f"channels=1,2 pending_limit={PENDING_MAX} ring_seconds={RING_SECONDS} "
        f"backlog_seconds={BACKLOG_SECONDS}; session IDs are local to this bridge process")
    os.makedirs(os.path.dirname(AUDIO_PIPE), exist_ok=True)
    for p in (AUDIO_PIPE, META_PIPE):
        if not os.path.exists(p):
            os.mkfifo(p, 0o666)
    wiim = Renderer(RENDERER_IP)
    OBSERVER = RecipientObserver(RENDERER_IP)
    threading.Thread(target=OBSERVER.run, daemon=True).start()
    threading.Thread(target=audio_reader, args=(wiim,), daemon=True).start()
    threading.Thread(target=metadata_reader, args=(wiim,), daemon=True).start()
    log(f"serving FLAC+ICY on :{PORT}/stream.flac (diagnostic WAV on /stream.wav)")
    BoundedHTTPServer(("", PORT), StreamHandler).serve_forever()


if __name__ == "__main__":
    main()
