"""CivicClean backend: Flask + SQLite.
Run locally:  pip install -r requirements.txt  &&  python app.py
"""
import hmac
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from functools import lru_cache, wraps

from flask import Flask, g, jsonify, request, send_from_directory, session

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(BASE, "civicclean.db"))
PASSCODES = {
    "citizen": os.environ.get("CITIZEN_PASSCODE", "admin123"),
    "worker": os.environ.get("WORKER_PASSCODE", "admin123"),
    "admin": os.environ.get("ADMIN_PASSCODE", "admin123"),
}
OWNER_EMAIL = "himanshu@gmail.com"
OWNER_NAME = "Himanshu Meshram"


def display_name(email):
    if email == OWNER_EMAIL:
        return OWNER_NAME
    words = [w for w in re.split(r"[._\-\s]+", email.split("@")[0]) if w]
    return " ".join(w.capitalize() for w in words) or email


app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-in-production")
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024


# ---------- database ----------
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            last_login INTEGER NOT NULL,
            UNIQUE(name, role)
        );
        CREATE TABLE IF NOT EXISTS reports(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            by_name TEXT NOT NULL,
            addr TEXT,
            descr TEXT,
            lat REAL, lng REAL,
            before_img TEXT NOT NULL,
            after_img TEXT,
            after_lat REAL, after_lng REAL,
            worker TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            resolved_at INTEGER,
            remark TEXT,
            reviewed_by TEXT
        );
        """
    )
    cols = [r[1] for r in con.execute("PRAGMA table_info(reports)")]
    for c in ("remark", "reviewed_by"):
        if c not in cols:
            con.execute(f"ALTER TABLE reports ADD COLUMN {c} TEXT")
    for col in ("by_name", "worker", "reviewed_by"):
        con.execute(f"UPDATE reports SET {col}=? WHERE {col}='himanshumeshram'", (OWNER_NAME,))
    con.commit()
    con.close()


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    con = g.pop("db", None)
    if con:
        con.close()


init_db()


# ---------- helpers ----------
def now_ms():
    return int(time.time() * 1000)


def need(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            if session.get("role") not in roles:
                return jsonify(error="Please log in first"), 401
            return fn(*a, **k)
        return wrapper
    return deco


def valid_img(s):
    return isinstance(s, str) and s.startswith("data:image/") and len(s) < 3_000_000


def num(v):
    return float(v) if isinstance(v, (int, float)) else None


def week_count():
    since = now_ms() - 7 * 24 * 3600 * 1000
    row = db().execute(
        "SELECT COUNT(*) c FROM reports WHERE status='approved' AND resolved_at>?", (since,)
    ).fetchone()
    return row["c"]


def out(r):
    return dict(
        id=r["id"], by=r["by_name"], addr=r["addr"], desc=r["descr"],
        lat=r["lat"], lng=r["lng"], before=r["before_img"], after=r["after_img"],
        status=r["status"], at=r["created_at"], worker=r["worker"],
        remark=r["remark"], rby=r["reviewed_by"],
    )


# ---------- auth ----------
@app.post("/api/login")
def login():
    d = request.get_json(silent=True) or {}
    role = d.get("role")
    email = str(d.get("email") or d.get("name") or "").strip().lower()[:80]
    if role not in PASSCODES or not email:
        return jsonify(error="Please enter your email"), 400
    given = str(d.get("password") or "").encode()
    if not hmac.compare_digest(given, PASSCODES[role].encode()):
        return jsonify(error="Wrong password"), 401
    name = display_name(email)
    db().execute(
        "INSERT INTO users(name,role,last_login) VALUES(?,?,?) "
        "ON CONFLICT(name,role) DO UPDATE SET last_login=excluded.last_login",
        (name, role, now_ms()),
    )
    db().commit()
    session["name"], session["role"] = name, role
    return jsonify(name=name, role=role)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def me():
    return jsonify(name=session.get("name"), role=session.get("role"))


@app.get("/api/stats")
def stats():
    return jsonify(week=week_count())


# ---------- reports ----------
@app.get("/api/reports")
@need("citizen", "worker", "admin")
def list_reports():
    role, name, q = session["role"], session["name"], db()
    record = []
    if role == "citizen":
        rows = q.execute("SELECT * FROM reports WHERE by_name=? ORDER BY id DESC", (name,)).fetchall()
    elif role == "worker":
        rows = q.execute("SELECT * FROM reports WHERE status IN ('pending','rejected') ORDER BY id DESC").fetchall()
        record = q.execute(
            "SELECT * FROM reports WHERE worker=? AND status IN ('cleaned','approved') ORDER BY id DESC LIMIT 100",
            (name,),
        ).fetchall()
    else:
        rows = q.execute("SELECT * FROM reports WHERE status='cleaned' ORDER BY id DESC").fetchall()
        record = q.execute(
            "SELECT * FROM reports WHERE status IN ('approved','rejected') ORDER BY id DESC LIMIT 100"
        ).fetchall()
    return jsonify(reports=[out(r) for r in rows], record=[out(r) for r in record], week=week_count())


@app.post("/api/reports")
@need("citizen")
def create_report():
    d = request.get_json(silent=True) or {}
    if not valid_img(d.get("before")):
        return jsonify(error="A valid photo is required"), 400
    lat, lng = num(d.get("lat")), num(d.get("lng"))
    addr = str(d.get("addr", "")).strip()[:200]
    if lat is None and not addr:
        return jsonify(error="Please add the location"), 400
    db().execute(
        "INSERT INTO reports(by_name,addr,descr,lat,lng,before_img,created_at) VALUES(?,?,?,?,?,?,?)",
        (session["name"], addr or "GPS location", str(d.get("desc", ""))[:500], lat, lng, d["before"], now_ms()),
    )
    db().commit()
    return jsonify(ok=True), 201


@app.post("/api/reports/<int:rid>/clean")
@need("worker")
def clean_report(rid):
    d = request.get_json(silent=True) or {}
    if not valid_img(d.get("after")):
        return jsonify(error="A valid after photo is required"), 400
    cur = db().execute(
        "UPDATE reports SET after_img=?, after_lat=?, after_lng=?, worker=?, status='cleaned', "
        "remark=NULL, reviewed_by=NULL WHERE id=? AND status IN ('pending','rejected')",
        (d["after"], num(d.get("lat")), num(d.get("lng")), session["name"], rid),
    )
    db().commit()
    if cur.rowcount == 0:
        return jsonify(error="Report is not waiting for cleaning"), 409
    return jsonify(ok=True)


@app.post("/api/reports/<int:rid>/review")
@need("admin")
def review_report(rid):
    d = request.get_json(silent=True) or {}
    remark = str(d.get("remark", "")).strip()[:300]
    if d.get("approve"):
        sql = "UPDATE reports SET status='approved', resolved_at=?, remark=?, reviewed_by=? WHERE id=? AND status='cleaned'"
        args = (now_ms(), remark or None, session["name"], rid)
    else:
        if not remark:
            return jsonify(error="Please write a remark for the worker"), 400
        sql = "UPDATE reports SET status='rejected', remark=?, reviewed_by=? WHERE id=? AND status='cleaned'"
        args = (remark, session["name"], rid)
    cur = db().execute(sql, args)
    db().commit()
    if cur.rowcount == 0:
        return jsonify(error="Report is not waiting for approval"), 409
    return jsonify(ok=True)


@lru_cache(maxsize=512)
def reverse_geocode(lat, lng):
    """Turn coordinates into a place name using OpenStreetMap Nominatim."""
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"format": "jsonv2", "lat": lat, "lon": lng, "zoom": 18, "addressdetails": 1}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "CivicClean/1.0 (civic garbage reporting app)"})
    with urllib.request.urlopen(req, timeout=6) as r:
        d = json.load(r)
    addr = d.get("address", {})
    parts = []
    for keys in (
        ("road", "pedestrian", "footway"),
        ("neighbourhood", "suburb", "quarter", "village", "hamlet"),
        ("city", "town", "municipality", "county", "state_district"),
        ("state",),
    ):
        for k in keys:
            v = addr.get(k)
            if v and v not in parts:
                parts.append(v)
                break
    return ", ".join(parts) or ", ".join(d.get("display_name", "").split(",")[:3]).strip()


@app.get("/api/geocode")
@need("citizen", "worker", "admin")
def geocode():
    lat, lng = request.args.get("lat", type=float), request.args.get("lng", type=float)
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify(error="Bad coordinates"), 400
    try:
        return jsonify(name=reverse_geocode(round(lat, 4), round(lng, 4)))
    except Exception:
        return jsonify(name="")


@app.get("/")
def index():
    return send_from_directory(BASE, "index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
