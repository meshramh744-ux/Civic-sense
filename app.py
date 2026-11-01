"""CivicClean backend: Flask + SQLite.
Run locally:  pip install -r requirements.txt  &&  python app.py
"""
import hmac
import os
import sqlite3
import time
from functools import wraps

from flask import Flask, g, jsonify, request, session

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(BASE, "civicclean.db"))
PASSCODES = {
    "worker": os.environ.get("WORKER_PASSCODE", "worker123"),
    "admin": os.environ.get("ADMIN_PASSCODE", "admin123"),
}

app = Flask(__name__, static_folder="static", static_url_path="")
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
            resolved_at INTEGER
        );
        """
    )
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
        alat=r["after_lat"], alng=r["after_lng"], status=r["status"], at=r["created_at"],
    )


# ---------- auth ----------
@app.post("/api/login")
def login():
    d = request.get_json(silent=True) or {}
    role = d.get("role")
    name = str(d.get("name", "")).strip()[:40]
    if role not in ("citizen", "worker", "admin") or not name:
        return jsonify(error="Please enter your name"), 400
    if role != "citizen":
        if not hmac.compare_digest(str(d.get("passcode", "")), PASSCODES[role]):
            return jsonify(error="Wrong passcode"), 401
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
    role = session["role"]
    if role == "citizen":
        rows = db().execute(
            "SELECT * FROM reports WHERE by_name=? ORDER BY id DESC", (session["name"],)
        ).fetchall()
    elif role == "worker":
        rows = db().execute("SELECT * FROM reports WHERE status='pending' ORDER BY id DESC").fetchall()
    else:
        rows = db().execute("SELECT * FROM reports WHERE status='cleaned' ORDER BY id DESC").fetchall()
    pending = db().execute("SELECT COUNT(*) c FROM reports WHERE status='pending'").fetchone()["c"]
    return jsonify(reports=[out(r) for r in rows], pending=pending, week=week_count())


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
        "UPDATE reports SET after_img=?, after_lat=?, after_lng=?, worker=?, status='cleaned' "
        "WHERE id=? AND status='pending'",
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
    if d.get("approve"):
        sql = "UPDATE reports SET status='approved', resolved_at=? WHERE id=? AND status='cleaned'"
        args = (now_ms(), rid)
    else:
        sql = "UPDATE reports SET status='pending', after_img=NULL, worker=NULL, resolved_at=? WHERE id=? AND status='cleaned'"
        args = (None, rid)
    cur = db().execute(sql, args)
    db().commit()
    if cur.rowcount == 0:
        return jsonify(error="Report is not waiting for approval"), 409
    return jsonify(ok=True)


@app.get("/")
def index():
    return app.send_static_file("index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
