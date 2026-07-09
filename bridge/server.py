#!/usr/bin/env python3
"""AirPlay 2 -> UPnP/DLNA renderer bridge:
- raw PCM (s16le/44100/2) from the shairport-sync pipe goes into a ring buffer
  (a slow/stalled player can never back-pressure shairport into dropping audio)
- /stream.flac : per-client ffmpeg encodes PCM->FLAC from the join point,
  served with ICY metadata
- /stream.wav  : plain WAV, no ICY ever (diagnostic/fallback)
- the shairport metadata pipe drives UPnP Play/Stop, hardware volume, and
  now-playing DIDL pushes on the renderer
"""
import base64, os, plistlib, re, socket, struct, subprocess, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

AUDIO_PIPE = "/shared/audio"
META_PIPE = "/shared/metadata"
PORT = int(os.environ.get("STREAM_PORT", "8000"))
STREAM_URL = os.environ.get("STREAM_URL", "")
RENDERER_IP = os.environ.get("RENDERER_IP") or os.environ.get("WIIM_IP", "")
MAX_VOLUME = int(os.environ.get("MAX_VOLUME", "100"))

ICY_META_INT = 131072               # same interval airupnp uses
RING_MAX = 2 * 1024 * 1024          # ~11s of 44100/16/2 PCM
CLIENT_BACKLOG = 256 * 1024         # ~1.5s handed out instantly to fill the player prebuffer
FRAME = 4                           # bytes per PCM frame


def log(msg):
    print(f"[bridge] {msg}", flush=True)


class Ring:
    def __init__(self):
        self.buf = bytearray()
        self.head = 0               # absolute offset of buf[0]
        self.session_start = 0      # absolute offset where the current session began
        self.cond = threading.Condition()

    def write(self, data):
        with self.cond:
            self.buf += data
            if len(self.buf) > RING_MAX:
                cut = len(self.buf) - RING_MAX
                cut -= cut % FRAME
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
            pos = max(self.session_start, self.head, end - CLIENT_BACKLOG)
            return pos + (-pos) % FRAME

    def read_from(self, pos, timeout=5):
        """return (newpos, bytes) at absolute pos, waiting for data if at the end"""
        with self.cond:
            end = self.head + len(self.buf)
            if pos >= end:
                self.cond.wait(timeout)
                end = self.head + len(self.buf)
                if pos >= end:
                    return pos, b""
            if pos < self.head:     # fell behind: jump near the live edge
                pos = max(self.head, end - CLIENT_BACKLOG)
                pos += (-pos) % FRAME
            return end, bytes(self.buf[pos - self.head:])


RING = Ring()
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
STATE = {"active": False, "dirty": 0.0, "pushed": None}
CLIENTS = set()          # sockets of connected stream clients


def drop_clients(reason):
    RING.mark_session()
    n = 0
    for s in list(CLIENTS):
        try:
            s.shutdown(socket.SHUT_RDWR)
            n += 1
        except OSError:
            pass
    if n:
        log(f"{reason}: dropped {n} client(s) to resync")


def wav_header():
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, 44100, 44100 * 4, 4, 16)
            + b"data" + struct.pack("<I", 0xFFFFFFFF))


class StreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        log(f"http {self.address_string()} {fmt % args}")

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
        self.connection.settimeout(30)
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.end_headers()
        log(f"wav client {self.address_string()} connected")
        pos = RING.join_pos()
        CLIENTS.add(self.connection)
        try:
            self.wfile.write(wav_header())
            while True:
                pos, data = RING.read_from(pos)
                if data:
                    self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, socket.timeout, TimeoutError, OSError):
            log(f"wav client {self.address_string()} disconnected")
        finally:
            CLIENTS.discard(self.connection)

    # ---- main endpoint: FLAC + ICY, the combo airupnp uses on the WiiM ----
    def serve_flac(self):
        icy = self.headers.get("Icy-MetaData") == "1"
        self.connection.settimeout(30)
        self.send_response(200)
        self.send_header("Content-Type", "audio/flac")
        self.send_header("icy-name", "AirPlay 2")
        if icy:
            self.send_header("icy-metaint", str(ICY_META_INT))
        self.end_headers()
        log(f"flac client {self.address_string()} connected (icy={icy})")

        enc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0",
             "-c:a", "flac", "-compression_level", "0", "-flush_packets", "1",
             "-f", "flac", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)

        CLIENTS.add(self.connection)
        stop = threading.Event()

        def feeder():
            pos = RING.join_pos()
            try:
                while not stop.is_set():
                    pos, data = RING.read_from(pos)
                    if data:
                        enc.stdin.write(data)
                        enc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

        threading.Thread(target=feeder, daemon=True).start()

        sent_since_meta, last_meta = 0, None
        try:
            while True:
                data = enc.stdout.read1(8192)
                if not data:
                    break
                if not icy:
                    self.wfile.write(data)
                    continue
                while data:
                    room = ICY_META_INT - sent_since_meta
                    self.wfile.write(data[:room])
                    sent_since_meta += min(room, len(data))
                    data = data[room:]
                    if sent_since_meta == ICY_META_INT:
                        block, last_meta = self.icy_block(last_meta)
                        self.wfile.write(block)
                        sent_since_meta = 0
        except (BrokenPipeError, ConnectionResetError, socket.timeout, TimeoutError, OSError):
            log(f"flac client {self.address_string()} disconnected")
        finally:
            CLIENTS.discard(self.connection)
            stop.set()
            enc.kill()

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


def audio_reader(wiim):
    last = 0.0
    while True:
        try:
            # unbuffered: a fifo read must return whatever is available
            with open(AUDIO_PIPE, "rb", buffering=0) as f:
                log("audio pipe open")
                while True:
                    data = f.read(8192)
                    if not data:
                        break
                    now = time.time()
                    # audio resumed after a pause: clients were dropped, so kick
                    # the player to reconnect right away instead of on its retry
                    if last and now - last > 2 and STATE["active"] and not CLIENTS:
                        log("audio resumed, kicking player")
                        threading.Thread(target=wiim.action, daemon=True,
                                         args=("Play", "<Speed>1</Speed>")).start()
                    last = now
                    RING.write(data)
            log("audio pipe closed (session ended)")
        except Exception as e:
            log(f"audio pipe error: {e}")
            time.sleep(1)


# ------------------------------------------------------------ renderer control
def http_req(url, data=None, headers=None, timeout=5):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


class Renderer:
    def __init__(self, ip):
        self.ip, self.controls = ip, {}

    def resolve(self, service):
        if not self.ip:
            return None
        if service not in self.controls:
            try:
                desc = http_req(f"http://{self.ip}:49152/description.xml").decode(errors="replace")
                m = re.search(rf"<service>(?:(?!</service>).)*?{service}(?:(?!</service>).)*?"
                              r"<controlURL>([^<]+)</controlURL>", desc, re.S)
                if m:
                    path = m.group(1)
                    self.controls[service] = (f"http://{self.ip}:49152"
                                              f"{path if path.startswith('/') else '/' + path}")
                    log(f"renderer {service}: {self.controls[service]}")
            except Exception as e:
                log(f"renderer description fetch failed: {e}")
        return self.controls.get(service)

    def action(self, name, args, service="AVTransport"):
        control = self.resolve(service)
        if not control:
            return
        srv = f"urn:schemas-upnp-org:service:{service}:1"
        body = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{name} xmlns:u="{srv}"><InstanceID>0</InstanceID>{args}</u:{name}>'
                f'</s:Body></s:Envelope>')
        try:
            http_req(control, data=body.encode(), headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPACTION": f'"{srv}#{name}"'})
        except Exception as e:
            log(f"renderer {name} failed: {e}")

    def set_volume(self, vol):
        self.action("SetVolume",
                    f"<Channel>Master</Channel><DesiredVolume>{vol}</DesiredVolume>",
                    service="RenderingControl")
        log(f"renderer: volume {vol}")

    def play(self, url):
        title = escape(NOW_PLAYING["title"] or "AirPlay 2")
        artist = escape(NOW_PLAYING["artist"])
        album = escape(NOW_PLAYING["album"])
        art = ""
        if ART["bytes"]:
            art_url = url.rsplit("/", 1)[0] + "/art-%d.jpg" % ART["id"]
            art = f'<upnp:albumArtURI>{escape(art_url)}</upnp:albumArtURI>'
        didl = escape(
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            f'<item id="1" parentID="0" restricted="1"><dc:title>{title}</dc:title>'
            f'<dc:creator>{artist}</dc:creator><upnp:artist>{artist}</upnp:artist>'
            f'<upnp:album>{album}</upnp:album>{art}'
            '<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>'
            f'<res protocolInfo="http-get:*:audio/flac:*">{url}</res></item></DIDL-Lite>')
        self.action("SetAVTransportURI", f"<CurrentURI>{escape(url)}</CurrentURI>"
                                         f"<CurrentURIMetaData>{didl}</CurrentURIMetaData>")
        self.action("Play", "<Speed>1</Speed>")
        log(f"renderer: play ({NOW_PLAYING['artist']} - {NOW_PLAYING['title']}, art={bool(ART['bytes'])})")

    def stop(self):
        self.action("Stop", "")
        log("renderer: stop")


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
            STATE["dirty"] = time.time()
            log(f"artwork updated ({len(art)} bytes, {ART['mime']})")

    if isinstance(title, str) and title and (
            title != NOW_PLAYING["title"] or (artist or "") != NOW_PLAYING["artist"]):
        NOW_PLAYING["title"] = title
        NOW_PLAYING["artist"] = artist if isinstance(artist, str) else ""
        NOW_PLAYING["album"] = album if isinstance(album, str) else ""
        STATE["dirty"] = time.time()
        log(f"now playing: {NOW_PLAYING['artist']} - {NOW_PLAYING['title']}")


ITEM = re.compile(
    rb'<item><type>([0-9a-f]{8})</type><code>([0-9a-f]{8})</code>'
    rb'<length>(\d+)</length>(?:\n<data encoding="base64">\n?([A-Za-z0-9+/=\s]*?)</data>)?</item>',
    re.S)


def didl_pusher(wiim):
    """single place that talks to the renderer transport: initial play and (debounced)
    track-change DIDL refreshes; the player restarts the stream on each push"""
    while True:
        time.sleep(0.5)
        if not STATE["active"] or not STATE["dirty"]:
            continue
        if time.time() - STATE["dirty"] < PUSH_SETTLE:
            continue
        snap = (NOW_PLAYING["title"], NOW_PLAYING["artist"], NOW_PLAYING["album"], ART["id"])
        STATE["dirty"] = 0.0
        if snap == STATE["pushed"]:
            continue
        if STATE["pushed"] is not None and not DIDL_PUSH:
            continue    # initial play happened; mid-play refreshes disabled
        STATE["pushed"] = snap
        wiim.play(STREAM_URL)


def metadata_reader(wiim):
    threading.Thread(target=didl_pusher, args=(wiim,), daemon=True).start()
    pending = {}

    while True:
        try:
            # unbuffered: small metadata items must not wait for a full buffer
            with open(META_PIPE, "rb", buffering=0) as f:
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
                            h = hash(data)
                            if h != ART["hash"]:
                                ART.update(id=ART["id"] + 1, bytes=data, hash=h)
                                cache_art(ART["id"], data, ART["mime"])
                                NOW_PLAYING["artwork"] = "%s/art-%d.jpg" % (
                                    STREAM_URL.rsplit("/", 1)[0], ART["id"])
                                STATE["dirty"] = time.time()
                                log(f"artwork updated ({len(data)} bytes)")
                        elif typ == "ssnc" and code == "mden":
                            if pending.get("minm") and (
                                    pending.get("minm") != NOW_PLAYING["title"] or
                                    pending.get("asar", "") != NOW_PLAYING["artist"]):
                                NOW_PLAYING["title"] = pending.get("minm", "")
                                NOW_PLAYING["artist"] = pending.get("asar", "")
                                NOW_PLAYING["album"] = pending.get("asal", "")
                                STATE["dirty"] = time.time()
                                log(f"now playing: {NOW_PLAYING['artist']} - {NOW_PLAYING['title']}")
                        elif typ == "ssnc" and code in ("pfls", "paus") and FLUSH_RESYNC:
                            # seek/pause: make the player discard stale buffered
                            # audio so it reacts at the new position promptly
                            drop_clients("seek/pause" if code == "pfls" else "pause")
                        elif typ == "ssnc" and code == "pbeg" and not STATE["active"]:
                            RING.mark_session()
                            # clear any stale now-playing/artwork left by a previous
                            # session so a reconnect never pushes old metadata or art
                            pending.clear()
                            NOW_PLAYING.update(title="", artist="", album="", artwork="")
                            ART.update(bytes=b"", hash=None)
                            log("session begin")
                            # let the first metadata land, then didl_pusher starts playback
                            STATE.update(active=True, pushed=None, dirty=time.time())
                        elif typ == "ssnc" and code == "pend":
                            pending.clear()
                            NOW_PLAYING.update(title="", artist="", album="", artwork="")
                            ART.update(bytes=b"", hash=None)
                            STATE.update(active=False, dirty=0.0, pushed=None)
                            log("session end")
                            wiim.stop()
                    # a partial cover-art item can be ~700KB of base64: keep enough tail
                    buf = buf[pos:] if pos else buf[-2097152:]
        except Exception as e:
            log(f"metadata pipe error: {e}")
            time.sleep(1)


def main():
    os.makedirs(os.path.dirname(AUDIO_PIPE), exist_ok=True)
    for p in (AUDIO_PIPE, META_PIPE):
        if not os.path.exists(p):
            os.mkfifo(p, 0o666)
    wiim = Renderer(RENDERER_IP)
    threading.Thread(target=audio_reader, args=(wiim,), daemon=True).start()
    threading.Thread(target=metadata_reader, args=(wiim,), daemon=True).start()
    log(f"serving FLAC+ICY on :{PORT}/stream.flac (diagnostic WAV on /stream.wav)")
    ThreadingHTTPServer(("", PORT), StreamHandler).serve_forever()


if __name__ == "__main__":
    main()
