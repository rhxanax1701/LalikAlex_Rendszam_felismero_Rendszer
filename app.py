"""
app.py — Gate Web Server
Runs on gateserver behind nginx.

Auth:  Flask-Login with two roles
  admin  → full access (add/delete plates, change password)
  viewer → read-only (see dashboard and plate list)

Default accounts (CHANGE ON FIRST RUN!):
  admin / admin
  viewer / viewer
"""

from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, abort, flash, Response,
)
from flask_login import (
    LoginManager, UserMixin,
    login_user, logout_user, login_required, current_user,
)
from werkzeug.security import generate_password_hash, check_password_hash

import sqlite3
import statistics
import re
import time
import json
from collections import defaultdict, deque
from functools import wraps

import cv2
import numpy as np
from fast_alpr import ALPR

# ── App setup ─────────────────────────────────────────────────────────────────
DB_FILE = "plates.db"
app     = Flask(__name__)

# IMPORTANT: change this before running in production!
app.secret_key = "CHANGE_THIS_TO_A_RANDOM_STRING_BEFORE_PRODUCTION"

# ── ALPR (runs on CPU, shared instance) ──────────────────────────────────────
alpr = ALPR(
    detector_model="yolo-v9-t-384-license-plate-end2end",
    ocr_model="cct-xs-v1-global-model",
    ocr_device="cpu",
)

# ── Ingest / voting config ────────────────────────────────────────────────────
ALLOWED_INGEST_IPS  = None   # None = trust nginx ACL; set to a set() to also check here
MIN_PLATE_CONF      = 0.80
VOTE_WINDOW_SEC     = 2.5
VOTES_REQUIRED      = 2
CAMERA_TIMEOUT_SEC  = 12

# In-memory state (resets on restart, that's fine)
recent_by_source: dict = defaultdict(lambda: deque(maxlen=30))
last_ingest_time: dict = {}   # src_ip -> float (epoch)
camera_state: dict     = {}   # src_ip -> "ok" | "recovered" | "error"

# ── Flask-Login ───────────────────────────────────────────────────────────────
login_manager = LoginManager(app)
login_manager.login_view         = "login"
login_manager.login_message      = "Kérlek jelentkezz be."
login_manager.login_message_category = "info"


class User(UserMixin):
    def __init__(self, uid: int, username: str, role: str):
        self.id       = str(uid)
        self.username = username
        self.role     = role   # "admin" | "viewer"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@login_manager.user_loader
def load_user(uid: str):
    conn = _db()
    row  = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    if row:
        return User(row["id"], row["username"], row["role"])
    return None


def admin_required(f):
    """Decorator: login required + must be admin role."""
    @wraps(f)
    @login_required
    def inner(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return inner

# ── Database ──────────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _db()
    c    = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS allowed_plates (
            plate TEXT PRIMARY KEY
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS last_seen (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            plate      TEXT,
            confidence REAL,
            source_ip  TEXT,
            timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role     TEXT NOT NULL DEFAULT 'viewer',
            pw_changed INTEGER NOT NULL DEFAULT 0
        )
    """)

    # Seed default accounts only when the table is empty
    if not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        c.execute(
            "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
            ("admin",  generate_password_hash("admin"),  "admin"),
        )
        c.execute(
            "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
            ("viewer", generate_password_hash("viewer"), "viewer"),
        )
        print("[app] Default users created: admin/admin  viewer/viewer  ← CHANGE PASSWORDS!")

    conn.commit()
    conn.close()

# ── DB helpers ────────────────────────────────────────────────────────────────
def get_last_plate_and_status() -> tuple:
    conn = _db()
    row  = conn.execute(
        "SELECT plate FROM last_seen ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    last_plate = row["plate"] if row else "—"
    allowed    = {r["plate"] for r in conn.execute("SELECT plate FROM allowed_plates").fetchall()}
    conn.close()
    status = "ALLOWED" if last_plate in allowed else "DENIED"
    return last_plate, status


def insert_last_seen(plate: str, confidence: float, source_ip: str) -> None:
    conn = _db()
    conn.execute(
        "INSERT INTO last_seen (plate, confidence, source_ip) VALUES (?, ?, ?)",
        (plate, confidence, source_ip),
    )
    conn.commit()
    conn.close()


def is_allowed_plate(plate: str) -> bool:
    conn = _db()
    ok   = conn.execute(
        "SELECT 1 FROM allowed_plates WHERE plate = ? LIMIT 1", (plate,)
    ).fetchone()
    conn.close()
    return ok is not None


def get_camera_status() -> str:
    if not last_ingest_time:
        return "error"
    now   = time.time()
    alive = [ip for ip, t in last_ingest_time.items() if (now - t) < CAMERA_TIMEOUT_SEC]
    if not alive:
        return "error"
    for ip in alive:
        if camera_state.get(ip) == "recovered":
            return "recovered"
    return "ok"


def touch_camera(src_ip: str) -> None:
    now  = time.time()
    prev = last_ingest_time.get(src_ip)
    last_ingest_time[src_ip] = now
    if prev is None or (now - prev) >= CAMERA_TIMEOUT_SEC:
        camera_state[src_ip] = "recovered"
    elif camera_state.get(src_ip) not in ("ok", "recovered"):
        camera_state[src_ip] = "ok"


# ── Init ──────────────────────────────────────────────────────────────────────
init_db()

# ── Auth routes ───────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = _db()
        row  = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        conn.close()

        if row and check_password_hash(row["password"], password):
            user = User(row["id"], row["username"], row["role"])
            login_user(user, remember=True)
            return redirect(request.args.get("next") or url_for("index"))

        flash("Hibás felhasználónév vagy jelszó.", "error")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/change_password", methods=["POST"])
@login_required
def change_password():
    current_pw = request.form.get("current_password", "")
    new_pw     = request.form.get("new_password", "")
    confirm_pw = request.form.get("confirm_password", "")

    conn = _db()
    row  = conn.execute("SELECT * FROM users WHERE id = ?", (current_user.id,)).fetchone()

    if not check_password_hash(row["password"], current_pw):
        flash("A jelenlegi jelszó helytelen.", "error")
    elif len(new_pw) < 6:
        flash("Az új jelszónak legalább 6 karakter kell.", "error")
    elif new_pw != confirm_pw:
        flash("A két jelszó nem egyezik.", "error")
    else:
        conn.execute(
            "UPDATE users SET password = ?, pw_changed = 1 WHERE id = ?",
            (generate_password_hash(new_pw), current_user.id),
        )
        conn.commit()
        flash("Jelszó sikeresen megváltoztatva!", "success")

    conn.close()
    return redirect(url_for("index"))

# ── Main routes ───────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    last_plate, status = get_last_plate_and_status()
    # Warn admin if default password is still in use
    conn = _db()
    warn_pw = False
    if current_user.is_admin:
        row = conn.execute(
            "SELECT pw_changed FROM users WHERE id = ?", (current_user.id,)
        ).fetchone()
        warn_pw = row and not row["pw_changed"]
    conn.close()
    return render_template(
        "index.html", last_plate=last_plate, status=status, warn_pw=warn_pw
    )


@app.route("/last_plate")
@login_required
def last_plate_api():
    last_plate, status = get_last_plate_and_status()
    cam = get_camera_status()
    return jsonify({
        "plate":            last_plate,
        "status":           status,
        "camera_error":     cam == "error",
        "camera_recovered": cam == "recovered",
    })


@app.route("/events")
@login_required
def events():
    """
    Server-Sent Events endpoint for real-time dashboard updates.
    The browser connects once and receives JSON pushes whenever state changes.
    nginx must have proxy_buffering off for this location.
    """
    def stream():
        last_data = None
        while True:
            plate, status = get_last_plate_and_status()
            cam  = get_camera_status()
            data = {
                "plate":            plate,
                "status":           status,
                "camera_error":     cam == "error",
                "camera_recovered": cam == "recovered",
            }
            if data != last_data:
                last_data = data
                yield f"data: {json.dumps(data)}\n\n"
            time.sleep(1.0)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",   # tells nginx to disable buffering
        },
    )


@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    """Pi calls this every HEARTBEAT_INTERVAL seconds when camera is alive."""
    src_ip = request.headers.get("X-Real-IP", request.remote_addr)
    if ALLOWED_INGEST_IPS is not None and src_ip not in ALLOWED_INGEST_IPS:
        abort(403)
    touch_camera(src_ip)
    return jsonify({"ok": True})


@app.route("/plates", methods=["GET", "POST"])
@login_required
def plates():
    conn = _db()

    if request.method == "POST":
        if not current_user.is_admin:
            abort(403)
        plate = re.sub(r"[^A-Z0-9]", "", request.form.get("plate", "").strip().upper())
        if plate:
            conn.execute(
                "INSERT OR IGNORE INTO allowed_plates (plate) VALUES (?)", (plate,)
            )
            conn.commit()
        conn.close()
        return redirect(url_for("plates"))

    plates_list = [
        r["plate"] for r in conn.execute(
            "SELECT plate FROM allowed_plates ORDER BY plate"
        ).fetchall()
    ]
    conn.close()
    return render_template("plates.html", plates=plates_list)


@app.route("/plates/delete/<plate>")
@admin_required
def delete_plate(plate: str):
    plate = re.sub(r"[^A-Z0-9]", "", plate.upper())
    conn  = _db()
    conn.execute("DELETE FROM allowed_plates WHERE plate = ?", (plate,))
    conn.commit()
    conn.close()
    return redirect(url_for("plates"))


@app.route("/ingest", methods=["POST"])
def ingest():
    """Receives JPEG frames from the Pi, runs ALPR, votes, commits to DB."""
    src_ip = request.headers.get("X-Real-IP", request.remote_addr)
    if ALLOWED_INGEST_IPS is not None and src_ip not in ALLOWED_INGEST_IPS:
        abort(403)

    if "image" not in request.files:
        return jsonify({"error": "missing field 'image'"}), 400

    img_bytes = request.files["image"].read()
    if not img_bytes:
        return jsonify({"error": "empty image"}), 400

    np_img = np.frombuffer(img_bytes, np.uint8)
    frame  = cv2.imdecode(np_img, cv2.IMREAD_COLOR)
    if frame is None:
        return jsonify({"error": "invalid image data"}), 400

    touch_camera(src_ip)

    try:
        results = alpr.predict(frame)
    except Exception as exc:
        return jsonify({"error": f"alpr failed: {exc}"}), 500

    best_plate: str | None = None
    best_conf: float       = 0.0

    for r in results:
        o = r.ocr
        if not o or not o.text:
            continue
        conf = o.confidence
        if isinstance(conf, list):
            conf = statistics.mean(conf) if conf else 0.0
        conf = float(conf or 0.0)
        text = re.sub(r"[^A-Z0-9]", "", o.text.strip().upper())
        if text and conf > best_conf:
            best_plate, best_conf = text, conf

    now = time.time()

    if best_plate and best_conf >= MIN_PLATE_CONF:
        dq = recent_by_source[src_ip]
        dq.append((now, best_plate, best_conf))

        # Expire old entries outside the vote window
        while dq and (now - dq[0][0]) > VOTE_WINDOW_SEC:
            dq.popleft()

        counts: dict = {}
        confs:  dict = {}
        for _, p, c in dq:
            counts[p] = counts.get(p, 0) + 1
            confs.setdefault(p, []).append(c)

        winner   = max(counts, key=counts.get)
        votes    = counts[winner]
        avg_conf = sum(confs[winner]) / len(confs[winner])

        committed = False
        if votes >= VOTES_REQUIRED:
            insert_last_seen(winner, avg_conf, src_ip)
            committed = True
            camera_state[src_ip] = "ok"

        return jsonify({
            "plate":           best_plate,
            "confidence":      best_conf,
            "winner":          winner,
            "winner_avg_conf": avg_conf,
            "votes":           votes,
            "required":        VOTES_REQUIRED,
            "committed":       committed,
            "status":          "ALLOWED" if committed and is_allowed_plate(winner) else "DENIED",
        })

    return jsonify({"plate": None, "committed": False})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
