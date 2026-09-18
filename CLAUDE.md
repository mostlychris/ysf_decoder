# CLAUDE.md — ysf_decoder

Standalone Python app that receives YSF (Yaesu System Fusion) audio from an
`analog_bridge` instance (YSF mode) via the USRP UDP protocol, detects
transmissions via PTT signaling, and uploads completed calls to the dispatcher
app (`mostlychris/dispatcher`) via `POST /api/call-upload`.

## Pipeline

```
YSF Reflector (Internet, UDP port 42000)
    ↕ YSFGateway (G4KLX YSFClients)
    ↕ analog_bridge (second instance, YSF mode, ports 34001/34002)
    ↓ USRP UDP audio
    ysf_decoder.py
    ↓ POST /api/call-upload
    dispatcher (mostlychris/dispatcher)
```

## Running

```bash
python3 ysf_decoder.py                          # default port 8082
python3 ysf_decoder.py --config /path/to/cfg.json
python3 ysf_decoder.py --listen-port 8083
python3 ysf_decoder.py --debug                  # prints PTT on/off events
```

## Dependencies

```bash
pip install fastapi "uvicorn[standard]" numpy scipy
# YSFGateway and analog_bridge must be installed and configured separately
```

## Configuration

Copy `ysf_decoder_config.example.json` → `ysf_decoder_config.json` and edit.

| Key | Description |
|-----|-------------|
| `reflector` | YSF reflector name (display only), e.g. `"REF001"` |
| `label` | Human-readable label, e.g. `"America-Link"` |
| `usrp_listen_port` | UDP port to receive audio from analog_bridge (default `34002`) |
| `usrp_send_port` | UDP port analog_bridge listens on for keepalives (default `34001`) |
| `usrp_send_host` | Host running analog_bridge (default `"127.0.0.1"`) |
| `squelch_hold` | Seconds to hold call open after PTT drops; default `0.3` |
| `min_call_length` | Minimum call duration in seconds to upload; default `1.0` |
| `audio_lp_hz` | LP filter cutoff; default `3200` (Nyquist-safe for 8 kHz audio) |
| `audio_gain` | Output gain multiplier; default `0.85` |

## How it differs from dmr_decoder

| | dmr_decoder | ysf_decoder |
|---|---|---|
| Input | subprocess stdout (rtl_fm → dsd-fme) | USRP UDP socket |
| Audio rate | 16 000 Hz | 8 000 Hz |
| Call detection | RMS squelch (no explicit PTT) | USRP PTT field (explicit) |
| RTL-SDR required | Yes | No (network audio) |

## USRP protocol

analog_bridge sends 32-byte USRP headers + 320 bytes of 8 kHz 16-bit PCM per packet (20 ms):

```
Bytes 0-3:   'USRP'
Bytes 4-7:   Sequence number (uint32 big-endian)
Bytes 8-11:  Talkgroup (uint32 big-endian)
Bytes 12-15: PTT — 0=off, 1=on (uint32 big-endian)
Bytes 16-19: Type — 0=PCM audio, 1=metadata
Bytes 20-23: MPXID / source ID
Bytes 24-31: Reserved
Bytes 32+:   Audio payload (320 bytes = 160 samples)
```

ysf_decoder sends keepalive USRP frames to analog_bridge every 20 seconds to
maintain the USRP connection.

## API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /status` | JSON: name, reflector, label, active, rx_count, talkgroup, source, usrp_connected |
| `GET /stream` | Live WAV stream (infinite-length header), 8 kHz mono 16-bit |
| `WS /ws/audio` | Raw 16-bit PCM WebSocket at 8 kHz |

## Dispatcher integration

The dispatcher (`mostlychris/dispatcher`) exposes:
- `GET /api/ysf/status` — proxies to `/status` above
- `GET /api/ysf/stream` — proxies to `/stream` above

Set `YSF_DECODER_URL` in dispatcher's `config.py`:
```python
YSF_DECODER_URL = 'http://127.0.0.1:8082'
```

## Systemd service

```bash
bash install-service.sh
```

```bash
sudo systemctl status ysf-decoder
sudo journalctl -u ysf-decoder -f
sudo systemctl restart ysf-decoder
```

## analog_bridge YSF configuration

Run a second `analog_bridge` instance dedicated to YSF, separate from the
DMR instance. Key settings in its `.ini` file:

```ini
[USRP]
txPort = 34001     # ysf_decoder sends keepalives here
rxPort = 34002     # ysf_decoder listens here

[AMBE_AUDIO]
# YSF-specific AMBE settings
```

Use a different service name (e.g. `analog_bridge_ysf.service`) so the
watchdog can monitor it independently from the DMR `analog_bridge.service`.

## Repository

GitHub: https://github.com/mostlychris/ysf_decoder
Companion dispatcher: https://github.com/mostlychris/dispatcher
