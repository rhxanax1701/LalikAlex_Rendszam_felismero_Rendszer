# 🚗 Microcomputer-Controlled License Plate Recognition Gate System

> Automatic license plate recognition based on Raspberry Pi + AI, using cheap hardware.

![Python](https://img.shields.io/badge/Python-3.9+-blue)
![Flask](https://img.shields.io/badge/Flask-3.x-green)
![OpenCV](https://img.shields.io/badge/OpenCV-4.x-red)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## 📁 File Structure

```
gate-system/
│
├── server/                          # Server-side code (Linux PC / laptop)
│   ├── app.py                       # Flask application — API, ALPR, auth
│   ├── plates.db                    # SQLite database (auto-created)
│   │
│   ├── templates/
│   │   ├── index.html               # Home page — real-time dashboard
│   │   ├── plates.html              # License plate management page
│   │   └── login.html               # Login page
│   │
│   ├── static/
│   │   └── style.css                # Dark theme CSS
│   │
│   └── nginx_gate-web               # nginx configuration (to be copied)
│
├── pi/                              # Raspberry Pi side code
│   ├── pi_cam_sender.py             # Camera + motion detection + sender
│   └── gate-cam.service             # systemd service file
│
├── docs/
│   └── diagram.png                  # System architecture diagram
│
├── .gitignore
└── README.md
```

---

## ⚙️ How It Works

```
┌─────────────────────┐        HTTP POST        ┌──────────────────────────┐
│   Raspberry Pi       │ ──── send image ─────► │   Server (Linux PC)      │
│                      │                         │                          │
│  EyeToy camera       │ ◄─── heartbeat ──────── │   Flask + Gunicorn       │
│  MOG2 motion detect  │                         │   ALPR (AI model)        │
│  State machine       │                         │   SQLite database        │
│  Discord webhook     │                         │   nginx + HTTPS          │
└─────────────────────┘                         └──────────────┬───────────┘
                                                               │
                                                    SSE (real-time)
                                                               │
                                                 ┌─────────────▼───────────┐
                                                 │   Browser / Website     │
                                                 │   admin / viewer account│
                                                 └─────────────────────────┘
```

**State Machine (Pi):**
```
IDLE → (motion detected) → ACTIVE → (car stopped) → PARKED → (1.5s wait) → SEND IMAGE → COOLDOWN → IDLE
```

---

## 🖥️ Server Setup

### Requirements
- Linux (Ubuntu 22.04+ recommended)
- Python 3.9+
- nginx

### 1. Clone and create virtual environment

```bash
git clone https://github.com/rhxanax1701/LalikAlex_Rendszam_felismero_Rendszer.git
cd gate-system/server

python3 -m venv venv
source venv/bin/activate
```

### 2. Install Python packages

```bash
pip install flask flask-login gunicorn opencv-python-headless fast-alpr numpy
```

### 3. Set the secret key

Open the `app.py` file and replace this line:

```python
app.secret_key = "CHANGE_THIS_TO_A_RANDOM_STRING_BEFORE_PRODUCTION"
```

Generate one:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 4. nginx configuration

```bash
sudo cp nginx_gate-web /etc/nginx/sites-available/gate-web
sudo ln -s /etc/nginx/sites-available/gate-web /etc/nginx/sites-enabled/gate-web
sudo rm /etc/nginx/sites-enabled/default   # optional

sudo nginx -t && sudo systemctl reload nginx
```

### 5. HTTPS certificate (self-signed, for LAN)

```bash
sudo openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /etc/ssl/private/gate-web.key \
  -out /etc/ssl/certs/gate-web.crt \
  -subj "/CN=gateserver"
```

### 6. Create systemd service

```bash
sudo nano /etc/systemd/system/gate-web.service
```

```ini
[Unit]
Description=Gate Web (Gunicorn + Flask)
After=network.target

[Service]
User=thedoctor
WorkingDirectory=/home/thedoctor/gate-system/server
ExecStart=/home/thedoctor/gate-system/server/venv/bin/gunicorn \
    --workers 2 \
    --threads 4 \
    --timeout 120 \
    --bind 127.0.0.1:8000 \
    app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable gate-web
sudo systemctl start gate-web
```

### 7. First login

Open in browser: `https://SERVER_IP`

| User    | Password | Permission       |
|---------|----------|-------------------|
| `admin`  | `admin`  | Full access       |
| `viewer` | `viewer` | View-only access  |

> ⚠️ **Change the passwords after the first login!** The system will prompt you to do so.

---

## 🍓 Raspberry Pi Setup

### Requirements
- Raspberry Pi Zero 2W (or any Pi)
- USB camera (tested with: PS2 EyeToy)
- Raspberry Pi OS Lite (64-bit recommended)

### 1. Clone and create virtual environment

```bash
git clone https://github.com/rhxanax1701/LalikAlex_Rendszam_felismero_Rendszer.git
cd gate-system/pi

python3 -m venv venv
source venv/bin/activate
```

### 2. Install Python packages

```bash
pip install opencv-python requests urllib3
```

### 3. v4l2 utilities (for camera detection)

```bash
sudo apt install v4l-utils -y
```

### 4. Install systemd service

```bash
sudo cp gate-cam.service /etc/systemd/system/gate-cam.service
sudo nano /etc/systemd/system/gate-cam.service
```

Modify these lines:

```ini
Environment="SERVER_INGEST_URL=http://SERVER_IP/ingest"
Environment="CAMERA_DEVICE=/dev/video0"

# For Discord notifications (optional):
# Environment="DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/..."
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable gate-cam
sudo systemctl start gate-cam
```

### 5. Verification

```bash
journalctl -u gate-cam -f
```

If working correctly, you should see:

```
10:25:01 [INFO] gate-cam — gate-cam starting  ingest=http://192.168.x.x/ingest
10:25:02 [INFO] gate-cam — Opened /dev/video0 @ 320x240 15.0 fps
10:25:03 [INFO] gate-cam — Cooldown done — watching for next car
```

---

## 🔔 Discord Debug Setup (optional)

1. Discord → channel ⚙️ → **Integrations** → **Webhooks** → **New Webhook**
2. Click: **Copy URL**
3. Paste it into the service file:

```ini
Environment="DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/XXXXXXX/XXXXXXX"
```

After this, you'll receive an image and ALPR result on Discord for every parked car:

```
10:25:03  🚗 Park shot (ALPR target)
          [image]
10:25:18  ✅ AA795PB → ALLOWED (votes: 2)
```

---

## 🌐 Tailscale VPN (recommended instead of a fixed IP)

If you don't have a static IP on your network:

```bash
# On BOTH the server AND the Pi:
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# On the server:
tailscale status   # note the hostname, e.g. "gateserver"
```

Then in the Pi's service file:

```ini
Environment="SERVER_INGEST_URL=http://gateserver/ingest"
```

You'll never need to change the IP again.

---

## 🔧 Tunable Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `PARK_WAIT_SEC` | `1.5` | Wait time after stopping before sending the image |
| `PARK_BURST_COUNT` | `3` | Number of images sent when parking |
| `COOLDOWN_SECONDS` | `12.0` | Wait time until the next car |
| `MOTION_MIN_AREA` | `500` | Minimum motion area (in pixels) |
| `CONNECT_TIMEOUT` | `3.0` | Connection timeout (seconds) |
| `READ_TIMEOUT` | `25.0` | Read timeout — for ALPR processing |
| `MIN_PLATE_CONF` | `0.80` | Minimum AI confidence threshold |
| `VOTES_REQUIRED` | `2` | Number of matching readings required to record |

---

## 🛠️ Troubleshooting

| Symptom | Cause | Solution |
|---------|-------|----------|
| "Camera error" on the website | Server isn't receiving the heartbeat | Check the IP in the service file |
| 504 Gateway Timeout | Gunicorn processing too slowly | Increase the `--timeout` value |
| License plate not recognized | Image blurry or bad angle | Increase the `PARK_WAIT_SEC` value |
| Camera won't open | Wrong device path | Run: `ls /dev/video*` |
| Import error on startup | Missing Python package | Re-run the `pip install` command |

---

---

