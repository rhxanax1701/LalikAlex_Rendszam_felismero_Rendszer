#!/usr/bin/env python3
"""
pi_cam_sender.py — Gate Camera Motion Sender
Raspberry Pi Zero 2W → Gate Server (slow 2013 laptop)

HOW IT WORKS (state machine):
  IDLE     → motion detected for N frames              → ACTIVE
  ACTIVE   → motion stops (car parked)                 → PARKED
  ACTIVE   → still moving after ACTIVE_TIMEOUT_SEC     → fallback burst → COOLDOWN
  PARKED   → wait PARK_WAIT_SEC for car to settle      → park burst → COOLDOWN
  COOLDOWN → wait before looking for next car          → IDLE

DISCORD DEBUG:
  Set DISCORD_WEBHOOK_URL in the environment to receive preview images
  and ALPR results in a Discord channel. Zero load on the gate server.

Threads:
  Main      : camera read + state machine
  Sender    : HTTP POST queue to server (handles slow ALPR independently)
  Discord   : sends debug images to Discord webhook (optional)
  Heartbeat : keepalive ping every 5s (fire-and-forget)
"""

import os
import time
import glob
import subprocess
import threading
import logging
from queue import Queue, Full, Empty

import cv2
import numpy as np
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Config ────────────────────────────────────────────────────────────────────
SERVER_INGEST_URL = os.getenv("SERVER_INGEST_URL", "http://172.16.0.132/ingest")
SERVER_HEARTBEAT_URL = os.getenv(
    "SERVER_HEARTBEAT_URL",
    SERVER_INGEST_URL.replace("/ingest", "/heartbeat"),
)

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

CAMERA_DEVICE  = os.getenv("CAMERA_DEVICE", "").strip()
CAPTURE_WIDTH  = int(os.getenv("CAPTURE_WIDTH",  "320"))
CAPTURE_HEIGHT = int(os.getenv("CAPTURE_HEIGHT", "240"))
CAPTURE_FPS    = int(os.getenv("CAPTURE_FPS",    "15"))

MOTION_SCALE_W         = int(os.getenv("MOTION_SCALE_W",         "160"))
MOTION_SCALE_H         = int(os.getenv("MOTION_SCALE_H",         "120"))
MOTION_THRESHOLD       = int(os.getenv("MOTION_THRESHOLD",       "20"))
MOTION_MIN_AREA        = int(os.getenv("MOTION_MIN_AREA",        "500"))
MOTION_FRAMES_REQUIRED = int(os.getenv("MOTION_FRAMES_REQUIRED", "3"))
MOTION_GONE_FRAMES     = int(os.getenv("MOTION_GONE_FRAMES",     "8"))

PARK_WAIT_SEC       = float(os.getenv("PARK_WAIT_SEC",       "1.5"))
PARK_BURST_COUNT    = int(os.getenv("PARK_BURST_COUNT",      "3"))
PARK_BURST_INTERVAL = float(os.getenv("PARK_BURST_INTERVAL", "0.5"))
PARK_JPEG_QUALITY   = int(os.getenv("PARK_JPEG_QUALITY",     "88"))

ACTIVE_TIMEOUT_SEC = float(os.getenv("ACTIVE_TIMEOUT_SEC", "8.0"))
ACTIVE_BURST_COUNT = int(os.getenv("ACTIVE_BURST_COUNT",   "2"))

COOLDOWN_SECONDS = float(os.getenv("COOLDOWN_SECONDS", "12.0"))

RECOVERY_BURST_COUNT    = int(os.getenv("RECOVERY_BURST_COUNT",    "3"))
RECOVERY_BURST_INTERVAL = float(os.getenv("RECOVERY_BURST_INTERVAL", "0.5"))
RECOVERY_JPEG_QUALITY   = int(os.getenv("RECOVERY_JPEG_QUALITY",   "88"))

SEND_QUEUE_MAX       = int(os.getenv("SEND_QUEUE_MAX",       "6"))

# KEY FIX: split connect timeout (fast failure) from read timeout (slow ALPR)
# connect=3  → fail fast if server unreachable, don't block sender for 20s
# read=25    → give the slow server time to finish ALPR
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "3.0"))
READ_TIMEOUT    = float(os.getenv("READ_TIMEOUT",    "25.0"))

RECONNECT_BASE_DELAY = float(os.getenv("RECONNECT_BASE_DELAY", "1.0"))
RECONNECT_MAX_DELAY  = float(os.getenv("RECONNECT_MAX_DELAY",  "20.0"))

HEARTBEAT_INTERVAL = float(os.getenv("HEARTBEAT_INTERVAL", "5.0"))
REBOOT_AFTER_SEC   = float(os.getenv("REBOOT_AFTER_SEC",   "60.0"))

# ── States ────────────────────────────────────────────────────────────────────
IDLE     = "idle"
ACTIVE   = "active"
PARKED   = "parked"
COOLDOWN = "cooldown"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gate-cam")

# ── HTTP session (image POSTs to server, 1 retry on 5xx only) ────────────────
def _make_session():
    s = requests.Session()
    # Only retry on server errors (5xx), NOT on timeouts/connect errors
    # Retrying connect errors just doubles the wait time uselessly
    retry = Retry(
        total=1,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["POST"],
        raise_on_status=False,
        # Don't retry on connection/read errors — fail fast instead
        connect=0,
        read=0,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://",  adapter)
    s.mount("https://", adapter)
    s.verify = False
    return s

_session = _make_session()

# ── Camera helpers ────────────────────────────────────────────────────────────
def _is_capture_device(dev):
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", "--device", dev, "--info"],
            stderr=subprocess.DEVNULL, timeout=2,
        ).decode(errors="ignore")
        return "Video Capture" in out
    except Exception:
        return False


def _list_capture_devices():
    devs    = sorted(glob.glob("/dev/video*"))
    capture = [d for d in devs if _is_capture_device(d)]
    log.info("Capture devices: %s", capture or "none")
    return capture


def open_camera():
    candidates = []
    if CAMERA_DEVICE:
        candidates.append(CAMERA_DEVICE)
    for d in _list_capture_devices():
        if d not in candidates:
            candidates.append(d)

    for dev in candidates:
        log.info("Trying %s …", dev)
        cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            continue
        ret, frame = cap.read()
        if not ret or frame is None:
            cap.release()
            continue

        for prop, val in [
            (cv2.CAP_PROP_BUFFERSIZE,   1),
            (cv2.CAP_PROP_FOURCC,       cv2.VideoWriter_fourcc(*"MJPG")),
            (cv2.CAP_PROP_FRAME_WIDTH,  CAPTURE_WIDTH),
            (cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_HEIGHT),
            (cv2.CAP_PROP_FPS,          CAPTURE_FPS),
        ]:
            try:
                cap.set(prop, val)
            except Exception:
                pass

        w   = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h   = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        log.info("Opened %s @ %dx%d %.1f fps", dev, w, h, fps)
        return cap, dev

    return None, None


def flush_buffer(cap, count=3):
    for _ in range(count):
        cap.grab()


def encode_jpeg(frame, quality=85):
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return None
    jpg = buf.tobytes()
    if not (jpg[:2] == b"\xff\xd8" and jpg[-2:] == b"\xff\xd9"):
        return None
    return jpg

# ── Network: server ───────────────────────────────────────────────────────────
def post_image(jpg):
    files = {"image": ("frame.jpg", jpg, "image/jpeg")}
    # Tuple timeout: (connect_timeout, read_timeout)
    # connect=3s → fail fast if server IP changed or is down
    # read=25s   → give ALPR on old CPU time to finish
    r = _session.post(
        SERVER_INGEST_URL,
        files=files,
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}


def send_heartbeat():
    try:
        requests.post(
            SERVER_HEARTBEAT_URL,
            timeout=(2.0, 3.0),        # fast connect + fast read for heartbeat
            verify=False,
            allow_redirects=False,
        )
        log.debug("Heartbeat OK")
    except Exception:
        pass

# ── Network: Discord ──────────────────────────────────────────────────────────
_discord_queue = Queue(maxsize=10)


def _discord_loop():
    if not DISCORD_WEBHOOK_URL:
        return

    log.info("Discord debug enabled")

    while True:
        try:
            jpg, caption = _discord_queue.get(timeout=1.0)
        except Empty:
            continue

        try:
            ts      = time.strftime("%H:%M:%S")
            payload = {"content": f"`{ts}` {caption}"}
            files   = {"file": ("gate_cam.jpg", jpg, "image/jpeg")}
            r = requests.post(
                DISCORD_WEBHOOK_URL,
                data=payload,
                files=files,
                timeout=8.0,
            )
            if r.status_code in (200, 204):
                log.debug("Discord: sent %s", caption)
            else:
                log.debug("Discord: HTTP %d", r.status_code)
        except Exception as exc:
            log.debug("Discord send failed: %s", exc)
        finally:
            _discord_queue.task_done()


def discord_send(jpg, caption):
    if not DISCORD_WEBHOOK_URL or jpg is None:
        return
    try:
        _discord_queue.put_nowait((jpg, caption))
    except Full:
        pass

# ── Reboot ────────────────────────────────────────────────────────────────────
def do_reboot():
    log.warning("Camera gone too long — rebooting!")
    time.sleep(1)
    for cmd in ["sudo /sbin/reboot", "sudo /usr/sbin/reboot",
                "systemctl reboot", "/sbin/reboot"]:
        if os.system(cmd) == 0:
            break
    os._exit(1)

# ── Heartbeat thread ──────────────────────────────────────────────────────────
def _heartbeat_loop():
    while True:
        send_heartbeat()
        time.sleep(HEARTBEAT_INTERVAL)

# ── Sender thread ─────────────────────────────────────────────────────────────
send_queue = Queue(maxsize=SEND_QUEUE_MAX)


def _sender_loop():
    while True:
        try:
            jpg, label = send_queue.get(timeout=1.0)
        except Empty:
            continue
        try:
            code, data = post_image(jpg)
            if 200 <= code < 300:
                committed = data.get("committed", False)
                plate     = data.get("winner") or data.get("plate") or "—"
                votes     = data.get("votes", "?")
                status    = data.get("status", "")
                log.info("[%s] HTTP %d  committed=%s  plate=%s  votes=%s  %s",
                         label, code, committed, plate, votes, status)

                if DISCORD_WEBHOOK_URL and committed:
                    emoji = "✅" if status == "ALLOWED" else "❌"
                    try:
                        requests.post(
                            DISCORD_WEBHOOK_URL,
                            data={"content": f"{emoji} `{plate}` → **{status}** (votes: {votes})"},
                            timeout=5.0,
                        )
                    except Exception:
                        pass
            else:
                log.warning("[%s] Server returned HTTP %d", label, code)
        except Exception as exc:
            log.warning("[%s] Send error: %s", label, exc)
        finally:
            send_queue.task_done()


def enqueue(jpg, label):
    try:
        send_queue.put_nowait((jpg, label))
    except Full:
        log.debug("Queue full — dropping %s", label)


def shoot_burst(cap, count, interval, label_prefix, quality,
                send_to_discord=False, discord_label=""):
    """
    Capture `count` frames, enqueue to server.
    Only the first frame goes to Discord (they all look the same).
    """
    frame        = None
    discord_sent = False

    for i in range(count):
        ret, f = cap.read()
        if ret and f is not None:
            frame = f
        if frame is None:
            time.sleep(interval)
            continue

        jpg = encode_jpeg(frame, quality)
        if jpg:
            enqueue(jpg, f"{label_prefix}-{i + 1}/{count}")

            if send_to_discord and not discord_sent and DISCORD_WEBHOOK_URL:
                discord_send(jpg, discord_label or label_prefix)
                discord_sent = True

        if i < count - 1:
            time.sleep(interval)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("gate-cam starting  ingest=%s", SERVER_INGEST_URL)
    log.info(
        "connect_timeout=%.1fs  read_timeout=%.1fs  "
        "park_wait=%.1fs  park_burst=%d  cooldown=%.1fs",
        CONNECT_TIMEOUT, READ_TIMEOUT,
        PARK_WAIT_SEC, PARK_BURST_COUNT, COOLDOWN_SECONDS,
    )
    if DISCORD_WEBHOOK_URL:
        log.info("Discord debug: ENABLED")
    else:
        log.info("Discord debug: disabled")

    threading.Thread(target=_heartbeat_loop, name="heartbeat", daemon=True).start()
    threading.Thread(target=_sender_loop,    name="sender",    daemon=True).start()
    threading.Thread(target=_discord_loop,   name="discord",   daemon=True).start()

    cap                  = None
    reconnect_delay      = RECONNECT_BASE_DELAY
    camera_missing_since = None
    just_recovered       = False

    state            = IDLE
    state_since      = time.time()
    motion_streak    = 0
    no_motion_streak = 0

    def fresh_fgbg():
        return cv2.createBackgroundSubtractorMOG2(
            history=150,
            varThreshold=float(MOTION_THRESHOLD),
            detectShadows=False,
        )

    fgbg = fresh_fgbg()

    while True:

        # ── (Re)open camera ───────────────────────────────────────────────
        if cap is None or not cap.isOpened():
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
                cap = None

            if camera_missing_since is None:
                camera_missing_since = time.time()
            elif (time.time() - camera_missing_since) >= REBOOT_AFTER_SEC:
                do_reboot()

            log.info("Scanning for camera (retry in %.1f s) …", reconnect_delay)
            cap, dev_label = open_camera()

            if cap is None:
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX_DELAY)
                continue

            was_missing          = camera_missing_since is not None
            camera_missing_since = None
            reconnect_delay      = RECONNECT_BASE_DELAY
            motion_streak        = 0
            no_motion_streak     = 0
            state                = IDLE
            fgbg                 = fresh_fgbg()

            if was_missing:
                just_recovered = True
                log.info("Camera recovered on %s", dev_label)

        # ── Read frame ────────────────────────────────────────────────────
        ret, frame = cap.read()
        if not ret or frame is None:
            log.warning("Frame read failed — will reconnect")
            try:
                cap.release()
            except Exception:
                pass
            cap = None
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX_DELAY)
            continue

        reconnect_delay = RECONNECT_BASE_DELAY
        now = time.time()

        # ── Recovery burst ────────────────────────────────────────────────
        if just_recovered:
            just_recovered = False
            log.info("Recovery burst — checking for parked car …")
            flush_buffer(cap)
            shoot_burst(
                cap, RECOVERY_BURST_COUNT, RECOVERY_BURST_INTERVAL,
                "recovery", RECOVERY_JPEG_QUALITY,
                send_to_discord=True,
                discord_label="🔄 Recovery check (camera reconnected)",
            )
            fgbg        = fresh_fgbg()
            state       = COOLDOWN
            state_since = now
            continue

        # ── Motion detection ──────────────────────────────────────────────
        small   = cv2.resize(frame, (MOTION_SCALE_W, MOTION_SCALE_H))
        gray    = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray    = cv2.GaussianBlur(gray, (5, 5), 0)
        fg_mask = fgbg.apply(gray)

        kernel  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, kernel)
        fg_mask = cv2.dilate(fg_mask, kernel, iterations=2)

        contours, _ = cv2.findContours(
            fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        motion = any(cv2.contourArea(c) >= MOTION_MIN_AREA for c in contours)

        # ── State machine ─────────────────────────────────────────────────
        if state == IDLE:
            if motion:
                motion_streak += 1
            else:
                motion_streak = max(0, motion_streak - 1)

            if motion_streak >= MOTION_FRAMES_REQUIRED:
                log.info("Motion confirmed — car approaching")
                state            = ACTIVE
                state_since      = now
                motion_streak    = 0
                no_motion_streak = 0

        elif state == ACTIVE:
            if motion:
                no_motion_streak = 0
            else:
                no_motion_streak += 1

            time_in_active = now - state_since

            if no_motion_streak >= MOTION_GONE_FRAMES:
                log.info(
                    "Car stopped after %.1fs — waiting %.1fs to settle",
                    time_in_active, PARK_WAIT_SEC,
                )
                state            = PARKED
                state_since      = now
                no_motion_streak = 0

            elif time_in_active >= ACTIVE_TIMEOUT_SEC:
                log.info("Car still moving after %.1fs — fallback burst", time_in_active)
                flush_buffer(cap, count=2)
                shoot_burst(
                    cap, ACTIVE_BURST_COUNT, 0.4,
                    "fallback", 82,
                    send_to_discord=True,
                    discord_label="⚠️ Fallback shot (car still moving)",
                )
                state       = COOLDOWN
                state_since = now

        elif state == PARKED:
            if (now - state_since) >= PARK_WAIT_SEC:
                log.info("Shooting parked car — %d frames", PARK_BURST_COUNT)
                flush_buffer(cap, count=2)
                shoot_burst(
                    cap, PARK_BURST_COUNT, PARK_BURST_INTERVAL,
                    "park", PARK_JPEG_QUALITY,
                    send_to_discord=True,
                    discord_label="🚗 Park shot (ALPR target)",
                )
                state       = COOLDOWN
                state_since = now

        elif state == COOLDOWN:
            if (now - state_since) >= COOLDOWN_SECONDS:
                log.info("Cooldown done — watching for next car")
                state         = IDLE
                motion_streak = 0
                fgbg          = fresh_fgbg()


if __name__ == "__main__":
    main()
