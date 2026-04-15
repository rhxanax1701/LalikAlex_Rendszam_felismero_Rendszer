# 🚗 Mikroszámítógép-vezérelt Rendszámfelismerő Kapunyitó Rendszer

> Automatikus rendszámfelismerés Raspberry Pi + AI alapon, olcsó hardverrel.

![Python](https://img.shields.io/badge/Python-3.9+-blue)
![Flask](https://img.shields.io/badge/Flask-3.x-green)
![OpenCV](https://img.shields.io/badge/OpenCV-4.x-red)
![License](https://img.shields.io/badge/Licenc-MIT-yellow)

---

## 📁 Fájlstruktúra

```
gate-system/
│
├── server/                          # Szerveroldali kód (Linux PC / laptop)
│   ├── app.py                       # Flask alkalmazás — API, ALPR, auth
│   ├── plates.db                    # SQLite adatbázis (auto-létrejön)
│   │
│   ├── templates/
│   │   ├── index.html               # Főoldal — valós idejű műszerfal
│   │   ├── plates.html              # Rendszámkezelő oldal
│   │   └── login.html               # Bejelentkezési oldal
│   │
│   ├── static/
│   │   └── style.css                # Sötét témájú CSS
│   │
│   └── nginx_gate-web               # nginx konfiguráció (másolandó)
│
├── pi/                              # Raspberry Pi oldali kód
│   ├── pi_cam_sender.py             # Kamera + mozgásérzékelés + küldő
│   └── gate-cam.service             # systemd service fájl
│
├── docs/
│   └── diagram.png                  # Rendszer architektúra diagram
│
├── .gitignore
└── README.md
```

---

## ⚙️ Hogyan működik

```
┌─────────────────────┐        HTTP POST        ┌──────────────────────────┐
│   Raspberry Pi       │ ──── kép küldése ────► │   Szerver (Linux PC)     │
│                      │                         │                          │
│  EyeToy kamera       │ ◄─── heartbeat ──────── │   Flask + Gunicorn       │
│  MOG2 mozgásészlelés │                         │   ALPR (AI modell)       │
│  Állapotgép          │                         │   SQLite adatbázis       │
│  Discord webhook     │                         │   nginx + HTTPS          │
└─────────────────────┘                         └──────────────┬───────────┘
                                                               │
                                                    SSE (valós idő)
                                                               │
                                                 ┌─────────────▼───────────┐
                                                 │   Böngésző / Weboldal   │
                                                 │   admin / viewer fiók   │
                                                 └─────────────────────────┘
```

**Állapotgép (Pi):**
```
IDLE → (mozgás észlelve) → ACTIVE → (autó megállt) → PARKED → (1.5s várakozás) → KÉPKÜLDÉS → COOLDOWN → IDLE
```

---

## 🖥️ Szerver telepítése

### Követelmények
- Linux (Ubuntu 22.04+ ajánlott)
- Python 3.9+
- nginx

### 1. Klónozás és virtuális környezet

```bash
git clone https://github.com/rhxanax1701/LalikAlex_Rendszam_felismero_Rendszer.git
cd gate-system/server

python3 -m venv venv
source venv/bin/activate
```

### 2. Python csomagok telepítése

```bash
pip install flask flask-login gunicorn opencv-python-headless fast-alpr numpy
```

### 3. Titkos kulcs beállítása

Nyisd meg az `app.py` fájlt és cseréld le ezt a sort:

```python
app.secret_key = "CHANGE_THIS_TO_A_RANDOM_STRING_BEFORE_PRODUCTION"
```

Generálj egyet:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 4. nginx konfiguráció

```bash
sudo cp nginx_gate-web /etc/nginx/sites-available/gate-web
sudo ln -s /etc/nginx/sites-available/gate-web /etc/nginx/sites-enabled/gate-web
sudo rm /etc/nginx/sites-enabled/default   # opcionális

sudo nginx -t && sudo systemctl reload nginx
```

### 5. HTTPS tanúsítvány (önaláírt, LAN-ra)

```bash
sudo openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /etc/ssl/private/gate-web.key \
  -out /etc/ssl/certs/gate-web.crt \
  -subj "/CN=gateserver"
```

### 6. systemd service létrehozása

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

### 7. Első bejelentkezés

Nyisd meg a böngészőben: `https://SZERVER_IP`

| Felhasználó | Jelszó | Jogosultság |
|-------------|--------|-------------|
| `admin`     | `admin` | Teljes hozzáférés |
| `viewer`    | `viewer` | Csak megtekintés |

> ⚠️ **Változtasd meg a jelszavakat az első bejelentkezés után!** A rendszer figyelmeztet rá.

---

## 🍓 Raspberry Pi telepítése

### Követelmények
- Raspberry Pi Zero 2W (vagy bármely Pi)
- USB kamera (tesztelve: PS2 EyeToy)
- Raspberry Pi OS Lite (64-bit ajánlott)

### 1. Klónozás és virtuális környezet

```bash
git clone https://github.com/rhxanax1701/LalikAlex_Rendszam_felismero_Rendszer.git
cd gate-system/pi

python3 -m venv venv
source venv/bin/activate
```

### 2. Python csomagok telepítése

```bash
pip install opencv-python requests urllib3
```

### 3. v4l2 eszközök (kamera detektáláshoz)

```bash
sudo apt install v4l-utils -y
```

### 4. systemd service telepítése

```bash
sudo cp gate-cam.service /etc/systemd/system/gate-cam.service
sudo nano /etc/systemd/system/gate-cam.service
```

Módosítsd ezeket a sorokat:

```ini
Environment="SERVER_INGEST_URL=http://SZERVER_IP/ingest"
Environment="CAMERA_DEVICE=/dev/video0"

# Discord értesítéshez (opcionális):
# Environment="DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/..."
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable gate-cam
sudo systemctl start gate-cam
```

### 5. Ellenőrzés

```bash
journalctl -u gate-cam -f
```

Helyes működés esetén ezt kell látnod:

```
10:25:01 [INFO] gate-cam — gate-cam starting  ingest=http://192.168.x.x/ingest
10:25:02 [INFO] gate-cam — Opened /dev/video0 @ 320x240 15.0 fps
10:25:03 [INFO] gate-cam — Cooldown done — watching for next car
```

---

## 🔔 Discord debug beállítása (opcionális)

1. Discord → csatorna ⚙️ → **Integrations** → **Webhooks** → **New Webhook**
2. Kattints: **Copy URL**
3. Illeszd be a service fájlba:

```ini
Environment="DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/XXXXXXX/XXXXXXX"
```

Ezután minden parkoló autóról kapsz képet és ALPR eredményt a Discordon:

```
10:25:03  🚗 Park shot (ALPR target)
          [kép]
10:25:18  ✅ AA795PB → ALLOWED (votes: 2)
```

---

## 🌐 Tailscale VPN (fix IP helyett — ajánlott)

Ha nincs statikus IP a hálózaton:

```bash
# Szerveren ÉS a Pi-n:
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Szerveren:
tailscale status   # jegyezd fel a hostnamet, pl. "gateserver"
```

Ezután a Pi service fájlban:

```ini
Environment="SERVER_INGEST_URL=http://gateserver/ingest"
```

Többé nem kell IP-t változtatni.

---

## 🔧 Hangolható paraméterek

| Paraméter | Alapértelmezett | Leírás |
|-----------|----------------|--------|
| `PARK_WAIT_SEC` | `1.5` | Várakozás megállás után a képküldés előtt |
| `PARK_BURST_COUNT` | `3` | Hány képet küld parkoláskor |
| `COOLDOWN_SECONDS` | `12.0` | Várakozás következő autóig |
| `MOTION_MIN_AREA` | `500` | Minimális mozgási terület (pixelben) |
| `CONNECT_TIMEOUT` | `3.0` | Kapcsolódási timeout (másodperc) |
| `READ_TIMEOUT` | `25.0` | Olvasási timeout — ALPR feldolgozáshoz |
| `MIN_PLATE_CONF` | `0.80` | Minimum AI bizonyossági küszöb |
| `VOTES_REQUIRED` | `2` | Hány egyező olvasat kell a rögzítéshez |

---

## 🛠️ Hibakeresés

| Tünet | Ok | Megoldás |
|-------|----|----------|
| „Kamera hiba" a weboldalon | Szerver nem kapja a heartbeat-et | Ellenőrizd az IP-t a service fájlban |
| 504 Gateway Timeout | Gunicorn lassan dolgozza fel | Növeld a `--timeout` értéket |
| Rendszám nem ismerhető fel | Kép elmosódott vagy rossz szög | Növeld a `PARK_WAIT_SEC` értéket |
| Kamera nem nyílik meg | Rossz device path | Futtasd: `ls /dev/video*` |
| Import hiba indításkor | Hiányzó Python csomag | Futtasd újra a `pip install` parancsot |

---

---

*Infoprog projekt — 2026*
