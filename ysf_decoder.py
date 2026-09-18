#!/usr/bin/env python3
"""
ysf_decoder.py — YSF (Yaesu System Fusion) reflector audio decoder.

Pipeline: YSF Reflector → YSFGateway → analog_bridge (YSF mode) → USRP UDP → ysf_decoder

Outputs
-------
1. Call upload   POST completed call WAVs to dispatcher /api/call-upload.
                 One POST per discrete transmission.
2. /stream       Live WAV stream for real-time monitoring (VLC, ffplay …)
3. /ws/audio     Raw 16-bit mono PCM WebSocket at AUDIO_RATE Hz
4. /status       JSON health / state

Usage
-----
  python3 ysf_decoder.py
  python3 ysf_decoder.py --config /path/to/config.json
  python3 ysf_decoder.py --listen-port 8082
  python3 ysf_decoder.py --debug
"""

import argparse
import asyncio
import json
import os
import queue as _q_mod
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
import uvicorn

# ── Audio constants ────────────────────────────────────────────────────────────
AUDIO_RATE  = 8_000                                # standard USRP/G.711 rate
CHUNK_MS    = 120                                  # PCM distribution granularity
CHUNK_SIZE  = AUDIO_RATE * 2 * CHUNK_MS // 1000   # bytes — 16-bit mono (1920)
SILENCE     = bytes(CHUNK_SIZE)

# ── USRP protocol constants ────────────────────────────────────────────────────
USRP_MAGIC      = b'USRP'
USRP_HDR_SIZE   = 32
USRP_AUDIO_SIZE = 320   # 160 samples × 2 bytes at 8 kHz = 20 ms per packet
USRP_TYPE_PCM   = 0
USRP_TYPE_META  = 1
USRP_PTT_OFF    = 0
USRP_PTT_ON     = 1

DEFAULT_CONFIG = "ysf_decoder_config.json"

# ── WAV helpers ────────────────────────────────────────────────────────────────

def _wav_stream_header(rate: int = AUDIO_RATE) -> bytes:
    data_size = 0xFFFFFFF0
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", data_size + 36, b"WAVE",
        b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16,
        b"data", data_size,
    )

def _wav_file(pcm: bytes, rate: int = AUDIO_RATE) -> bytes:
    n = len(pcm)
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", n + 36, b"WAVE",
        b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16,
        b"data", n,
    ) + pcm


# ── Call uploader ──────────────────────────────────────────────────────────────

class CallUploader:
    """POSTs completed call WAV files to the dispatcher's /api/call-upload endpoint."""

    def __init__(self, cfg: dict, freq_mhz: Optional[float] = None):
        self._url      = cfg["url"].rstrip("/") + "/api/call-upload"
        self._api_key  = cfg.get("api_key", "")
        self._system   = cfg.get("system", "YSF")
        self._tg       = str(cfg.get("talkgroup", 0))
        self._tg_tag   = cfg.get("talkgroup_tag", "YSF")
        self._tg_name  = cfg.get("talkgroup_name", "YSF Reflector")
        self._tg_group = cfg.get("talkgroup_group", "YSF")
        self._freq_hz  = str(int(freq_mhz * 1_000_000)) if freq_mhz else "0"

    def upload(self, pcm: bytes, start_time: float, duration: float,
               talkgroup: Optional[str] = None, source: Optional[str] = None):
        import urllib.request, urllib.parse

        tg    = talkgroup if talkgroup else self._tg
        wav   = _wav_file(pcm)
        fname = f"{self._system}_{tg}_{int(start_time)}.wav"

        fields = {
            "key":            self._api_key,
            "systemLabel":    self._system,
            "talkgroup":      tg,
            "dateTime":       str(int(start_time)),
            "frequency":      self._freq_hz,
            "talkgroupTag":   self._tg_tag,
            "talkgroupName":  self._tg_name,
            "talkgroupGroup": self._tg_group,
            "sources":        f'[{{"src":"{source}"}}]' if source else "[]",
        }

        boundary = b"----YsfDecoderBoundary"
        body     = bytearray()
        for k, v in fields.items():
            body += (
                b"--" + boundary + b"\r\n"
                b'Content-Disposition: form-data; name="' + k.encode() + b'"\r\n'
                b"\r\n" + v.encode() + b"\r\n"
            )
        body += (
            b"--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="audio"; filename="'
            + fname.encode() + b'"\r\n'
            b"Content-Type: audio/wav\r\n"
            b"\r\n" + wav + b"\r\n"
            b"--" + boundary + b"--\r\n"
        )

        print(f"[Upload] → {self._url}  tg={tg}  src={source}  system={self._system}  file={fname}  dur={duration:.1f}s")
        req = urllib.request.Request(
            self._url,
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                print(f"[Upload] ✓ HTTP {resp.getcode()}")
        except Exception as e:
            print(f"[Upload] Error posting {fname}: {e}", file=sys.stderr)


# ── Call detector (PTT-based state machine) ────────────────────────────────────

class CallDetector:
    """Buffers PCM during active transmissions (PTT-driven) and uploads on PTT off.

    States: IDLE → ACTIVE → HOLD → IDLE

    Unlike the RMS-based detector in dmr_decoder, this one is driven by the
    explicit PTT field in USRP packets from analog_bridge, which is more
    reliable for network audio.
    """

    _IDLE   = 0
    _ACTIVE = 1
    _HOLD   = 2

    def __init__(self, cfg: dict, uploader: Optional[CallUploader]):
        self._uploader   = uploader
        self._hold_secs  = float(cfg.get("squelch_hold", 0.3))
        self._min_length = float(cfg.get("min_call_length", 1.0))
        self._state      = self._IDLE
        self._buf        = bytearray()
        self._start      = 0.0
        self._hold_until = 0.0

        self._current_tg:  Optional[str] = None
        self._current_src: Optional[str] = None

        self.active   = False
        self.rx_count = 0

    def ptt_on(self, tg: Optional[str] = None, src: Optional[str] = None):
        """Signal transmission start."""
        if self._state == self._IDLE:
            self._state        = self._ACTIVE
            self._start        = time.time()
            self._buf          = bytearray()
            self.active        = True
            self.rx_count     += 1
            self._current_tg   = tg
            self._current_src  = src
        elif self._state == self._HOLD:
            # PTT bounced back before hold expired — extend the same call
            self._state = self._ACTIVE

    def ptt_off(self):
        """Signal transmission end; start hold timer."""
        if self._state == self._ACTIVE:
            self._state      = self._HOLD
            self._hold_until = time.time() + self._hold_secs

    def feed(self, pcm: bytes):
        """Feed a PCM chunk; advances hold timer in HOLD state."""
        if self._state in (self._ACTIVE, self._HOLD):
            self._buf.extend(pcm)

        if self._state == self._HOLD and time.time() >= self._hold_until:
            self._finalize()
            self._state = self._IDLE
            self.active = False

    def _finalize(self):
        pcm      = bytes(self._buf)
        duration = len(pcm) / (AUDIO_RATE * 2)
        if duration < self._min_length:
            return
        if self._uploader:
            threading.Thread(
                target=self._uploader.upload,
                args=(pcm, self._start, duration,
                      self._current_tg, self._current_src),
                daemon=True,
                name="call-upload",
            ).start()


# ── Audio post-processor ───────────────────────────────────────────────────────

class AudioProcessor:
    """Low-pass filter + gain trim for AMBE-decoded PCM from analog_bridge."""

    def __init__(self, rate: int = AUDIO_RATE, lp_hz: float = 3200.0,
                 gain: float = 0.85):
        self._gain       = float(gain)
        self._use_filter = False
        try:
            import numpy as np
            from scipy.signal import butter, lfilter_zi
            nyq          = rate / 2.0
            cutoff       = min(float(lp_hz), nyq - 1.0)
            b, a         = butter(2, cutoff / nyq, btype="low")
            self._b      = b
            self._a      = a
            self._zi     = lfilter_zi(b, a) * 0.0
            self._np     = np
            self._use_filter = True
        except ImportError:
            pass

    def process(self, data: bytes) -> bytes:
        if not self._use_filter:
            return data
        from scipy.signal import lfilter
        np      = self._np
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        filt, self._zi = lfilter(self._b, self._a, samples, zi=self._zi)
        out     = np.clip(filt * self._gain, -1.0, 1.0)
        return (out * 32767.0).astype(np.int16).tobytes()


# ── Audio broadcaster ─────────────────────────────────────────────────────────

class AudioBroadcaster:
    """Distributes real-time PCM from the USRP listener thread to asyncio consumers."""

    def __init__(self):
        self._clients: dict[int, asyncio.Queue] = {}
        self._lock    = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._next_id = 0

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def add(self) -> tuple[int, "asyncio.Queue[bytes]"]:
        q: asyncio.Queue = asyncio.Queue(maxsize=60)
        with self._lock:
            cid = self._next_id
            self._next_id += 1
            self._clients[cid] = q
        return cid, q

    def remove(self, cid: int):
        with self._lock:
            self._clients.pop(cid, None)

    def broadcast(self, pcm: bytes):
        if not self._loop:
            return
        with self._lock:
            queues = list(self._clients.values())
        for q in queues:
            try:
                self._loop.call_soon_threadsafe(q.put_nowait, pcm)
            except Exception:
                pass


# ── Icecast / Broadcastify feeder (optional) ──────────────────────────────────

class IcecastFeeder:
    """Encodes decoded 8 kHz PCM to MP3 and pushes to an Icecast server."""

    _RECONNECT_DELAY = 5.0

    def __init__(self, cfg: dict, label: str):
        server   = cfg["server"]
        port     = cfg.get("port", 80)
        password = cfg["password"]
        mount    = cfg["mountpoint"]
        self.url      = f"icecast://source:{password}@{server}:{port}{mount}"
        self.bitrate  = cfg.get("bitrate", 32)
        self._label   = label
        self._q: _q_mod.Queue = _q_mod.Queue(maxsize=200)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.connected   = False
        self.last_error: Optional[str] = None

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True,
                                         name="icecast-feeder")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=8.0)

    def send(self, pcm: bytes):
        try:
            self._q.put_nowait(pcm)
        except _q_mod.Full:
            try:
                self._q.get_nowait()
            except _q_mod.Empty:
                pass
            try:
                self._q.put_nowait(pcm)
            except _q_mod.Full:
                pass

    def _run(self):
        chunk_secs = CHUNK_SIZE / (AUDIO_RATE * 2)
        while self._running:
            proc = None
            try:
                cmd = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", "1",
                    "-i", "pipe:0",
                    "-c:a", "libmp3lame", "-b:a", f"{self.bitrate}k",
                    "-ice_name",        self._label,
                    "-ice_description", "YSF reflector decoder",
                    "-ice_genre",       "Scanner",
                    "-content_type",    "audio/mpeg",
                    "-f", "mp3", self.url,
                ]
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
                self.connected = True
                print(f"[Icecast] Connected → {self.url.split('@')[-1]}")

                next_tick = time.monotonic()
                while self._running:
                    if proc.poll() is not None:
                        stderr = proc.stderr.read().decode(errors="replace").strip()
                        raise RuntimeError(f"ffmpeg exited: {stderr or '(no output)'}")
                    try:
                        chunk = self._q.get_nowait()
                    except _q_mod.Empty:
                        chunk = SILENCE
                    try:
                        proc.stdin.write(chunk)
                        proc.stdin.flush()
                    except BrokenPipeError:
                        raise RuntimeError("ffmpeg stdin closed")
                    next_tick += chunk_secs
                    gap = next_tick - time.monotonic()
                    if gap > 0:
                        time.sleep(gap)
            except Exception as e:
                self.connected  = True
                self.last_error = str(e)
                print(f"[Icecast] Error: {e}", file=sys.stderr)
                if proc:
                    try:
                        proc.stdin.close()
                        proc.wait(timeout=3)
                    except Exception:
                        proc.kill()
                if self._running:
                    time.sleep(self._RECONNECT_DELAY)


# ── USRP packet parser ─────────────────────────────────────────────────────────

def _parse_usrp(data: bytes) -> Optional[dict]:
    if len(data) < USRP_HDR_SIZE or data[:4] != USRP_MAGIC:
        return None
    seq, tg, ptt, pkt_type, mpxid = struct.unpack_from('>IIIII', data, 4)
    return {
        "seq":     seq,
        "tg":      tg,
        "ptt":     ptt,
        "type":    pkt_type,
        "mpxid":   mpxid,
        "payload": data[USRP_HDR_SIZE:],
    }

def _build_usrp_keepalive() -> bytes:
    return struct.pack('>4sIIIII', USRP_MAGIC, 0, 0, 0, 0, 0) + bytes(8) + bytes(USRP_AUDIO_SIZE)


# ── YSF Decoder (USRP listener) ────────────────────────────────────────────────

class YSFDecoder:
    """Receives USRP audio from analog_bridge (YSF mode), detects calls, uploads."""

    def __init__(self, cfg: dict, bcast: AudioBroadcaster,
                 detect: CallDetector, feeder: Optional[IcecastFeeder],
                 debug: bool = False):
        self._cfg    = cfg
        self._bcast  = bcast
        self._detect = detect
        self._feeder = feeder
        self._debug  = debug
        self._proc   = AudioProcessor(
            lp_hz=cfg.get("audio_lp_hz", 3200.0),
            gain =cfg.get("audio_gain",  0.85),
        )
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self._current_tg:  Optional[str] = None
        self._current_src: Optional[str] = None
        self._usrp_connected = False

    # Public accessors for /status
    def get_tg(self) -> tuple[Optional[str], Optional[str]]:
        return self._current_tg, self._current_src

    @property
    def usrp_connected(self) -> bool:
        return self._usrp_connected

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True,
                                         name="ysf-usrp")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _loop(self):
        while self._running:
            try:
                self._run()
            except Exception as e:
                print(f"[YSF] USRP listener error: {e}", file=sys.stderr)
            if self._running:
                print("[YSF] Restarting USRP listener in 2 s…")
                time.sleep(2)

    def _run(self):
        listen_host  = self._cfg.get("usrp_listen_host", "0.0.0.0")
        listen_port  = int(self._cfg.get("usrp_listen_port", 34002))
        send_host    = self._cfg.get("usrp_send_host",   "127.0.0.1")
        send_port    = int(self._cfg.get("usrp_send_port",   34001))
        reflector    = self._cfg.get("reflector", "")
        label        = self._cfg.get("label", "YSF")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((listen_host, listen_port))
        sock.settimeout(0.5)
        print(f"[YSF] USRP listener on {listen_host}:{listen_port}  →  {send_host}:{send_port}")
        if reflector:
            print(f"[YSF] Reflector: {reflector}  {label}")

        last_reg    = 0.0
        last_packet = 0.0
        last_ptt    = USRP_PTT_OFF
        buf         = bytearray()
        timeout     = CHUNK_MS / 1000

        try:
            while self._running:
                now = time.time()

                # Send keepalive to analog_bridge every 20 s
                if now - last_reg > 20:
                    try:
                        sock.sendto(_build_usrp_keepalive(), (send_host, send_port))
                    except Exception:
                        pass
                    last_reg = now

                # Mark disconnected if no packets in 35 s
                if self._usrp_connected and (now - last_packet) > 35:
                    self._usrp_connected = False
                    print("[YSF] USRP connection lost (no packets)")

                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    # Tick detector so HOLD timer can expire
                    self._detect.feed(SILENCE)
                    continue
                except Exception as e:
                    print(f"[YSF] recv error: {e}", file=sys.stderr)
                    continue

                last_packet = time.time()
                if not self._usrp_connected:
                    self._usrp_connected = True
                    print(f"[YSF] USRP connected from {addr}")

                frame = _parse_usrp(data)
                if not frame:
                    continue

                ptt = frame["ptt"]

                # PTT rising edge → call start
                if ptt == USRP_PTT_ON and last_ptt != USRP_PTT_ON:
                    tg  = str(frame["tg"]) if frame["tg"] else None
                    src = str(frame["mpxid"]) if frame["mpxid"] else None
                    if tg:
                        self._current_tg  = tg
                    if src:
                        self._current_src = src
                    self._detect.ptt_on(
                        tg  = self._current_tg,
                        src = self._current_src,
                    )
                    if self._debug:
                        print(f"[YSF] PTT ON  tg={self._current_tg}  src={self._current_src}")

                # PTT falling edge → call end
                elif ptt == USRP_PTT_OFF and last_ptt != USRP_PTT_OFF:
                    self._detect.ptt_off()
                    if self._debug:
                        print(f"[YSF] PTT OFF")

                last_ptt = ptt

                # Audio payload (type=0, PTT on)
                if frame["type"] == USRP_TYPE_PCM and frame["payload"]:
                    buf.extend(frame["payload"])

                while len(buf) >= CHUNK_SIZE:
                    chunk = self._proc.process(bytes(buf[:CHUNK_SIZE]))
                    del buf[:CHUNK_SIZE]
                    self._detect.feed(chunk)
                    self._bcast.broadcast(chunk)
                    if self._feeder:
                        self._feeder.send(chunk)

        finally:
            sock.close()


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(title="YSF Decoder")

_cfg:    dict                    = {}
_dec:    Optional[YSFDecoder]    = None
_bcast:  Optional[AudioBroadcaster] = None
_detect: Optional[CallDetector]  = None
_feeder: Optional[IcecastFeeder] = None


@app.get("/status")
def status():
    ice = _cfg.get("broadcastify", {})
    tg, src = _dec.get_tg() if _dec else (None, None)
    return {
        "name":           _cfg.get("name", "YSF Decoder"),
        "reflector":      _cfg.get("reflector", ""),
        "label":          _cfg.get("label", ""),
        "active":         bool(_detect and _detect.active),
        "rx_count":       _detect.rx_count if _detect else 0,
        "talkgroup":      tg,
        "source":         src,
        "usrp_connected": _dec.usrp_connected if _dec else False,
        "audio_rate":     AUDIO_RATE,
        "icecast": {
            "enabled":    ice.get("enabled", False),
            "connected":  _feeder.connected if _feeder else False,
            "last_error": _feeder.last_error if _feeder else None,
        },
    }


@app.get("/stream")
async def stream():
    async def gen():
        cid, q = _bcast.add()
        try:
            yield _wav_stream_header()
            while True:
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    chunk = SILENCE
                yield chunk
        finally:
            _bcast.remove(cid)
    return StreamingResponse(gen(), media_type="audio/wav",
                             headers={"Cache-Control": "no-cache"})


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket):
    await ws.accept()
    cid, q = _bcast.add()
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                chunk = SILENCE
            await ws.send_bytes(chunk)
    except WebSocketDisconnect:
        pass
    finally:
        _bcast.remove(cid)


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    global _cfg, _dec, _bcast, _detect, _feeder

    parser = argparse.ArgumentParser(description="YSF reflector audio decoder")
    parser.add_argument("--config",       default=DEFAULT_CONFIG)
    parser.add_argument("--listen-port",  type=int, default=None)
    parser.add_argument("--debug",        action="store_true")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if cfg_path.exists():
        with cfg_path.open() as f:
            _cfg = json.load(f)
    else:
        print(f"[YSF] Config not found: {cfg_path} — using defaults", file=sys.stderr)
        _cfg = {}

    host = _cfg.get("host", "0.0.0.0")
    port = args.listen_port or _cfg.get("port", 8082)

    _bcast = AudioBroadcaster()

    uploader = None
    if "dispatcher" in _cfg:
        uploader = CallUploader(_cfg["dispatcher"])

    _detect = CallDetector(_cfg, uploader)

    _feeder = None
    ice_cfg = _cfg.get("broadcastify", {})
    if ice_cfg.get("enabled"):
        _feeder = IcecastFeeder(ice_cfg, label=_cfg.get("label", "YSF"))
        _feeder.start()

    _dec = YSFDecoder(_cfg, _bcast, _detect, _feeder, debug=args.debug)
    _detect._dec = _dec  # allow CallDetector to call _dec.get_tg() if needed

    async def serve():
        _bcast.set_loop(asyncio.get_running_loop())
        _dec.start()
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()
        _dec.stop()
        if _feeder:
            _feeder.stop()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
