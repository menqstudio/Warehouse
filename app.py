# -*- coding: utf-8 -*-
"""
MenQ — Խանութի կառավարում / Store management (Pro)
Universal: works for ANY shop. Warehouse + POS + delivery + purchases +
returns + multi-shop + finance + reports. Configurable product attributes.
Pure Python stdlib (http.server + sqlite3). Zero dependencies.
Run:  python app.py        ->  http://127.0.0.1:8765
      python app.py lan    ->  serve on the whole local network
Packaged (PyInstaller) mode is supported: index.html is bundled and the
database lives in %LOCALAPPDATA%\\MenQ so it survives updates/reinstalls.
"""
import json
import os
import sys
import sqlite3
import hashlib
import secrets
import subprocess
import webbrowser
import threading
import csv
import io
import tempfile
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Windows consoles often default to cp1252 — force UTF-8 so Armenian prints.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

APP_NAME = "MenQ"
FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    # Running as a packaged .exe: bundled files live in sys._MEIPASS (read-only),
    # while the database must sit in a per-user writable folder.
    RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA")
                            or os.path.dirname(sys.executable), "MenQ")
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        DATA_DIR = os.path.dirname(sys.executable)
else:
    RES_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = RES_DIR
BASE_DIR = DATA_DIR
DB_PATH = os.path.join(DATA_DIR, "warehouse.db")
INDEX_PATH = os.path.join(RES_DIR, "index.html")
HOST, PORT = "127.0.0.1", 8765
WAREHOUSE = "Պահեստ"   # canonical main location (== products.quantity)
MAX_PHOTO = 700_000    # cap stored photo data-URL length (~0.5 MB image)
SRV = None             # running server (set in main) so /api/shutdown can stop it
LAN_URL = ""           # set in main when serving on the LAN; shown in the UI


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 8000")   # wait out concurrent writers
    return conn


def money2(x):
    """Round money to 2 decimals, killing float dust (e.g. 50.050000004)."""
    try:
        return round(float(x or 0) + 0.0, 2)
    except (TypeError, ValueError):
        return 0.0


# Core product columns. Store-specific variant fields (size/color/…) live in
# the flexible JSON `attrs` column and are defined by the owner in Settings.
PRODUCT_COLUMNS = [
    ("barcode", "TEXT NOT NULL DEFAULT ''"),
    ("sku", "TEXT NOT NULL DEFAULT ''"),
    ("name", "TEXT NOT NULL DEFAULT ''"),
    ("category", "TEXT NOT NULL DEFAULT ''"),
    ("brand", "TEXT NOT NULL DEFAULT ''"),
    ("supplier", "TEXT NOT NULL DEFAULT ''"),
    ("attrs", "TEXT NOT NULL DEFAULT '{}'"),
    ("photo", "TEXT NOT NULL DEFAULT ''"),
    ("unit", "TEXT NOT NULL DEFAULT 'հատ'"),
    ("cost_price", "REAL NOT NULL DEFAULT 0"),
    ("sell_price", "REAL NOT NULL DEFAULT 0"),
    ("wholesale_price", "REAL NOT NULL DEFAULT 0"),
    ("quantity", "INTEGER NOT NULL DEFAULT 0"),
    ("min_qty", "INTEGER NOT NULL DEFAULT 2"),
]

# Master-data reference entities: entity -> ordered column list (besides id).
REF_ENTITIES = {
    "categories": ["name"],
    "brands":     ["name"],
    "units":      ["name"],
    "suppliers":  ["name", "phone", "contact", "note"],
    "shops":      ["name", "address", "phone", "contact", "note"],
    "couriers":   ["name", "phone", "note"],
    "cars":       ["courier", "make", "model", "plate"],
    "customers":  ["name", "phone", "bonus", "note"],
}

DEFAULT_ATTRS = []  # generic store starts with no custom attributes


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL DEFAULT '',
            updated_at  TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS movements (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            kind        TEXT NOT NULL,
            qty         INTEGER NOT NULL,
            unit_cost   REAL NOT NULL DEFAULT 0,
            unit_sell   REAL NOT NULL DEFAULT 0,
            ref         TEXT NOT NULL DEFAULT '',
            note        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sales (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ref         TEXT NOT NULL DEFAULT '',
            type        TEXT NOT NULL DEFAULT 'retail',
            price_mode  TEXT NOT NULL DEFAULT 'retail',
            location    TEXT NOT NULL DEFAULT '',
            courier     TEXT NOT NULL DEFAULT '',
            shop        TEXT NOT NULL DEFAULT '',
            customer    TEXT NOT NULL DEFAULT '',
            payment     TEXT NOT NULL DEFAULT 'cash',
            subtotal    REAL NOT NULL DEFAULT 0,
            discount    REAL NOT NULL DEFAULT 0,
            tax         REAL NOT NULL DEFAULT 0,
            total       REAL NOT NULL DEFAULT 0,
            paid        REAL NOT NULL DEFAULT 0,
            debt        REAL NOT NULL DEFAULT 0,
            profit      REAL NOT NULL DEFAULT 0,
            bonus       REAL NOT NULL DEFAULT 0,
            seller      TEXT NOT NULL DEFAULT '',
            note        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sale_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id     INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            product_id  INTEGER NOT NULL,
            name        TEXT NOT NULL DEFAULT '',
            variant     TEXT NOT NULL DEFAULT '',
            qty         INTEGER NOT NULL DEFAULT 0,
            unit_price  REAL NOT NULL DEFAULT 0,
            unit_cost   REAL NOT NULL DEFAULT 0,
            line        REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS purchases (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ref         TEXT NOT NULL DEFAULT '',
            supplier    TEXT NOT NULL DEFAULT '',
            location    TEXT NOT NULL DEFAULT '',
            payment     TEXT NOT NULL DEFAULT 'cash',
            total       REAL NOT NULL DEFAULT 0,
            paid        REAL NOT NULL DEFAULT 0,
            debt        REAL NOT NULL DEFAULT 0,
            note        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS purchase_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_id INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
            product_id  INTEGER NOT NULL,
            name        TEXT NOT NULL DEFAULT '',
            qty         INTEGER NOT NULL DEFAULT 0,
            unit_cost   REAL NOT NULL DEFAULT 0,
            line        REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS returns (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ref         TEXT NOT NULL DEFAULT '',
            sale_ref    TEXT NOT NULL DEFAULT '',
            location    TEXT NOT NULL DEFAULT '',
            payment     TEXT NOT NULL DEFAULT 'cash',
            total       REAL NOT NULL DEFAULT 0,
            profit      REAL NOT NULL DEFAULT 0,
            seller      TEXT NOT NULL DEFAULT '',
            note        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS return_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            return_id   INTEGER NOT NULL REFERENCES returns(id) ON DELETE CASCADE,
            product_id  INTEGER NOT NULL,
            name        TEXT NOT NULL DEFAULT '',
            variant     TEXT NOT NULL DEFAULT '',
            qty         INTEGER NOT NULL DEFAULT 0,
            unit_price  REAL NOT NULL DEFAULT 0,
            unit_cost   REAL NOT NULL DEFAULT 0,
            line        REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS cashbox (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            kind        TEXT NOT NULL DEFAULT 'in',
            amount      REAL NOT NULL DEFAULT 0,
            location    TEXT NOT NULL DEFAULT '',
            reason      TEXT NOT NULL DEFAULT '',
            ref         TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS loc_stock (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id  INTEGER NOT NULL,
            location    TEXT NOT NULL,
            qty         INTEGER NOT NULL DEFAULT 0,
            UNIQUE(product_id, location)
        );
        CREATE TABLE IF NOT EXISTS transfers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id  INTEGER NOT NULL,
            from_loc    TEXT NOT NULL DEFAULT '',
            to_loc      TEXT NOT NULL DEFAULT '',
            qty         INTEGER NOT NULL DEFAULT 0,
            note        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT UNIQUE NOT NULL,
            password    TEXT NOT NULL,
            role        TEXT NOT NULL DEFAULT 'seller',
            name        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT ''
        );
        """
    )
    # migrate: add any missing product columns (keeps old DBs working)
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
    for col, decl in PRODUCT_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE products ADD COLUMN {col} {decl}")
    # migrate legacy shoe columns (color/size/gender) into attrs JSON, once
    legacy = [c for c in ("color", "size", "gender") if c in existing]
    if legacy and "attrs" not in existing:
        pass  # brand-new column, nothing to migrate
    if legacy:
        for r in conn.execute("SELECT id, attrs, "
                              + ", ".join(legacy) + " FROM products").fetchall():
            try:
                cur = json.loads(r["attrs"] or "{}")
            except Exception:
                cur = {}
            changed = False
            for c in legacy:
                if r[c] and c not in cur:
                    cur[c] = r[c]; changed = True
            if changed:
                conn.execute("UPDATE products SET attrs=? WHERE id=?",
                             (json.dumps(cur, ensure_ascii=False), r["id"]))
    # ensure newer columns on older sales/cashbox tables
    for tbl, col, decl in (("sales", "location", "TEXT NOT NULL DEFAULT ''"),
                           ("sales", "tax", "REAL NOT NULL DEFAULT 0"),
                           ("sales", "seller", "TEXT NOT NULL DEFAULT ''"),
                           ("sale_items", "variant", "TEXT NOT NULL DEFAULT ''"),
                           ("cashbox", "location", "TEXT NOT NULL DEFAULT ''"),
                           ("movements", "ref", "TEXT NOT NULL DEFAULT ''")):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({tbl})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {decl}")
    # reference (master-data) tables
    for entity, cols in REF_ENTITIES.items():
        col_defs = ", ".join(f"{c} TEXT NOT NULL DEFAULT ''" for c in cols)
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS ref_{entity} "
            f"(id INTEGER PRIMARY KEY AUTOINCREMENT, {col_defs}, created_at TEXT NOT NULL DEFAULT '')"
        )
    # default settings
    for k, v in (("shop_name", "Իմ խանութը"), ("currency", "֏"),
                 ("bonus_percent", "0"), ("tax_percent", "0"),
                 ("label_w", "40"), ("label_h", "30"),
                 ("attrs_def", json.dumps(DEFAULT_ATTRS, ensure_ascii=False))):
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    # seed common units once
    if not conn.execute("SELECT COUNT(*) c FROM ref_units").fetchone()["c"]:
        for u in ("հատ", "կգ", "գ", "լ", "մլ", "մ", "տուփ", "փաթեթ"):
            conn.execute("INSERT INTO ref_units(name,created_at) VALUES(?,?)", (u, now_iso()))
    # seed default users once (name left blank -> UI shows the translated role)
    if not conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]:
        conn.execute("INSERT INTO users(username,password,role,name,created_at) VALUES(?,?,?,?,?)",
                     ("admin", hash_pw("admin"), "admin", "", now_iso()))
        conn.execute("INSERT INTO users(username,password,role,name,created_at) VALUES(?,?,?,?,?)",
                     ("vacharox", hash_pw("1234"), "seller", "", now_iso()))
    # migrate old demo dbs ONCE: clear the language-locked default names.
    # Gated by a flag so a user who deliberately names themselves "Տնօրեն" later
    # is never silently reset on the next startup.
    if not conn.execute(
            "SELECT 1 FROM settings WHERE key='_migr_names'").fetchone():
        conn.execute("UPDATE users SET name='' WHERE (username='admin' AND name='Տնօրեն') "
                     "OR (username='vacharox' AND name='Աշխատող')")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('_migr_names','1')")
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ----------------------------------------------------------------------------
# Auth (users, passwords, sessions)
# ----------------------------------------------------------------------------
SESSIONS = {}  # token -> username


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                            salt.encode("utf-8"), 100000).hex()
    return f"{salt}${h}"


def verify_pw(pw, stored):
    try:
        salt, h = stored.split("$", 1)
    except (ValueError, AttributeError):
        return False
    calc = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                               salt.encode("utf-8"), 100000).hex()
    return secrets.compare_digest(calc, h)


def authenticate(username, password):
    conn = get_db()
    u = conn.execute("SELECT * FROM users WHERE username=?",
                     (username.strip(),)).fetchone()
    conn.close()
    if u and verify_pw(password, u["password"]):
        return {"username": u["username"], "role": u["role"], "name": u["name"]}
    return None


def _clean_name(s):
    """Sanitize a user-supplied display name: drop angle brackets (defense-in-depth
    against stored XSS if a name is ever rendered unescaped) and cap the length."""
    return (s or "").replace("<", "").replace(">", "").strip()[:60]


def update_own_profile(username, data):
    """Let the logged-in user set their own display name and (optionally) password."""
    conn = get_db()
    u = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not u:
        conn.close()
        return {"error": "not found"}, 404
    nw = (data.get("new") or "").strip()
    if nw:
        if not verify_pw(data.get("current") or "", u["password"]):
            conn.close()
            return {"error": "wrong current password"}, 400
        conn.execute("UPDATE users SET password=? WHERE username=?", (hash_pw(nw), username))
    if "name" in data:
        conn.execute("UPDATE users SET name=? WHERE username=?",
                     (_clean_name(data.get("name")), username))
    conn.commit()
    row = conn.execute("SELECT username,role,name FROM users WHERE username=?",
                       (username,)).fetchone()
    conn.close()
    return {"ok": True, "username": row["username"], "role": row["role"], "name": row["name"]}


def user_by_name(username):
    conn = get_db()
    u = conn.execute("SELECT username,role,name FROM users WHERE username=?",
                     (username,)).fetchone()
    conn.close()
    return dict(u) if u else None


def list_users():
    conn = get_db()
    rows = conn.execute(
        "SELECT id,username,role,name,created_at FROM users ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_user(data):
    username = (data.get("username") or "").strip()
    pw = (data.get("password") or "").strip()
    role = data.get("role") if data.get("role") in ("admin", "seller") else "seller"
    if not username or not pw:
        return {"error": "username and password required"}, 400
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users(username,password,role,name,created_at) VALUES(?,?,?,?,?)",
            (username, hash_pw(pw), role, _clean_name(data.get("name")), now_iso()))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return {"error": "username exists"}, 400
    conn.close()
    return {"ok": True}


def update_user(uid, data):
    conn = get_db()
    if data.get("password"):
        conn.execute("UPDATE users SET password=? WHERE id=?",
                     (hash_pw(data["password"].strip()), uid))
    if data.get("role") in ("admin", "seller"):
        # never let the last admin be demoted to seller (would lock everyone out)
        if data["role"] == "seller":
            cur = conn.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
            if cur and cur["role"] == "admin":
                n = conn.execute(
                    "SELECT COUNT(*) c FROM users WHERE role='admin'").fetchone()["c"]
                if n <= 1:
                    conn.close()
                    return {"error": "cannot demote the last admin"}, 400
        conn.execute("UPDATE users SET role=? WHERE id=?", (data["role"], uid))
    if "name" in data:
        conn.execute("UPDATE users SET name=? WHERE id=?",
                     (_clean_name(data.get("name")), uid))
    conn.commit()
    conn.close()
    return {"ok": True}


def delete_user(uid):
    conn = get_db()
    row = conn.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
    if row and row["role"] == "admin":
        n = conn.execute("SELECT COUNT(*) c FROM users WHERE role='admin'").fetchone()["c"]
        if n <= 1:
            conn.close()
            return {"error": "cannot delete the last admin"}, 400
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return {"ok": True}


def ean13_check(d12):
    s = sum((3 if i % 2 else 1) * int(c) for i, c in enumerate(d12))
    return str((10 - s % 10) % 10)


def gen_barcode(pid):
    body = ("200" + str(pid).zfill(9))[:12]
    return body + ean13_check(body)


# ----------------------------------------------------------------------------
# Attributes (owner-defined product fields) helpers
# ----------------------------------------------------------------------------
def attrs_def():
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key='attrs_def'").fetchone()
    conn.close()
    try:
        d = json.loads(row["value"]) if row else []
        return d if isinstance(d, list) else []
    except Exception:
        return []


def variant_str(attrs):
    """Human-readable variant label from an attrs dict, e.g. '42 · Սև'."""
    if not attrs:
        return ""
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs or "{}")
        except Exception:
            return ""
    vals = [str(v).strip() for v in attrs.values() if str(v).strip()]
    return " · ".join(vals)


# ----------------------------------------------------------------------------
# Products
# ----------------------------------------------------------------------------
def row_to_dict(r):
    d = dict(r)
    try:
        d["attrs"] = json.loads(d.get("attrs") or "{}")
    except Exception:
        d["attrs"] = {}
    d["variant"] = variant_str(d["attrs"])
    d["low"] = bool(d.get("quantity", 0) <= d.get("min_qty", 0))
    d["stock_cost"] = round(d.get("quantity", 0) * d.get("cost_price", 0), 2)
    d["stock_retail"] = round(d.get("quantity", 0) * d.get("sell_price", 0), 2)
    d["has_photo"] = bool(d.get("photo"))
    return d


def list_products(search="", with_photo=False):
    conn = get_db()
    if search:
        like = f"%{search}%"
        rows = conn.execute(
            """SELECT * FROM products
               WHERE barcode LIKE ? OR sku LIKE ? OR brand LIKE ? OR name LIKE ?
                  OR category LIKE ? OR supplier LIKE ? OR attrs LIKE ?
               ORDER BY id DESC""",
            (like,) * 7,
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = row_to_dict(r)
        if not with_photo:
            d.pop("photo", None)  # keep list responses light
        out.append(d)
    return out


def get_product(pid):
    conn = get_db()
    r = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    return row_to_dict(r) if r else None


def get_by_barcode(code):
    conn = get_db()
    r = conn.execute("SELECT * FROM products WHERE barcode=?", (code.strip(),)).fetchone()
    conn.close()
    if not r:
        return None
    d = row_to_dict(r)
    d.pop("photo", None)
    return d


def _clean_photo(p):
    p = (p or "").strip()
    if not p:
        return ""
    if not p.startswith("data:image/"):
        return ""
    if len(p) > MAX_PHOTO:      # reject oversized — never truncate base64 (corrupts the image)
        return None             # signal "too big" to the caller
    return p


def create_product(data):
    photo = _clean_photo(data.get("photo"))
    if photo is None:
        return {"error": "photo too big"}, 400
    for f in ("quantity", "cost_price", "sell_price", "wholesale_price", "min_qty"):
        try:
            if float(data.get(f) or 0) < 0:
                return {"error": "negative not allowed"}, 400
        except (TypeError, ValueError):
            return {"error": "bad number"}, 400
    conn = get_db()
    ts = now_iso()
    attrs = data.get("attrs") or {}
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except Exception:
            attrs = {}
    cur = conn.execute(
        """INSERT INTO products
           (barcode, sku, name, category, brand, supplier, attrs, photo, unit,
            cost_price, sell_price, wholesale_price, quantity, min_qty,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            data.get("barcode", "").strip(),
            data.get("sku", "").strip(),
            data.get("name", "").strip(),
            data.get("category", "").strip(),
            data.get("brand", "").strip(),
            data.get("supplier", "").strip(),
            json.dumps(attrs, ensure_ascii=False),
            photo,
            (data.get("unit") or "հատ").strip(),
            float(data.get("cost_price") or 0),
            float(data.get("sell_price") or 0),
            float(data.get("wholesale_price") or 0),
            int(data.get("quantity") or 0),
            int(data.get("min_qty") or 0),
            ts, ts,
        ),
    )
    pid = cur.lastrowid
    if not data.get("barcode", "").strip():
        bc = gen_barcode(pid)
        conn.execute("UPDATE products SET barcode=? WHERE id=?", (bc, pid))
    qty = int(data.get("quantity") or 0)
    if qty > 0:
        conn.execute(
            """INSERT INTO movements
               (product_id, kind, qty, unit_cost, unit_sell, ref, note, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (pid, "in", qty, float(data.get("cost_price") or 0),
             float(data.get("sell_price") or 0), "",
             "Սկզբնական մնացորդ / Initial stock", ts),
        )
    conn.commit()
    conn.close()
    return {"id": pid}


def update_product(pid, data):
    if "photo" in data:
        photo = _clean_photo(data.get("photo"))
        if photo is None:
            return {"error": "photo too big"}, 400
    for f in ("cost_price", "sell_price", "wholesale_price", "min_qty"):
        try:
            if float(data.get(f) or 0) < 0:
                return {"error": "negative not allowed"}, 400
        except (TypeError, ValueError):
            return {"error": "bad number"}, 400
    conn = get_db()
    ts = now_iso()
    attrs = data.get("attrs") or {}
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except Exception:
            attrs = {}
    # keep existing photo unless a new one is explicitly provided
    sets = ["barcode=?", "sku=?", "name=?", "category=?", "brand=?", "supplier=?",
            "attrs=?", "unit=?", "cost_price=?", "sell_price=?", "wholesale_price=?",
            "min_qty=?", "updated_at=?"]
    vals = [
        data.get("barcode", "").strip(),
        data.get("sku", "").strip(),
        data.get("name", "").strip(),
        data.get("category", "").strip(),
        data.get("brand", "").strip(),
        data.get("supplier", "").strip(),
        json.dumps(attrs, ensure_ascii=False),
        (data.get("unit") or "հատ").strip(),
        float(data.get("cost_price") or 0),
        float(data.get("sell_price") or 0),
        float(data.get("wholesale_price") or 0),
        int(data.get("min_qty") or 0),
        ts,
    ]
    if "photo" in data:
        sets.insert(7, "photo=?")
        vals.insert(7, photo)
    vals.append(pid)
    conn.execute(f"UPDATE products SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()
    conn.close()
    return {"ok": True}


def delete_product(pid):
    conn = get_db()
    conn.execute("DELETE FROM products WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return {"ok": True}


def add_movement(data):
    pid = int(data["product_id"])
    kind = data.get("kind", "in")
    qty = int(data.get("qty") or 0)
    note = data.get("note", "").strip()
    # reject bad quantities: a negative "in"/"out" would silently reverse the movement
    if kind in ("in", "out") and qty <= 0:
        return {"error": "bad qty"}, 400
    if kind == "adjust" and qty < 0:
        return {"error": "bad qty"}, 400
    conn = get_db()
    p = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        conn.close()
        return {"error": "product not found"}, 404
    ts = now_iso()
    # relative, guarded writes so a concurrent sale/movement can't be clobbered
    if kind == "in":
        conn.execute("UPDATE products SET quantity=quantity+?, updated_at=? WHERE id=?",
                     (qty, ts, pid))
        new_qty = p["quantity"] + qty
    elif kind == "out":
        cur = conn.execute("UPDATE products SET quantity=quantity-?, updated_at=? "
                           "WHERE id=? AND quantity>=?", (qty, ts, pid, qty))
        if cur.rowcount == 0:      # stock moved under us — reread and reject
            avail = conn.execute("SELECT quantity q FROM products WHERE id=?",
                                 (pid,)).fetchone()["q"]
            conn.close()
            return {"error": "not enough stock", "available": avail}, 400
        new_qty = p["quantity"] - qty
    elif kind == "adjust":       # absolute set — manual admin correction
        new_qty = qty
        qty = new_qty - p["quantity"]
        conn.execute("UPDATE products SET quantity=?, updated_at=? WHERE id=?",
                     (new_qty, ts, pid))
    else:
        conn.close()
        return {"error": "bad kind"}, 400
    conn.execute(
        """INSERT INTO movements
           (product_id, kind, qty, unit_cost, unit_sell, ref, note, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (pid, kind, abs(qty), p["cost_price"], p["sell_price"], "", note, ts),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "quantity": new_qty}


# ----------------------------------------------------------------------------
# Locations & stock
# ----------------------------------------------------------------------------
def locations():
    conn = get_db()
    shops = [r["name"] for r in conn.execute(
        "SELECT name FROM ref_shops WHERE name<>'' ORDER BY name").fetchall()]
    conn.close()
    return [WAREHOUSE] + shops


def loc_get(conn, pid, loc):
    if loc == WAREHOUSE:
        r = conn.execute("SELECT quantity q FROM products WHERE id=?", (pid,)).fetchone()
        return r["q"] if r else 0
    r = conn.execute("SELECT qty FROM loc_stock WHERE product_id=? AND location=?",
                     (pid, loc)).fetchone()
    return r["qty"] if r else 0


def loc_add(conn, pid, loc, delta, ts):
    if loc == WAREHOUSE:
        conn.execute("UPDATE products SET quantity=quantity+?, updated_at=? WHERE id=?",
                     (delta, ts, pid))
    else:
        conn.execute(
            "INSERT INTO loc_stock(product_id,location,qty) VALUES(?,?,?) "
            "ON CONFLICT(product_id,location) DO UPDATE SET qty=qty+?",
            (pid, loc, delta, delta))


def loc_take(conn, pid, loc, qty, ts):
    """Atomically remove `qty` from a location only if enough is on hand.
    Returns True on success. The WHERE qty>=? guard, evaluated under SQLite's
    write lock, prevents two concurrent sales from overselling the last unit."""
    if loc == WAREHOUSE:
        cur = conn.execute(
            "UPDATE products SET quantity=quantity-?, updated_at=? "
            "WHERE id=? AND quantity>=?", (qty, ts, pid, qty))
    else:
        cur = conn.execute(
            "UPDATE loc_stock SET qty=qty-? "
            "WHERE product_id=? AND location=? AND qty>=?", (qty, pid, loc, qty))
    return cur.rowcount == 1


# ----------------------------------------------------------------------------
# Sales (POS + delivery), multi-location
# ----------------------------------------------------------------------------
def make_sale(data):
    items = data.get("items") or []
    if not items:
        return {"error": "empty sale"}, 400
    price_mode = data.get("price_mode", "retail")
    stype = data.get("type", "retail")
    payment = data.get("payment", "cash")
    location = (data.get("location") or WAREHOUSE).strip() or WAREHOUSE
    courier = data.get("courier", "").strip()
    shop = data.get("shop", "").strip()
    customer = data.get("customer", "").strip()
    discount = float(data.get("discount") or 0)
    note = data.get("note", "").strip()
    seller = (data.get("_seller") or "").strip()
    is_admin = (data.get("_role") == "admin")   # only admins may bend the price

    conn = get_db()
    ts = now_iso()
    ref = "S-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    # aggregate quantities + optional per-line custom price per product
    agg = {}; order = []
    for it in items:
        pid = int(it["product_id"]); q = int(it.get("qty") or 0)
        if q <= 0:
            conn.close()
            return {"error": "bad qty"}, 400
        if pid not in agg:
            agg[pid] = {"qty": 0, "unit_price": None}; order.append(pid)
        agg[pid]["qty"] += q
        up = it.get("unit_price")
        if up not in (None, ""):
            agg[pid]["unit_price"] = float(up)
    rows = {}
    for pid in order:
        p = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            conn.close()
            return {"error": f"product {pid} not found"}, 404
        rows[pid] = p

    settings = get_settings()
    subtotal = profit = 0
    line_rows = []
    for pid in order:
        p = rows[pid]; q = agg[pid]["qty"]
        # SECURITY: a seller can never set a custom price or use wholesale — that would
        # let them sell at 0 straight through the API. Only admins get those levers.
        if is_admin and agg[pid]["unit_price"] is not None:      # admin cashier-set price wins
            unit = max(0.0, agg[pid]["unit_price"])
        elif is_admin and price_mode == "wholesale" and p["wholesale_price"] > 0:
            unit = p["wholesale_price"]
        else:
            unit = p["sell_price"]
        line = q * unit
        subtotal += line
        profit += q * (unit - p["cost_price"])
        line_rows.append((pid, p, q, unit, line))

    if not is_admin:
        discount = 0.0                               # sellers cannot discount (theft guard)
    discount = max(0.0, min(discount, subtotal))    # never negative / over subtotal
    after_disc = max(0, subtotal - discount)
    try:
        tax_pct = float(settings.get("tax_percent", "0") or 0)
    except ValueError:
        tax_pct = 0
    tax = money2(after_disc * tax_pct / 100)
    subtotal = money2(subtotal); discount = money2(discount)
    total = money2(after_disc + tax)
    profit = money2(profit - discount)
    if payment == "credit":
        paid = min(money2(max(0, float(data.get("paid") or 0))), total)  # prepay, never negative
        debt = money2(total - paid)
    else:
        paid = total
        debt = 0

    bonus = 0
    if customer:
        try:
            pct = float(settings.get("bonus_percent", "0") or 0)
        except ValueError:
            pct = 0
        bonus = money2(total * pct / 100)

    cur = conn.execute(
        """INSERT INTO sales
           (ref, type, price_mode, location, courier, shop, customer, payment,
            subtotal, discount, tax, total, paid, debt, profit, bonus, seller, note, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ref, stype, price_mode, location, courier, shop, customer, payment,
         subtotal, discount, tax, total, paid, debt, profit, bonus, seller, note, ts),
    )
    sale_id = cur.lastrowid

    receipt = []
    for pid, p, q, unit, line in line_rows:
        if not loc_take(conn, pid, location, q, ts):   # atomic; rolls back on shortfall
            conn.rollback(); conn.close()
            return {"error": "not enough stock", "product_id": pid,
                    "location": location}, 400
        var = variant_str(p["attrs"])
        conn.execute(
            """INSERT INTO movements
               (product_id, kind, qty, unit_cost, unit_sell, ref, note, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (pid, "out", q, p["cost_price"], unit, ref,
             (f"{stype}/{payment}@{location}" + (f" → {shop}" if shop else "")), ts),
        )
        nm = (f'{p["brand"]} {p["name"]}').strip()
        conn.execute(
            """INSERT INTO sale_items
               (sale_id, product_id, name, variant, qty, unit_price, unit_cost, line)
               VALUES (?,?,?,?,?,?,?,?)""",
            (sale_id, pid, nm, var, q, unit, p["cost_price"], line),
        )
        receipt.append({"name": nm, "variant": var, "qty": q,
                        "unit_sell": unit, "line": line})

    if customer and bonus:
        c = conn.execute("SELECT id,bonus FROM ref_customers WHERE name=?",
                         (customer,)).fetchone()
        if c:
            try:
                newb = float(c["bonus"] or 0) + bonus
            except ValueError:
                newb = bonus
            conn.execute("UPDATE ref_customers SET bonus=? WHERE id=?",
                         (str(round(newb, 2)), c["id"]))

    if payment in ("cash", "credit") and paid > 0:
        cashbox_add(conn, "in", paid, f"Վաճառք / Sale {ref}", ref, ts, location)

    conn.commit()
    conn.close()
    return {"ok": True, "id": sale_id, "ref": ref, "type": stype,
            "price_mode": price_mode, "location": location, "courier": courier,
            "shop": shop, "customer": customer, "payment": payment,
            "subtotal": subtotal, "discount": discount, "tax": tax, "total": total,
            "paid": paid, "debt": debt, "profit": profit, "bonus": bonus,
            "seller": seller, "items": receipt, "created_at": ts}


def list_sales(limit=200):
    conn = get_db()
    rows = conn.execute("SELECT * FROM sales ORDER BY id DESC LIMIT ?",
                        (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def sale_detail(sid):
    conn = get_db()
    s = conn.execute("SELECT * FROM sales WHERE id=?", (sid,)).fetchone()
    if not s:
        conn.close()
        return None
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM sale_items WHERE sale_id=?", (sid,)).fetchall()]
    conn.close()
    d = dict(s); d["items"] = items
    return d


def list_debts():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM sales WHERE debt > 0 ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------------
# Purchases (receiving stock) + supplier debt (accounts payable)
# ----------------------------------------------------------------------------
def make_purchase(data):
    items = data.get("items") or []
    if not items:
        return {"error": "empty purchase"}, 400
    supplier = (data.get("supplier") or "").strip()
    location = (data.get("location") or WAREHOUSE).strip() or WAREHOUSE
    payment = data.get("payment", "cash")   # cash | credit | transfer
    note = data.get("note", "").strip()
    conn = get_db()
    ts = now_iso()
    ref = "P-" + datetime.now().strftime("%Y%m%d-%H%M%S")

    total = 0
    prepared = []
    for it in items:
        pid = int(it["product_id"])
        p = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            conn.close()
            return {"error": f"product {pid} not found"}, 404
        q = int(it.get("qty") or 0)
        uc = float(it.get("unit_cost") if it.get("unit_cost") not in (None, "")
                   else p["cost_price"])
        if q <= 0:
            continue
        total += q * uc
        prepared.append((pid, p, q, uc))
    if not prepared:
        conn.close()
        return {"error": "empty purchase"}, 400

    total = money2(total)
    paid = float(data.get("paid") if data.get("paid") not in (None, "") else total)
    paid = money2(max(0, min(paid, total)))   # never negative, never over total
    if payment == "credit":
        debt = money2(total - paid)
    else:
        debt = 0
        paid = total

    cur = conn.execute(
        """INSERT INTO purchases
           (ref, supplier, location, payment, total, paid, debt, note, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (ref, supplier, location, payment, total, paid, debt, note, ts))
    pur_id = cur.lastrowid

    for pid, p, q, uc in prepared:
        loc_add(conn, pid, location, q, ts)
        # update the product's cost price to the latest purchase cost
        conn.execute("UPDATE products SET cost_price=?, updated_at=? WHERE id=?",
                     (uc, ts, pid))
        conn.execute(
            """INSERT INTO movements
               (product_id, kind, qty, unit_cost, unit_sell, ref, note, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (pid, "in", q, uc, p["sell_price"], ref,
             f"Գնում / Purchase {supplier}".strip() + f" @{location}", ts))
        nm = (f'{p["brand"]} {p["name"]}').strip()
        conn.execute(
            """INSERT INTO purchase_items
               (purchase_id, product_id, name, qty, unit_cost, line)
               VALUES (?,?,?,?,?,?)""",
            (pur_id, pid, nm, q, uc, q * uc))

    # cash paid now leaves the cash box (transfers go through bank, not the box)
    if payment in ("cash", "credit") and paid > 0:
        cashbox_add(conn, "out", paid, f"Գնում / Purchase {supplier} {ref}".strip(),
                    ref, ts, location)

    conn.commit()
    conn.close()
    return {"ok": True, "id": pur_id, "ref": ref, "supplier": supplier,
            "location": location, "payment": payment, "total": total,
            "paid": paid, "debt": debt, "created_at": ts}


def list_purchases(limit=200):
    conn = get_db()
    rows = conn.execute("SELECT * FROM purchases ORDER BY id DESC LIMIT ?",
                        (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_payables():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM purchases WHERE debt > 0 ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def pay_supplier(data):
    pid = int(data["purchase_id"])
    amount = money2(max(0, float(data.get("amount") or 0)))
    conn = get_db()
    p = conn.execute("SELECT * FROM purchases WHERE id=?", (pid,)).fetchone()
    if not p:
        conn.close()
        return {"error": "not found"}, 404
    pay = money2(min(amount, p["debt"]))
    if pay <= 0:
        conn.close()
        return {"error": "nothing to pay"}, 400
    ts = now_iso()
    # guarded update: only pay if enough debt still remains (race-safe across terminals)
    new_debt = money2(p["debt"] - pay)
    if new_debt < 0.005:            # clear residual dust so debt never sticks at 0.004
        new_debt = 0
    cur = conn.execute(
        "UPDATE purchases SET paid=paid+?, debt=? WHERE id=? AND debt>=?",
        (pay, new_debt, pid, pay))
    if cur.rowcount == 0:           # another terminal already paid it down
        conn.close()
        return {"error": "debt changed, retry"}, 409
    cashbox_add(conn, "out", pay,
                f"Մատակարարի պարտք / Supplier {p['supplier']}".strip(), p["ref"], ts,
                p["location"])
    conn.commit()
    conn.close()
    return {"ok": True, "paid": pay}


# ----------------------------------------------------------------------------
# Returns / refunds (customer)
# ----------------------------------------------------------------------------
def make_return(data):
    items = data.get("items") or []
    if not items:
        return {"error": "empty return"}, 400
    location = (data.get("location") or WAREHOUSE).strip() or WAREHOUSE
    payment = data.get("payment", "cash")     # cash refund | credit (reduce debt)
    sale_ref = (data.get("sale_ref") or "").strip()
    note = data.get("note", "").strip()
    seller = (data.get("_seller") or "").strip()
    conn = get_db()
    ts = now_iso()
    ref = "R-" + datetime.now().strftime("%Y%m%d-%H%M%S")

    total = profit = 0
    prepared = []
    for it in items:
        pid = int(it["product_id"])
        p = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            conn.close()
            return {"error": f"product {pid} not found"}, 404
        q = int(it.get("qty") or 0)
        up = float(it.get("unit_price") if it.get("unit_price") not in (None, "")
                   else p["sell_price"])
        if q <= 0:
            continue
        total += q * up
        profit += q * (up - p["cost_price"])
        prepared.append((pid, p, q, up))
    if not prepared:
        conn.close()
        return {"error": "empty return"}, 400

    cur = conn.execute(
        """INSERT INTO returns
           (ref, sale_ref, location, payment, total, profit, seller, note, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (ref, sale_ref, location, payment, total, profit, seller, note, ts))
    ret_id = cur.lastrowid

    receipt = []
    for pid, p, q, up in prepared:
        loc_add(conn, pid, location, q, ts)   # stock comes back
        var = variant_str(p["attrs"])
        conn.execute(
            """INSERT INTO movements
               (product_id, kind, qty, unit_cost, unit_sell, ref, note, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (pid, "return", q, p["cost_price"], up, ref,
             f"Վերադարձ / Return @{location}", ts))
        nm = (f'{p["brand"]} {p["name"]}').strip()
        conn.execute(
            """INSERT INTO return_items
               (return_id, product_id, name, variant, qty, unit_price, unit_cost, line)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ret_id, pid, nm, var, q, up, p["cost_price"], q * up))
        receipt.append({"name": nm, "variant": var, "qty": q,
                        "unit_sell": up, "line": q * up})

    # cash refund leaves the box
    if payment == "cash" and total > 0:
        cashbox_add(conn, "out", total, f"Վերադարձ / Refund {ref}", ref, ts, location)

    conn.commit()
    conn.close()
    return {"ok": True, "id": ret_id, "ref": ref, "location": location,
            "payment": payment, "total": total, "items": receipt, "created_at": ts}


def list_returns(limit=200):
    conn = get_db()
    rows = conn.execute("SELECT * FROM returns ORDER BY id DESC LIMIT ?",
                        (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------------
# Cash box
# ----------------------------------------------------------------------------
def cashbox_add(conn, kind, amount, reason, ref, ts, location=""):
    conn.execute(
        "INSERT INTO cashbox(kind,amount,location,reason,ref,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (kind, float(amount or 0), location, reason, ref, ts))


def cashbox_balance(conn, location=None):
    if location:
        return conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN kind='in' THEN amount ELSE -amount END),0) b "
            "FROM cashbox WHERE location=?", (location,)).fetchone()["b"]
    return conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN kind='in' THEN amount ELSE -amount END),0) b "
        "FROM cashbox").fetchone()["b"]


def list_cashbox(limit=200):
    conn = get_db()
    rows = conn.execute("SELECT * FROM cashbox ORDER BY id DESC LIMIT ?",
                        (limit,)).fetchall()
    bal = cashbox_balance(conn)
    conn.close()
    return {"balance": bal, "rows": [dict(r) for r in rows]}


def cashbox_manual(data):
    kind = data.get("kind", "in")
    if kind not in ("in", "out"):
        return {"error": "bad kind"}, 400
    conn = get_db()
    cashbox_add(conn, kind, data.get("amount") or 0,
                data.get("reason", "").strip(), "", now_iso(),
                (data.get("location") or "").strip())
    conn.commit()
    conn.close()
    return {"ok": True}


def pay_debt(data):
    sid = int(data["sale_id"])
    amount = money2(max(0, float(data.get("amount") or 0)))
    conn = get_db()
    s = conn.execute("SELECT * FROM sales WHERE id=?", (sid,)).fetchone()
    if not s:
        conn.close()
        return {"error": "not found"}, 404
    pay = money2(min(amount, s["debt"]))
    if pay <= 0:
        conn.close()
        return {"error": "nothing to pay"}, 400
    ts = now_iso()
    new_debt = money2(s["debt"] - pay)
    if new_debt < 0.01:      # kill float dust so the debt truly closes
        new_debt = 0
    new_paid = money2(s["paid"] + pay)
    # guarded: only apply if the debt is still there (race-safe across terminals)
    cur = conn.execute("UPDATE sales SET paid=?, debt=? WHERE id=? AND debt>=?",
                       (new_paid, new_debt, sid, pay))
    if cur.rowcount == 0:
        conn.close()
        return {"error": "debt changed, retry"}, 409
    who = s["shop"] or s["customer"] or s["ref"]
    cashbox_add(conn, "in", pay, f"Պարտքի մարում / Debt {who}", s["ref"], ts,
                s["location"])
    conn.commit()
    conn.close()
    return {"ok": True, "paid": pay}


def finance():
    conn = get_db()
    s = conn.execute(
        "SELECT COALESCE(SUM(total),0) revenue, COALESCE(SUM(profit),0) gross, "
        "COALESCE(SUM(debt),0) debt FROM sales").fetchone()
    cogs = conn.execute(
        "SELECT COALESCE(SUM(qty*unit_cost),0) c FROM sale_items").fetchone()["c"]
    exp = conn.execute(
        "SELECT COALESCE(SUM(amount),0) e FROM cashbox WHERE kind='out' AND ref=''"
    ).fetchone()["e"]
    ret = conn.execute(
        "SELECT COALESCE(SUM(total),0) t, COALESCE(SUM(profit),0) p FROM returns"
    ).fetchone()
    payable = conn.execute(
        "SELECT COALESCE(SUM(debt),0) d FROM purchases").fetchone()["d"]
    bal = cashbox_balance(conn)
    conn.close()
    net = s["gross"] - exp - ret["p"]
    return {"revenue": round(s["revenue"] - ret["t"], 2),
            "gross_profit": round(s["gross"] - ret["p"], 2),
            "cogs": cogs, "expenses": exp, "returns": ret["t"],
            "net": round(net, 2), "cash_balance": bal,
            "total_debt": s["debt"], "payable": payable}


# ----------------------------------------------------------------------------
# Transfers between locations
# ----------------------------------------------------------------------------
def make_transfer(data):
    pid = int(data["product_id"])
    frm = (data.get("from_loc") or WAREHOUSE).strip()
    to = (data.get("to_loc") or "").strip()
    qty = int(data.get("qty") or 0)
    note = data.get("note", "").strip()
    if qty <= 0 or not to or frm == to:
        return {"error": "bad transfer"}, 400
    conn = get_db()
    ts = now_iso()
    if not loc_take(conn, pid, frm, qty, ts):   # atomic source decrement
        avail = loc_get(conn, pid, frm)
        conn.close()
        return {"error": "not enough stock", "available": avail}, 400
    loc_add(conn, pid, to, qty, ts)
    conn.execute(
        "INSERT INTO transfers(product_id,from_loc,to_loc,qty,note,created_at) "
        "VALUES(?,?,?,?,?,?)", (pid, frm, to, qty, note, ts))
    conn.execute(
        """INSERT INTO movements
           (product_id, kind, qty, unit_cost, unit_sell, ref, note, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (pid, "transfer", qty, 0, 0, "", f"{frm} → {to}", ts))
    conn.commit()
    conn.close()
    return {"ok": True}


def list_transfers(limit=200):
    conn = get_db()
    rows = conn.execute(
        """SELECT tr.*, p.brand, p.name, p.attrs
           FROM transfers tr JOIN products p ON p.id=tr.product_id
           ORDER BY tr.id DESC LIMIT ?""", (limit,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r); d["variant"] = variant_str(d.pop("attrs", "{}")); out.append(d)
    return out


def loc_stock():
    """Per-shop balances (everything outside the main warehouse)."""
    conn = get_db()
    rows = conn.execute(
        """SELECT ls.location, ls.qty, p.brand, p.name, p.attrs
           FROM loc_stock ls JOIN products p ON p.id=ls.product_id
           WHERE ls.qty > 0 ORDER BY ls.location, p.brand, p.name""").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r); d["variant"] = variant_str(d.pop("attrs", "{}")); out.append(d)
    return out


# ----------------------------------------------------------------------------
# Reports
# ----------------------------------------------------------------------------
def period_report(frm, to):
    today = datetime.now().date()
    frm = frm or (today - timedelta(days=29)).isoformat()
    to = to or today.isoformat()
    conn = get_db()
    cond = "substr(s.created_at,1,10) >= ? AND substr(s.created_at,1,10) <= ?"
    a = (frm, to)
    srow = conn.execute(
        f"""SELECT COALESCE(SUM(total),0) revenue, COALESCE(SUM(profit),0) profit,
              COALESCE(SUM(debt),0) debt, COUNT(*) sales,
              COALESCE(SUM(CASE WHEN payment='cash'     THEN total ELSE 0 END),0) cash,
              COALESCE(SUM(CASE WHEN payment='card'     THEN total ELSE 0 END),0) card,
              COALESCE(SUM(CASE WHEN payment='credit'   THEN total ELSE 0 END),0) credit,
              COALESCE(SUM(CASE WHEN payment='transfer' THEN total ELSE 0 END),0) transfer
            FROM sales s WHERE {cond}""", a).fetchone()
    urow = conn.execute(
        f"""SELECT COALESCE(SUM(si.qty),0) units, COALESCE(SUM(si.qty*si.unit_cost),0) cogs
            FROM sale_items si JOIN sales s ON s.id=si.sale_id WHERE {cond}""", a).fetchone()
    rret = conn.execute(
        "SELECT COALESCE(SUM(total),0) t, COALESCE(SUM(profit),0) p FROM returns "
        "WHERE substr(created_at,1,10) >= ? AND substr(created_at,1,10) <= ?", a).fetchone()
    summary = {**dict(srow), "units": urow["units"], "cogs": urow["cogs"],
               "returns": money2(rret["t"])}
    summary["revenue"] = money2(summary["revenue"] - rret["t"])  # net of refunds
    summary["profit"] = money2(summary["profit"] - rret["p"])
    by_product = [dict(r) for r in conn.execute(
        f"""SELECT si.name, si.variant, SUM(si.qty) units,
              SUM(si.qty*si.unit_cost) cost_total, SUM(si.line) sell_total,
              SUM(si.line - si.qty*si.unit_cost) profit
            FROM sale_items si JOIN sales s ON s.id=si.sale_id WHERE {cond}
            GROUP BY si.product_id ORDER BY units DESC""", a).fetchall()]
    by_shop = [dict(r) for r in conn.execute(
        f"""SELECT s.shop, COUNT(DISTINCT s.id) sales, SUM(si.qty) units,
              SUM(si.line) revenue, SUM(si.line - si.qty*si.unit_cost) profit
            FROM sales s JOIN sale_items si ON si.sale_id=s.id
            WHERE {cond} AND s.shop<>'' GROUP BY s.shop ORDER BY revenue DESC""", a).fetchall()]
    by_courier = [dict(r) for r in conn.execute(
        f"""SELECT s.courier, COUNT(DISTINCT s.id) deliveries, SUM(si.qty) units,
              SUM(si.line) revenue
            FROM sales s JOIN sale_items si ON si.sale_id=s.id
            WHERE {cond} AND s.courier<>'' GROUP BY s.courier ORDER BY revenue DESC""", a).fetchall()]
    by_seller = [dict(r) for r in conn.execute(
        f"""SELECT s.seller, COUNT(DISTINCT s.id) sales, SUM(si.qty) units,
              SUM(si.line) revenue, SUM(si.line - si.qty*si.unit_cost) profit
            FROM sales s JOIN sale_items si ON si.sale_id=s.id
            WHERE {cond} AND s.seller<>'' GROUP BY s.seller ORDER BY revenue DESC""", a).fetchall()]
    conn.close()

    def hilo(lst, key):
        if not lst:
            return {"top": None, "low": None}
        s = sorted(lst, key=lambda x: x[key])
        return {"top": s[-1], "low": s[0]}

    stats = {"product": hilo(by_product, "units"),
             "courier": hilo(by_courier, "units"),
             "shop": hilo(by_shop, "units"),
             "seller": hilo(by_seller, "units")}
    return {"from": frm, "to": to, "summary": summary, "by_product": by_product,
            "by_shop": by_shop, "by_courier": by_courier, "by_seller": by_seller,
            "stats": stats}


def list_movements(limit=300):
    conn = get_db()
    rows = conn.execute(
        """SELECT m.*, p.barcode, p.sku, p.brand, p.name, p.attrs
           FROM movements m JOIN products p ON p.id = m.product_id
           ORDER BY m.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r); d["variant"] = variant_str(d.pop("attrs", "{}")); out.append(d)
    return out


def stats():
    conn = get_db()
    p = conn.execute(
        """SELECT COUNT(*) AS skus, COALESCE(SUM(quantity),0) AS units,
             COALESCE(SUM(quantity*cost_price),0) AS stock_cost,
             COALESCE(SUM(quantity*sell_price),0) AS stock_retail,
             COALESCE(SUM(CASE WHEN quantity<=min_qty THEN 1 ELSE 0 END),0) AS low_count
           FROM products"""
    ).fetchone()
    s = conn.execute(
        """SELECT
             (SELECT COALESCE(SUM(qty),0) FROM sale_items) AS sold_units,
             COALESCE(SUM(total),0)  AS revenue,
             COALESCE(SUM(profit),0) AS profit,
             COALESCE(SUM(debt),0)   AS total_debt
           FROM sales"""
    ).fetchone()
    payable = conn.execute("SELECT COALESCE(SUM(debt),0) d FROM purchases").fetchone()["d"]
    ret = conn.execute("SELECT COALESCE(SUM(total),0) t, COALESCE(SUM(profit),0) p "
                       "FROM returns").fetchone()
    bal = cashbox_balance(conn)
    conn.close()
    out = {**dict(p), **dict(s), "payable": payable, "cash_balance": bal}
    out["revenue"] = money2(out["revenue"] - ret["t"])   # net of refunds, like finance()
    out["profit"] = money2(out["profit"] - ret["p"])
    return out


def reports(days=14):
    conn = get_db()
    today = datetime.now().date()
    start = today - timedelta(days=days - 1)
    money_rows = conn.execute(
        """SELECT substr(created_at,1,10) AS d,
             COALESCE(SUM(total),0)  AS revenue,
             COALESCE(SUM(profit),0) AS profit
           FROM sales WHERE substr(created_at,1,10) >= ? GROUP BY d""",
        (start.isoformat(),),
    ).fetchall()
    unit_rows = conn.execute(
        """SELECT substr(s.created_at,1,10) AS d, COALESCE(SUM(si.qty),0) AS units
           FROM sales s JOIN sale_items si ON si.sale_id=s.id
           WHERE substr(s.created_at,1,10) >= ? GROUP BY d""",
        (start.isoformat(),),
    ).fetchall()
    by_money = {r["d"]: r for r in money_rows}
    by_units = {r["d"]: r["units"] for r in unit_rows}
    daily = []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        r = by_money.get(d)
        daily.append({"date": d, "units": by_units.get(d, 0),
                      "revenue": r["revenue"] if r else 0,
                      "profit": r["profit"] if r else 0})
    top = conn.execute(
        """SELECT name, variant,
             SUM(qty) AS units, SUM(line) AS revenue
           FROM sale_items GROUP BY product_id ORDER BY units DESC LIMIT 10"""
    ).fetchall()
    conn.close()
    return {"daily": daily, "top": [dict(r) for r in top]}


# ----------------------------------------------------------------------------
# Settings & master data
# ----------------------------------------------------------------------------
def get_settings():
    conn = get_db()
    rows = conn.execute("SELECT key,value FROM settings").fetchall()
    conn.close()
    out = {r["key"]: r["value"] for r in rows}
    try:
        out["attrs_def"] = json.loads(out.get("attrs_def", "[]"))
    except Exception:
        out["attrs_def"] = []
    out["app_name"] = APP_NAME
    out["lan_url"] = LAN_URL
    return out


def save_settings(data):
    if "logo" in data:
        logo = _clean_photo(data.get("logo"))
        if logo is None:
            return {"error": "logo too big"}, 400
        data = dict(data, logo=logo)   # already cleaned; skip re-clean below
    conn = get_db()
    for k, v in data.items():
        if k == "attrs_def":
            v = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
    conn.commit()
    conn.close()
    return {"ok": True}


def ref_list(entity):
    if entity not in REF_ENTITIES:
        return None
    conn = get_db()
    rows = conn.execute(f"SELECT * FROM ref_{entity} ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def ref_create(entity, data):
    if entity not in REF_ENTITIES:
        return {"error": "bad entity"}, 400
    cols = REF_ENTITIES[entity]
    vals = [str(data.get(c, "")).strip() for c in cols]
    conn = get_db()
    placeholders = ",".join("?" for _ in cols)
    cur = conn.execute(
        f"INSERT INTO ref_{entity} ({','.join(cols)},created_at) "
        f"VALUES ({placeholders},?)",
        (*vals, now_iso()),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return {"id": rid}


def ref_delete(entity, rid):
    if entity not in REF_ENTITIES:
        return {"error": "bad entity"}, 400
    conn = get_db()
    conn.execute(f"DELETE FROM ref_{entity} WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ----------------------------------------------------------------------------
# CSV export / import
# ----------------------------------------------------------------------------
def db_backup_bytes():
    """Return a consistent snapshot of the database using SQLite's online backup
    API. Reading the raw .db file while WAL pages are uncommitted can hand back a
    torn/corrupt copy — backup() serializes a clean, restorable image instead."""
    src = get_db()
    fd, tmp = tempfile.mkstemp(suffix=".db", dir=DATA_DIR)
    os.close(fd)
    try:
        dst = sqlite3.connect(tmp)
        with dst:
            src.backup(dst)
        dst.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        src.close()
        try:
            os.remove(tmp)
        except OSError:
            pass


def export_csv():
    rows = list_products()
    adef = attrs_def()
    akeys = [a.get("key") for a in adef]
    aheads = [a.get("hy") or a.get("key") for a in adef]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "barcode", "sku", "name", "category", "brand", "supplier",
                *aheads, "unit", "cost_price", "sell_price", "wholesale_price",
                "quantity", "min_qty"])
    for r in rows:
        at = r.get("attrs") or {}
        w.writerow([r["id"], r["barcode"], r["sku"], r["name"], r["category"],
                    r["brand"], r["supplier"], *[at.get(k, "") for k in akeys],
                    r["unit"], r["cost_price"], r["sell_price"],
                    r["wholesale_price"], r["quantity"], r["min_qty"]])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def import_csv(text):
    """Bulk-create products from CSV text. Header row required.
    Recognized headers (any case): name, barcode, sku, category, brand,
    supplier, unit, cost_price/cost, sell_price/price, wholesale/wholesale_price,
    quantity/qty, min_qty/min. Any other column becomes a product attribute."""
    if not text or not text.strip():
        return {"error": "empty"}, 400
    # tolerate a UTF-8 BOM
    text = text.lstrip("﻿")
    try:
        sample = text[:2048]
        delim = ";" if sample.count(";") > sample.count(",") else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    except Exception as e:
        return {"error": f"bad csv: {e}"}, 400

    alias = {
        "name": "name", "անուն": "name",
        "barcode": "barcode", "շտրիխ": "barcode", "շտրիխկոդ": "barcode",
        "sku": "sku", "կոդ": "sku", "արտիկուլ": "sku",
        "category": "category", "կատեգորիա": "category",
        "brand": "brand", "բրենդ": "brand",
        "supplier": "supplier", "մատակարար": "supplier",
        "unit": "unit", "միավոր": "unit",
        "cost": "cost_price", "cost_price": "cost_price", "ինքնարժեք": "cost_price",
        "price": "sell_price", "sell": "sell_price", "sell_price": "sell_price", "գին": "sell_price",
        "wholesale": "wholesale_price", "wholesale_price": "wholesale_price", "մեծածախ": "wholesale_price",
        "qty": "quantity", "quantity": "quantity", "քանակ": "quantity",
        "min": "min_qty", "min_qty": "min_qty", "նվազ": "min_qty",
    }
    core = {"name", "barcode", "sku", "category", "brand", "supplier", "unit",
            "cost_price", "sell_price", "wholesale_price", "quantity", "min_qty"}
    n = 0
    errors = []
    for i, raw in enumerate(reader, 2):
        rec = {}
        attrs = {}
        for col, val in raw.items():
            if col is None:
                continue
            key = alias.get(col.strip().lower())
            v = (val or "").strip()
            if key:
                rec[key] = v
            elif col.strip():
                if v:
                    attrs[col.strip()] = v
        if not (rec.get("name") or rec.get("barcode")):
            continue
        rec["attrs"] = attrs
        try:
            create_product(rec)
            n += 1
        except Exception as e:
            errors.append(f"line {i}: {e}")
    return {"ok": True, "imported": n, "errors": errors[:10]}


# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _raw_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return ""
        try:
            return self.rfile.read(length).decode("utf-8")
        except Exception:
            return ""

    def _current(self):
        token = None
        for part in self.headers.get("Cookie", "").split(";"):
            part = part.strip()
            if part.startswith("sid="):
                token = part[4:]
        username = SESSIONS.get(token)
        return user_by_name(username) if username else None

    def _authorized(self, method, path, role):
        if (method, path) in (("GET", "/"), ("GET", "/index.html"),
                              ("POST", "/api/login"), ("GET", "/api/me")):
            return True
        if role is None:
            return False
        if role == "admin":
            return True
        # seller: sell-only + read what POS needs
        if method == "GET" and path in (
                "/api/products", "/api/product_by_barcode", "/api/settings",
                "/api/ref", "/api/locations"):
            return True
        if method == "POST" and path in ("/api/sale", "/api/logout", "/api/me/password"):
            return True
        return False

    def _gate(self):
        user = self._current()
        role = user["role"] if user else None
        path = urlparse(self.path).path
        if not self._authorized(self.command, path, role):
            self._json({"error": "unauthorized"}, 401 if role is None else 403)
            return None
        return user or {"role": None}

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path in ("/", "/index.html"):
            return self._serve_index()
        user = self._gate()
        if user is None:
            return
        if path == "/api/me":
            cur = self._current()
            return self._json(cur if cur else {"error": "guest"},
                              200 if cur else 401)
        if path == "/api/users":
            return self._json(list_users())
        if path == "/api/products":
            rows = list_products(q.get("search", [""])[0])
            if user.get("role") == "seller":
                for r in rows:
                    r.pop("cost_price", None); r.pop("wholesale_price", None)
                    r.pop("stock_cost", None); r.pop("stock_retail", None)
            return self._json(rows)
        if path == "/api/product":
            r = get_product(int(q.get("id", ["0"])[0]))
            return self._json(r or {"error": "not found"}, 200 if r else 404)
        if path == "/api/product_by_barcode":
            r = get_by_barcode(q.get("code", [""])[0])
            if r and user.get("role") == "seller":   # never leak cost to sellers
                for k in ("cost_price", "wholesale_price", "stock_cost", "stock_retail"):
                    r.pop(k, None)
            return self._json(r or {"error": "not found"}, 200 if r else 404)
        if path == "/api/locations":
            return self._json(locations())
        if path == "/api/movements":
            return self._json(list_movements())
        if path == "/api/sales":
            return self._json(list_sales())
        if path == "/api/sale_detail":
            r = sale_detail(int(q.get("id", ["0"])[0]))
            return self._json(r or {"error": "not found"}, 200 if r else 404)
        if path == "/api/debts":
            return self._json(list_debts())
        if path == "/api/purchases":
            return self._json(list_purchases())
        if path == "/api/payables":
            return self._json(list_payables())
        if path == "/api/returns":
            return self._json(list_returns())
        if path == "/api/cashbox":
            return self._json(list_cashbox())
        if path == "/api/finance":
            return self._json(finance())
        if path == "/api/transfers":
            return self._json(list_transfers())
        if path == "/api/locstock":
            return self._json(loc_stock())
        if path == "/api/period":
            return self._json(period_report(q.get("from", [""])[0],
                                            q.get("to", [""])[0]))
        if path == "/api/stats":
            return self._json(stats())
        if path == "/api/reports":
            return self._json(reports(int(q.get("days", ["14"])[0])))
        if path == "/api/settings":
            return self._json(get_settings())
        if path == "/api/ref":
            r = ref_list(q.get("entity", [""])[0])
            return self._json(r if r is not None else {"error": "bad entity"},
                              200 if r is not None else 400)
        if path == "/api/backup":
            try:
                data = db_backup_bytes()
            except Exception:
                data = b""
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition",
                             f'attachment; filename="menq-backup-{stamp}.db"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/export":
            data = export_csv()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="menq_export.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        user = self._gate()
        if user is None:
            return
        if path == "/api/import":
            res = import_csv(self._raw_body())
            return self._json(*res) if isinstance(res, tuple) else self._json(res)
        if path == "/api/shutdown":     # admin-only clean quit (gate already checked)
            self._json({"ok": True})
            if SRV is not None:
                threading.Thread(target=SRV.shutdown, daemon=True).start()
            return
        body = self._body()
        if path == "/api/me/password":
            res = update_own_profile(user.get("username"), body)
            return self._json(*res) if isinstance(res, tuple) else self._json(res)
        if path in ("/api/sale", "/api/return"):
            body["_seller"] = user.get("name") or user.get("username") or ""
            body["_role"] = user.get("role") or ""   # so make_sale can deny seller price overrides
        if path == "/api/login":
            user = authenticate(body.get("username", ""), body.get("password", ""))
            if not user:
                return self._json({"error": "bad credentials"}, 401)
            token = secrets.token_hex(16)
            SESSIONS[token] = user["username"]
            data = json.dumps(user, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", f"sid={token}; Path=/; HttpOnly; SameSite=Lax")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/logout":
            for part in self.headers.get("Cookie", "").split(";"):
                part = part.strip()
                if part.startswith("sid="):
                    SESSIONS.pop(part[4:], None)
            return self._json({"ok": True})
        if path == "/api/users":
            res = create_user(body)
            return self._json(*res) if isinstance(res, tuple) else self._json(res)
        if path == "/api/products":
            res = create_product(body)
            return self._json(*res) if isinstance(res, tuple) else self._json(res, 201)
        if path == "/api/movements":
            res = add_movement(body)
            return self._json(*res) if isinstance(res, tuple) else self._json(res)
        if path == "/api/sale":
            res = make_sale(body)
            return self._json(*res) if isinstance(res, tuple) else self._json(res)
        if path == "/api/purchase":
            res = make_purchase(body)
            return self._json(*res) if isinstance(res, tuple) else self._json(res)
        if path == "/api/purchase/pay":
            res = pay_supplier(body)
            return self._json(*res) if isinstance(res, tuple) else self._json(res)
        if path == "/api/return":
            res = make_return(body)
            return self._json(*res) if isinstance(res, tuple) else self._json(res)
        if path == "/api/settings":
            res = save_settings(body)
            return self._json(*res) if isinstance(res, tuple) else self._json(res)
        if path == "/api/cashbox":
            res = cashbox_manual(body)
            return self._json(*res) if isinstance(res, tuple) else self._json(res)
        if path == "/api/debts/pay":
            res = pay_debt(body)
            return self._json(*res) if isinstance(res, tuple) else self._json(res)
        if path == "/api/transfer":
            res = make_transfer(body)
            return self._json(*res) if isinstance(res, tuple) else self._json(res)
        if path == "/api/ref":
            entity = parse_qs(urlparse(self.path).query).get("entity", [""])[0]
            res = ref_create(entity, body)
            if isinstance(res, tuple):
                return self._json(*res)
            return self._json(res, 201)
        return self._json({"error": "not found"}, 404)

    def do_PUT(self):
        path = urlparse(self.path).path
        if self._gate() is None:
            return
        if path.startswith("/api/users/"):
            res = update_user(int(path.rsplit("/", 1)[1]), self._body())
            return self._json(*res) if isinstance(res, tuple) else self._json(res)
        if path.startswith("/api/products/"):
            res = update_product(int(path.rsplit("/", 1)[1]), self._body())
            return self._json(*res) if isinstance(res, tuple) else self._json(res)
        return self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if self._gate() is None:
            return
        if path.startswith("/api/users/"):
            res = delete_user(int(path.rsplit("/", 1)[1]))
            return self._json(*res) if isinstance(res, tuple) else self._json(res)
        if path.startswith("/api/products/"):
            return self._json(delete_product(int(path.rsplit("/", 1)[1])))
        if path.startswith("/api/ref/"):
            parts = path.split("/")  # ['', 'api', 'ref', entity, id]
            res = ref_delete(parts[3], int(parts[4]))
            return self._json(*res) if isinstance(res, tuple) else self._json(res)
        return self._json({"error": "not found"}, 404)

    def _serve_index(self):
        try:
            with open(INDEX_PATH, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return self._json({"error": "index.html missing"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def find_chrome():
    cands = [
        os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                     r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"Google\Chrome\Application\chrome.exe"),
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def open_browser(url):
    chrome = find_chrome()
    try:
        if chrome:
            subprocess.Popen([chrome, f"--app={url}", "--window-size=1320,860"])
            return
    except Exception:
        pass
    webbrowser.open(url)


class QuietServer(ThreadingHTTPServer):
    # False so a 2nd instance fails to bind instead of silently stealing the port
    allow_reuse_address = False
    daemon_threads = True


_MUTEX_HANDLE = None    # kept alive for the process lifetime so the mutex isn't released


def _single_instance():
    """On Windows return False if another MenQ is already running (named mutex)."""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        import ctypes
        ERROR_ALREADY_EXISTS = 183
        # use_last_error makes GetLastError reliable across ctypes' own calls
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _MUTEX_HANDLE = k32.CreateMutexW(None, False, "MenQ_SingleInstance_Mutex")
        return ctypes.get_last_error() != ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _primary_ip():
    """Best-effort primary LAN IP (the adapter that routes outbound), not 127.0.0.1.
    Uses a UDP socket that never actually sends a packet."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return HOST
    finally:
        s.close()


def _default_admin_unchanged():
    return authenticate("admin", "admin") is not None


def _msgbox(text, title="MenQ"):
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)
        except Exception:
            pass


def main():
    global SRV, LAN_URL

    # single instance FIRST: if MenQ is already open, just show it and exit —
    # so a second launch never races init_db against the running instance
    if not _single_instance():
        if not os.environ.get("MENQ_NOBROWSER"):
            open_browser(f"http://{HOST}:{PORT}")
        return

    init_db()

    lan = len(sys.argv) > 1 and sys.argv[1].lower() == "lan"
    # never expose to the network while the default admin password still stands
    if lan and _default_admin_unchanged():
        msg = ("LAN mode blocked: the admin password is still the default (admin).\n"
               "Change it in Settings -> Users, then relaunch in LAN mode.\n\n"
               "Ցանցային ռեժիմն արգելափակված է՝ admin-ի գաղտնաբառը դեռ default է։\n"
               "Փոխիր Կարգավորումներ -> Օգտատերեր, հետո նորից բացիր LAN-ով։")
        print(msg); _msgbox(msg, "MenQ - LAN blocked")
        lan = False
    host = "0.0.0.0" if lan else HOST

    try:
        srv = QuietServer((host, PORT), Handler)
    except OSError:
        # we already passed the single-instance mutex, so another MenQ is NOT the
        # holder — some other program is sitting on the port. Tell the user plainly.
        msg = (f"Port {PORT} is in use by another program, so MenQ can't start.\n"
               f"Close whatever is using port {PORT} and try again.\n\n"
               f"{PORT} պորտը զբաղված է այլ ծրագրով, MenQ-ն չի կարող մեկնարկել։\n"
               f"Փակիր այդ ծրագիրը և նորից փորձիր։")
        print(msg); _msgbox(msg, "MenQ")
        if not FROZEN:
            input("\nPress Enter to close...")
        return
    SRV = srv
    url = f"http://{HOST}:{PORT}"
    print(f"{APP_NAME} · Պահեստ / Warehouse  →  {url}")
    if lan:
        LAN_URL = f"http://{_primary_ip()}:{PORT}"
        print(f"On the network  →  {LAN_URL}")
    print("Ctrl+C to stop")
    if not os.environ.get("MENQ_NOBROWSER"):
        threading.Timer(0.8, lambda: open_browser(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        err = traceback.format_exc()
        try:
            with open(os.path.join(DATA_DIR, "menq.log"), "a", encoding="utf-8") as f:
                f.write("\n=== " + now_iso() + " ===\n" + err + "\n")
        except Exception:
            pass
        _msgbox("MenQ could not start:\n\n" + err.strip().splitlines()[-1] +
                "\n\nDetails: " + os.path.join(DATA_DIR, "menq.log"), "MenQ - Error")
        raise
