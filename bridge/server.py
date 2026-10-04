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
import base64, collections, os, plistlib, re, select, socket, struct, subprocess, threading, time, http.client, ipaddress, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape
from xml.etree import ElementTree

AUDIO_PIPE = "/shared/audio"
META_PIPE = "/shared/metadata"
PORT = int(os.environ.get("STREAM_PORT", "8000"))
STREAM_URL = os.environ.get("STREAM_URL", "")
RENDERER_IP = os.environ.get("RENDERER_IP") or os.environ.get("WIIM_IP", "")
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

    def encoder_command(self):
        bits = max(16, self.bits)  # FLAC stores 8-bit input losslessly as 16-bit samples.
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
    print(f"[bridge] {stamp}.{int(now % 1 * 1000):03d}Z {msg}", flush=True)


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
# seed the artwork id from the clock so /art-<id>.jpg URLs never repeat across
# restarts: a renderer that cached art-1.jpg from a previous run would otherwise
# show that stale cover for the first track instead of re-fetching the new one
ART = {"id": int(time.time()), "bytes": b"", "hash": None, "mime": "image/jpeg"}
# recent artworks kept by id: a renderer lagging behind rapid track changes may
# fetch an older /art-<id>.jpg after ART has moved on - serve THAT push's image,
# not whatever is current, so it never caches the wrong cover for a track
ART_CACHE = {}
ART_CACHE_MAX = 16


def cache_art(art_id, data, mime):
    ART_CACHE[art_id] = (data, mime)
    while len(ART_CACHE) > ART_CACHE_MAX:
        del ART_CACHE[min(ART_CACHE)]
# WiiM's display only reads DIDL (it ignores ICY), so a track change needs a
# SetAVTransportURI push; the player restarts the stream on it (~2-4s gap).
DIDL_PUSH = os.environ.get("DIDL_PUSH", "1") == "1"
# on seek/pause, drop stream clients so the player discards its buffered stale
# audio and reconnects at the new position (faster reaction to phone actions)
FLUSH_RESYNC = os.environ.get("FLUSH_RESYNC", "1") == "1"
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
        if self.path.startswith("/stream.flac"):
            self.serve_flac()
        elif self.path.startswith("/stream.wav"):
            self.serve_wav()
        elif self.path.startswith("/art-"):
            self.serve_art()
        else:
            self.send_error(404)

    def serve_art(self):
        m = re.search(r"/art-(\d+)", self.path)
        entry = ART_CACHE.get(int(m.group(1))) if m else None
        data, mime = entry if entry else (ART["bytes"], ART["mime"])
        if not data:
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
        cur = (NOW_PLAYING["title"], NOW_PLAYING["artist"], NOW_PLAYING["artwork"])
        if not cur[0] or cur == last:
            return b"\x00", last
        title, artist, art = cur
        song = f"{artist} - {title}" if artist else title
        s = f"StreamTitle='{song}';"
        if art:
            s += f"StreamUrl='{art}';"
        raw = s.encode("utf-8")
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
             max_bytes=HTTP_BODY_MAX):
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
        if not 200 <= response.status < 300:
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
            host = f"[{self.ip}]" if ":" in self.ip else self.ip
            desc = http_req(f"http://{host}:49152/description.xml",
                            deadline=deadline).decode(errors="replace")
            m = re.search(rf"<service>(?:(?!</service>).)*?{service}(?:(?!</service>).)*?"
                          r"<controlURL>([^<]+)</controlURL>", desc, re.S)
            if m:
                path = m.group(1)
                self.controls[service] = (f"http://{host}:49152"
                                          f"{path if path.startswith('/') else '/' + path}")
            else:
                raise ValueError(f"renderer has no {service} control URL")
        return self.controls.get(service)

    def action(self, name, args, service="AVTransport"):
        """Worker-only I/O. Return failure explicitly, including SOAP faults."""
        deadline = time.monotonic() + HTTP_TIMEOUT
        try:
            control = self.resolve(service, deadline)
            if not control:
                return False
            srv = f"urn:schemas-upnp-org:service:{service}:1"
            body = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                    f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                    f'<u:{name} xmlns:u="{srv}"><InstanceID>0</InstanceID>{args}</u:{name}>'
                    f'</s:Body></s:Envelope>')
            response = http_req(control, data=body.encode(), headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPACTION": f'"{srv}#{name}"'}, deadline=deadline,
                max_bytes=SOAP_BODY_MAX)
            document = ElementTree.fromstring(response)
            if any(element.tag.rsplit("}", 1)[-1] == "Fault" for element in document.iter()):
                raise ValueError("renderer returned a SOAP fault")
            expected = f"{{{srv}}}{name}Response"
            if not any(element.tag == expected for element in document.iter()):
                raise ValueError(f"renderer omitted {name}Response")
            return True
        except Exception as error:
            log(f"renderer {name} failed: {error}")
            return False

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


# ---------------------------------------------------------- shairport metadata
def _walk_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_dicts(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_dicts(v)


def handle_copl(data):
    with AUDIO.lock:
        _handle_copl_locked(data)


def _handle_copl_locked(data):
    """AirPlay 2 sends now-playing info as 'copl' items: a binary plist with
    kMRMediaRemoteNowPlayingInfo* keys (title/artist/album/artwork). Frequent
    partial updates (elapsed time only) carry no title and are ignored."""
    try:
        pl = plistlib.loads(data)
    except Exception:
        return
    title = artist = album = art = mime = None
    for d in _walk_dicts(pl):
        for k, v in d.items():
            if not isinstance(k, str):
                continue
            if k.endswith("NowPlayingInfoTitle"):
                title = v
            elif k.endswith("NowPlayingInfoArtist"):
                artist = v
            elif k.endswith("NowPlayingInfoAlbum"):
                album = v
            elif k.endswith("NowPlayingInfoArtworkData"):
                art = v
            elif k.endswith("NowPlayingInfoArtworkMIMEType"):
                mime = v

    if isinstance(art, bytes) and art:
        h = hash(art)
        if h != ART["hash"]:
            ART.update(id=ART["id"] + 1, bytes=art, hash=h,
                       mime=mime if isinstance(mime, str) and mime else "image/jpeg")
            cache_art(ART["id"], art, ART["mime"])
            NOW_PLAYING["artwork"] = "%s/art-%d.jpg" % (
                STREAM_URL.rsplit("/", 1)[0], ART["id"])
            mark_metadata_dirty()
            log(f"artwork updated ({len(art)} bytes, {ART['mime']})")

    if isinstance(title, str) and title and (
            title != NOW_PLAYING["title"] or (artist or "") != NOW_PLAYING["artist"]):
        NOW_PLAYING["title"] = title
        NOW_PLAYING["artist"] = artist if isinstance(artist, str) else ""
        NOW_PLAYING["album"] = album if isinstance(album, str) else ""
        mark_metadata_dirty()
        log(f"now playing: {NOW_PLAYING['artist']} - {NOW_PLAYING['title']}")


ITEM = re.compile(
    rb'<item><type>([0-9a-f]{8})</type><code>([0-9a-f]{8})</code>'
    rb'<length>(\d+)</length>(?:\n<data encoding="base64">\n?([A-Za-z0-9+/=\s]*?)</data>)?</item>',
    re.S)


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
    if code == "odsc":
        handle_output_description(data, wiim)
    elif code == "pbeg":
        with AUDIO.lock:
            if not STATE["active"]:
                AUDIO.begin()
                pending.clear()
                NOW_PLAYING.update(title="", artist="", album="", artwork="")
                ART.update(bytes=b"", hash=None)
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
            ART.update(bytes=b"", hash=None)
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
                buf = b""
                while True:
                    chunk = f.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    if b"</item>" not in buf:
                        continue
                    pos = 0
                    for m in ITEM.finditer(buf):
                        pos = m.end()
                        typ = bytes.fromhex(m.group(1).decode()).decode(errors="replace")
                        code = bytes.fromhex(m.group(2).decode()).decode(errors="replace")
                        data = base64.b64decode(m.group(4)) if m.group(4) else b""
                        sequence += 1
                        if typ == "ssnc" and code in ("odsc", "sdsc", "pbeg", "pend", "pfls", "paus", "prsm", "styp"):
                            value = f" value={description_for_log(data)}" if code in ("sdsc", "styp") else ""
                            log(f"metadata event seq={sequence} code={code} bytes={len(data)}{value}; {AUDIO.status()}")

                        if typ == "ssnc" and handle_playback_metadata(code, data, wiim, pending):
                            continue
                        if typ == "ssnc" and code == "copl" and data:
                            handle_copl(data)
                        elif typ == "core" and code in ("minm", "asar", "asal"):
                            pending[code] = data.decode("utf-8", errors="replace")
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
                            with AUDIO.lock:
                                h = hash(data)
                                if h != ART["hash"]:
                                    ART.update(id=ART["id"] + 1, bytes=data, hash=h)
                                    cache_art(ART["id"], data, ART["mime"])
                                    NOW_PLAYING["artwork"] = "%s/art-%d.jpg" % (
                                        STREAM_URL.rsplit("/", 1)[0], ART["id"])
                                    mark_metadata_dirty()
                                    log(f"artwork updated ({len(data)} bytes)")
                        elif typ == "ssnc" and code == "mden":
                            with AUDIO.lock:
                                if pending.get("minm") and (
                                        pending.get("minm") != NOW_PLAYING["title"] or
                                        pending.get("asar", "") != NOW_PLAYING["artist"]):
                                    NOW_PLAYING["title"] = pending.get("minm", "")
                                    NOW_PLAYING["artist"] = pending.get("asar", "")
                                    NOW_PLAYING["album"] = pending.get("asal", "")
                                    mark_metadata_dirty()
                                    log(f"now playing: {NOW_PLAYING['artist']} - {NOW_PLAYING['title']}")
                        elif typ == "ssnc" and code in ("pfls", "paus") and FLUSH_RESYNC:
                            # seek/pause: make the player discard stale buffered
                            # audio so it reacts at the new position promptly
                            drop_clients("seek/pause" if code == "pfls" else "pause")
                    # a partial cover-art item can be ~700KB of base64: keep enough tail
                    buf = buf[pos:] if pos else buf[-2097152:]
            log(f"metadata pipe EOF unparsed_bytes={len(buf)} last_event_seq={sequence}; {AUDIO.status()}")
            with AUDIO.lock:
                session_id, was_active = AUDIO.session_id, STATE["active"]
                AUDIO.end("metadata pipe EOF")
                STATE.update(active=False, dirty=0.0, pushed=None)
                if was_active:
                    wiim.stop(session_id)
            cancel_session_clients(session_id, "metadata pipe closed")
        except Exception as e:
            log(f"metadata pipe error: {e}; last_event_seq={sequence}; {AUDIO.status()}")
            with AUDIO.lock:
                AUDIO.fail("metadata pipe lost")
                STATE.update(active=False, dirty=0.0, pushed=None)
                wiim.stop(AUDIO.session_id)
            drop_clients("metadata pipe lost")
            time.sleep(1)


def main():
    log(f"PCM requires Shairport 5+ ssnc/odsc metadata; formats={','.join(PCMFormat.FORMATS)} "
        f"channels=1,2 pending_limit={PENDING_MAX} ring_seconds={RING_SECONDS} "
        f"backlog_seconds={BACKLOG_SECONDS}; session IDs are local to this bridge process")
    os.makedirs(os.path.dirname(AUDIO_PIPE), exist_ok=True)
    for p in (AUDIO_PIPE, META_PIPE):
        if not os.path.exists(p):
            os.mkfifo(p, 0o666)
    wiim = Renderer(RENDERER_IP)
    threading.Thread(target=audio_reader, args=(wiim,), daemon=True).start()
    threading.Thread(target=metadata_reader, args=(wiim,), daemon=True).start()
    log(f"serving FLAC+ICY on :{PORT}/stream.flac (diagnostic WAV on /stream.wav)")
    BoundedHTTPServer(("", PORT), StreamHandler).serve_forever()


if __name__ == "__main__":
    main()
