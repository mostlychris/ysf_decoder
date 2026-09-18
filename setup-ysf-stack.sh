#!/usr/bin/env bash
# setup-ysf-stack.sh — configure the full YSF decode stack on this Pi.
#
# Creates:
#   /opt/Analog_Bridge/Analog_Bridge_YSF.ini
#   /opt/MMDVM_Bridge/MMDVM_Bridge_YSF.ini
#   /opt/MMDVM_Bridge/YSFClients/YSFGateway/YSFGateway.ini  (updated callsign/ID)
#   /opt/MMDVM_Bridge/ysf_decoder/ysf_decoder_config.json
#   /lib/systemd/system/ysf_gateway.service
#   /lib/systemd/system/mmdvm_bridge_ysf.service
#   /lib/systemd/system/analog_bridge_ysf.service
#   /lib/systemd/system/ysf-decoder.service
#
# Run once as root (or with sudo) after cloning ysf_decoder to
# /opt/MMDVM_Bridge/ysf_decoder and compiling YSFGateway.
set -euo pipefail

YSF_GW_DIR="/opt/MMDVM_Bridge/YSFClients/YSFGateway"
MMDVM_DIR="/opt/MMDVM_Bridge"
AB_DIR="/opt/Analog_Bridge"
YSF_DEC_DIR="/opt/MMDVM_Bridge/ysf_decoder"
PYTHON="$(command -v python3)"

# ── Verify prerequisites ───────────────────────────────────────────────────────
if [[ ! -x "$YSF_GW_DIR/YSFGateway" ]]; then
    echo "ERROR: YSFGateway binary not found at $YSF_GW_DIR/YSFGateway"
    echo "  Build it first: cd $YSF_GW_DIR && make"
    exit 1
fi

# ── Download YSF reflector list ────────────────────────────────────────────────
echo "[setup] Downloading YSFHosts.json..."
curl -fsSL "http://www.pistar.uk/downloads/YSF_Hosts.txt" -o "$YSF_GW_DIR/YSFHosts.txt" || true
if [[ -f "$YSF_GW_DIR/YSFHostsUpdate.sh" ]]; then
    bash "$YSF_GW_DIR/YSFHostsUpdate.sh" || true
fi

# ── Analog_Bridge_YSF.ini ──────────────────────────────────────────────────────
echo "[setup] Writing $AB_DIR/Analog_Bridge_YSF.ini..."
cat > "$AB_DIR/Analog_Bridge_YSF.ini" << 'EOF'
; Analog_Bridge configuration for YSF decode.
; Receives AMBE TLV from MMDVM_Bridge_YSF on port 35100, sends decoded USRP
; audio to ysf_decoder.py on port 34002.

[GENERAL]
logLevel = 3
exportMetadata = true
transferRootDir = /tmp
subscriberFile = /var/lib/dvswitch/subscriber_ids.csv
decoderFallBack = true
useEmulator = true
emulatorAddress = 127.0.0.1:2470
pcmPort = 2223

; AMBE TLV ↔ MMDVM_Bridge_YSF
; DVSwitch.ini [YSF] txPort=35100 / rxPort=35103
[AMBE_AUDIO]
address = 127.0.0.1
txPort = 35103                  ; Send TLV to MMDVM_Bridge_YSF
rxPort = 35100                  ; Receive TLV from MMDVM_Bridge_YSF
ambeMode = YSFN                 ; YSF Narrow (AMBE+2, same vocoder as DMR)
minTxTimeMS = 0

gatewayDmrId = 3223583
repeaterID = 322358311
txTg = 3223583
txTs = 2
colorCode = 1

; USRP ↔ ysf_decoder.py
; ysf_decoder usrp_listen_port=34002 / usrp_send_port=34001
[USRP]
address = 127.0.0.1
txPort = 34002                  ; Send USRP audio to ysf_decoder.py
rxPort = 34001                  ; Receive keepalives from ysf_decoder.py
usrpAudio = AUDIO_USE_AGC
usrpAGC = -20,10,100
tlvAudio = AUDIO_USE_AGC
tlvGain = 0.25

[MACROS]

[DV3000]
EOF

# ── MMDVM_Bridge_YSF.ini ──────────────────────────────────────────────────────
echo "[setup] Writing $MMDVM_DIR/MMDVM_Bridge_YSF.ini..."
cat > "$MMDVM_DIR/MMDVM_Bridge_YSF.ini" << 'EOF'
[General]
Callsign=N5LTC
Id=3223583
Timeout=180
Duplex=0

[Info]
RXFrequency=446650000
TXFrequency=446650000
Power=1
Latitude=41.7333
Longitude=-50.3999
Height=0
Location=Boerne, Texas
Description=MMDVM_Bridge_YSF
URL=https://groups.io/g/DVSwitch

[Log]
DisplayLevel=3
FileLevel=2
FilePath=/var/log/mmdvm
FileRoot=MMDVM_Bridge_YSF

[DMR Id Lookup]
File=/var/lib/mmdvm/DMRIds.dat
Time=24

[NXDN Id Lookup]
File=/var/lib/mmdvm/NXDN.csv
Time=24

[Modem]
Port=/dev/null
RSSIMappingFile=/dev/null
Trace=0
Debug=0

[System Fusion]
Enable=1

[System Fusion Network]
Enable=1
LocalAddress=127.0.0.1
LocalPort=3200
GatewayAddress=127.0.0.1
GatewayPort=4200
Debug=0

[DMR Network]
Enable=0

[P25 Network]
Enable=0

[NXDN Network]
Enable=0
EOF

# ── YSFGateway.ini — update callsign and DMR ID ───────────────────────────────
echo "[setup] Updating $YSF_GW_DIR/YSFGateway.ini callsign and ID..."
sed -i 's/^Callsign=.*/Callsign=N5LTC/'   "$YSF_GW_DIR/YSFGateway.ini"
sed -i 's/^Id=.*/Id=3223583/'             "$YSF_GW_DIR/YSFGateway.ini"
# Point to the compiled binary's directory for relative paths
sed -i 's|^Hosts=.*|Hosts=./YSFHosts.json|' "$YSF_GW_DIR/YSFGateway.ini" || true

echo ""
echo "[setup] YSFGateway.ini startup reflector is currently unset."
echo "  Edit $YSF_GW_DIR/YSFGateway.ini and set:"
echo "    Startup=<reflector name>   (e.g. America-Link)"
echo ""

# ── ysf_decoder_config.json ───────────────────────────────────────────────────
if [[ ! -f "$YSF_DEC_DIR/ysf_decoder_config.json" ]]; then
    echo "[setup] Writing $YSF_DEC_DIR/ysf_decoder_config.json..."
    cat > "$YSF_DEC_DIR/ysf_decoder_config.json" << 'EOF'
{
  "name":      "YSF Decoder",
  "host":      "0.0.0.0",
  "port":      8082,

  "reflector": "",
  "label":     "YSF",

  "usrp_listen_host": "0.0.0.0",
  "usrp_listen_port": 34002,
  "usrp_send_host":   "127.0.0.1",
  "usrp_send_port":   34001,

  "squelch_hold":    0.3,
  "min_call_length": 1.0,
  "audio_lp_hz":     3200,
  "audio_gain":      0.85,

  "dispatcher": {
    "url":             "http://127.0.0.1:9090",
    "api_key":         "",
    "system":          "YSF",
    "talkgroup":       0,
    "talkgroup_tag":   "YSF",
    "talkgroup_name":  "YSF Reflector",
    "talkgroup_group": "YSF"
  }
}
EOF
else
    echo "[setup] $YSF_DEC_DIR/ysf_decoder_config.json already exists — skipping."
fi

# ── Systemd: ysf_gateway.service ──────────────────────────────────────────────
echo "[setup] Writing systemd unit files..."

cat > /lib/systemd/system/ysf_gateway.service << EOF
[Unit]
Description=YSF Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$YSF_GW_DIR
ExecStart=$YSF_GW_DIR/YSFGateway $YSF_GW_DIR/YSFGateway.ini
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ysf_gateway

[Install]
WantedBy=multi-user.target
EOF

# ── Systemd: mmdvm_bridge_ysf.service ─────────────────────────────────────────
cat > /lib/systemd/system/mmdvm_bridge_ysf.service << EOF
[Unit]
Description=MMDVM Bridge (YSF)
After=network-online.target ysf_gateway.service
Wants=network-online.target
Requires=ysf_gateway.service

[Service]
Type=simple
User=root
WorkingDirectory=$MMDVM_DIR
Environment=DVSWITCH=$MMDVM_DIR/DVSwitch.ini
ExecStart=$MMDVM_DIR/MMDVM_Bridge $MMDVM_DIR/MMDVM_Bridge_YSF.ini
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=mmdvm_bridge_ysf

[Install]
WantedBy=multi-user.target
EOF

# ── Systemd: analog_bridge_ysf.service ────────────────────────────────────────
cat > /lib/systemd/system/analog_bridge_ysf.service << EOF
[Unit]
Description=Analog Bridge (YSF)
After=mmdvm_bridge_ysf.service
Requires=mmdvm_bridge_ysf.service

[Service]
Type=simple
User=root
WorkingDirectory=$AB_DIR
Environment=AnalogBridgeLogDir=/var/log/dvswitch
ExecStart=$AB_DIR/Analog_Bridge $AB_DIR/Analog_Bridge_YSF.ini
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=analog_bridge_ysf

[Install]
WantedBy=multi-user.target
EOF

# ── Systemd: ysf-decoder.service ──────────────────────────────────────────────
cat > /lib/systemd/system/ysf-decoder.service << EOF
[Unit]
Description=YSF Reflector Decoder
After=analog_bridge_ysf.service network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$YSF_DEC_DIR
ExecStart=$PYTHON $YSF_DEC_DIR/ysf_decoder.py
Restart=always
RestartSec=10
StartLimitIntervalSec=60
StartLimitBurst=3
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ysf-decoder

[Install]
WantedBy=multi-user.target
EOF

# ── Enable services ───────────────────────────────────────────────────────────
echo "[setup] Enabling services (not starting yet — set reflector first)..."
systemctl daemon-reload
systemctl enable ysf_gateway.service
systemctl enable mmdvm_bridge_ysf.service
systemctl enable analog_bridge_ysf.service
systemctl enable ysf-decoder.service

echo ""
echo "[setup] Done. Before starting, edit the startup reflector:"
echo "  $YSF_GW_DIR/YSFGateway.ini  →  set  Startup=<reflector name>"
echo ""
echo "  Browse reflectors:  grep -i 'america\|texas\|local' $YSF_GW_DIR/YSFHosts.txt"
echo ""
echo "  Then start the stack:"
echo "  sudo systemctl start ysf_gateway mmdvm_bridge_ysf analog_bridge_ysf ysf-decoder"
echo ""
echo "  Check logs:"
echo "  sudo journalctl -u ysf_gateway -u mmdvm_bridge_ysf -u analog_bridge_ysf -u ysf-decoder -f"
