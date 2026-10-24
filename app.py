import hashlib
import hmac
import io
import logging
import os
import secrets
import sqlite3
import re
import time
import unicodedata
import uuid
import zlib
from collections import Counter
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, send_from_directory, flash, abort, jsonify, g, Response
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from PIL import Image as PILImage, ImageChops, ImageOps
from PIL.ExifTags import TAGS

from backup import start_scheduler as start_backup_scheduler
from storage import storage_from_env

# ---------------------------------------------------------------------------
# Configuration
#
# DATA_DIR holds the database file (and, when NOT using Cloudflare R2, the
# photos and thumbnails too). Locally it defaults to a "data" folder next to
# this file. In production it points at a small persistent disk (README.md).
#
# Run locally with GALLERY_DEV=1 to skip the production safety checks.
# ---------------------------------------------------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
THUMB_FOLDER = os.path.join(DATA_DIR, "thumbnails")
TMP_FOLDER = os.path.join(DATA_DIR, "tmp")   # uploads are staged here first
DB_PATH = os.path.join(DATA_DIR, "gallery.db")

IS_DEV = os.environ.get("GALLERY_DEV") == "1"

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
THUMB_SIZE = (500, 500)
PER_PAGE = 24

# Photos are shrunk before they go to R2 (storage is the main running cost).
# Only photos whose longest side is above MAX_IMAGE_PX are downscaled and
# re-encoded (4K is plenty for zooming on a screen); smaller photos are only
# packed losslessly and keep every pixel. See optimize_image().
MAX_IMAGE_PX = int(os.environ.get("MAX_IMAGE_PX", "3840"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "92"))
# 4:2:0 is what cameras use and looks identical on photos. "4:4:4" keeps the
# finest colour edges but makes files roughly 30-50% bigger.
JPEG_SUBSAMPLING = {"4:4:4": 0, "4:2:2": 1, "4:2:0": 2}.get(
    os.environ.get("JPEG_SUBSAMPLING", "4:2:0"), 2)

# --- Security settings ------------------------------------------------------
LOGIN_MAX_FAILS = 5            # wrong passwords allowed ...
LOGIN_WINDOW_SECONDS = 15 * 60  # ... per IP address within this window
COMMENT_MAX = 5                # comments allowed ...
COMMENT_WINDOW_SECONDS = 10 * 60  # ... per IP address within this window

# Set TRUST_CLOUDFLARE=1 when the site sits behind Cloudflare, so the real
# visitor IP is read from the CF-Connecting-IP header (needed for the rate
# limits to work per visitor instead of per proxy).
TRUST_CLOUDFLARE = os.environ.get("TRUST_CLOUDFLARE") == "1"

# Optional: a secret that Cloudflare adds to every request (see OPSAETNING.md).
# When set, requests without it are refused, so nobody can bypass Cloudflare's
# protection by calling the server directly.
ORIGIN_SECRET = os.environ.get("ORIGIN_SECRET", "").strip()


def _load_secrets():
    secret_key = os.environ.get("SECRET_KEY", "").strip()
    password_hash = os.environ.get("ADMIN_PASSWORD_HASH", "").strip()

    if IS_DEV:
        if not secret_key:
            secret_key = secrets.token_hex(32)  # sessions reset on restart
        if not password_hash:
            plain = os.environ.get("UPLOAD_PASSWORD", "sgb73gzw")
            print(f"[dev] No ADMIN_PASSWORD_HASH set - using password '{plain}'")
            password_hash = generate_password_hash(plain, method="pbkdf2")
        return secret_key, password_hash

    problems = []
    if len(secret_key) < 32 or secret_key == "change-this-secret-key":
        problems.append("SECRET_KEY must be set to a long random string (32+ characters)")
    if not password_hash:
        problems.append("ADMIN_PASSWORD_HASH must be set (create it with tools/make_password_hash.py)")
    if problems:
        raise RuntimeError(
            "Refusing to start without secure settings:\n  - " + "\n  - ".join(problems)
            + "\n(For local testing only, start with GALLERY_DEV=1.)"
        )
    return secret_key, password_hash


SECRET_KEY, ADMIN_PASSWORD_HASH = _load_secrets()

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", "50")) * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not IS_DEV,
    PERMANENT_SESSION_LIFETIME=7 * 24 * 3600,  # stay logged in for a week
    SEND_FILE_MAX_AGE_DEFAULT=365 * 24 * 3600,  # file names are unique & immutable
)

# Public address of the site, e.g. https://galleri.example.dk (no trailing
# slash). Used for canonical links, the sitemap and robots.txt, which must
# contain full https:// addresses. If empty, the address of the current request
# is used - but behind a proxy that can come out as http://, so set it in production.
SITE_URL = os.environ.get("SITE_URL", "").strip().rstrip("/")

# Optional contact email. If left empty, the "Contact us" link is hidden.
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "").strip()

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TMP_FOLDER, exist_ok=True)

# Where the photo files live: local folders, or Cloudflare R2 (see storage.py).
app.logger.setLevel(logging.INFO)
storage = storage_from_env(UPLOAD_FOLDER, THUMB_FOLDER)

# Fixed vehicle categories shown as tiles on the homepage.
CATEGORIES = [
    {"slug": "car", "label": "Cars", "emoji": "\U0001F697"},
    {"slug": "bus", "label": "Buses", "emoji": "\U0001F68C"},
    {"slug": "truck", "label": "Trucks", "emoji": "\U0001F69B"},
    {"slug": "tram", "label": "Trams", "emoji": "\U0001F68B"},
    {"slug": "train", "label": "Trains", "emoji": "\U0001F686"},
    {"slug": "misc", "label": "Misc", "emoji": "\U0001F5C2\uFE0F"},
]
CATEGORY_MAP = {c["slug"]: c for c in CATEGORIES}


# ---------------------------------------------------------------------------
# Database helpers (plain sqlite3 - no ORM needed for something this small)
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets visitors read while someone writes (e.g. the visit counter or
    # an upload), which matters with several gunicorn workers.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def column_exists(conn, table, column):
    cols = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    return column in cols


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '',
            filename TEXT NOT NULL,
            thumbnail TEXT NOT NULL,
            uploaded_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            comment TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS site_stats (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("INSERT OR IGNORE INTO site_stats (key, value) VALUES ('total_visits', 0)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tag_cache (
            category TEXT PRIMARY KEY,
            n_images INTEGER NOT NULL,
            tags TEXT NOT NULL
        )
    """)
    # Remembers failed logins / posted comments per IP for the rate limits.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rate_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            ip TEXT NOT NULL,
            ts REAL NOT NULL
        )
    """)

    # Migrations: add columns that didn't exist in earlier versions of this
    # app. Each check runs every startup and is a no-op once applied, so
    # upgrading an existing site never loses data.
    if not column_exists(conn, "images", "album_id"):
        conn.execute("ALTER TABLE images ADD COLUMN album_id INTEGER REFERENCES albums(id)")
    if not column_exists(conn, "images", "category"):
        conn.execute("ALTER TABLE images ADD COLUMN category TEXT NOT NULL DEFAULT 'misc'")
    if not column_exists(conn, "images", "caption"):
        conn.execute("ALTER TABLE images ADD COLUMN caption TEXT NOT NULL DEFAULT ''")
    if not column_exists(conn, "images", "photo_taken_date"):
        conn.execute("ALTER TABLE images ADD COLUMN photo_taken_date TEXT")
    if not column_exists(conn, "images", "views"):
        conn.execute("ALTER TABLE images ADD COLUMN views INTEGER NOT NULL DEFAULT 0")

    # A photo can belong to several albums. image_albums is the source of
    # truth; images.album_id stays as the "main album" (first one), which keeps
    # the upload form and older data working. The backfill only runs when the
    # table is first created, so later edits are never overwritten.
    had_image_albums = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'image_albums'"
    ).fetchone() is not None
    conn.execute("""
        CREATE TABLE IF NOT EXISTS image_albums (
            image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
            album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
            PRIMARY KEY (image_id, album_id)
        )
    """)
    if not had_image_albums:
        conn.execute(
            "INSERT OR IGNORE INTO image_albums (image_id, album_id) "
            "SELECT id, album_id FROM images WHERE album_id IS NOT NULL"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_image_albums_album ON image_albums(album_id, image_id)")

    # Indexes keep category pages, album pages, prev/next and comment lookups
    # fast when the collection grows to tens of thousands of photos.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_images_category_id ON images(category, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_images_album ON images(album_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_image ON comments(image_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_events ON rate_events(kind, ip, ts)")

    setup_search_index(conn)

    conn.commit()
    conn.close()


# Fast word search (SQLite FTS5). Each photo's title, tags, caption and album
# names are kept in `images_fts`; database triggers keep it up to date whenever
# a photo or album changes, by this app or by any tool. A search term matches
# words that START with it ("volv" finds "Volvo"), which is also what
# type-ahead suggestions need later. If this SQLite has no FTS5, search falls
# back to the old (slower) LIKE search.
FTS_OK = False


def setup_search_index(conn):
    global FTS_OK
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS images_fts USING fts5(
                title, tags, caption, albums,
                tokenize = 'unicode61 remove_diacritics 2', prefix = '2 3 4')
        """)
    except sqlite3.OperationalError:
        FTS_OK = False
        return
    albums_of = ("(SELECT COALESCE(group_concat(a.name, ' '), '') FROM image_albums ia "
                 "JOIN albums a ON a.id = ia.album_id WHERE ia.image_id = {id})")
    triggers = {
        "images_fts_ai": f"""AFTER INSERT ON images BEGIN
            INSERT INTO images_fts(rowid, title, tags, caption, albums)
            VALUES (new.id, new.title, new.tags, new.caption, {albums_of.format(id="new.id")}); END""",
        "images_fts_au": f"""AFTER UPDATE OF title, tags, caption ON images BEGIN
            UPDATE images_fts SET title = new.title, tags = new.tags, caption = new.caption
            WHERE rowid = new.id; END""",
        "images_fts_ad": """AFTER DELETE ON images BEGIN
            DELETE FROM images_fts WHERE rowid = old.id; END""",
        "images_fts_ia_i": f"""AFTER INSERT ON image_albums BEGIN
            UPDATE images_fts SET albums = {albums_of.format(id="new.image_id")}
            WHERE rowid = new.image_id; END""",
        "images_fts_ia_d": f"""AFTER DELETE ON image_albums BEGIN
            UPDATE images_fts SET albums = {albums_of.format(id="old.image_id")}
            WHERE rowid = old.image_id; END""",
        "images_fts_alb_u": f"""AFTER UPDATE OF name ON albums BEGIN
            UPDATE images_fts SET albums = {albums_of.format(id="images_fts.rowid")}
            WHERE rowid IN (SELECT image_id FROM image_albums WHERE album_id = new.id); END""",
    }
    for name, body in triggers.items():
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {name} {body}")
    # Fill (or refill, if it ever got out of step) from the photos table.
    have = conn.execute("SELECT COUNT(*) FROM images_fts").fetchone()[0]
    want = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    if have != want:
        conn.execute("DELETE FROM images_fts")
        conn.execute(f"""INSERT INTO images_fts(rowid, title, tags, caption, albums)
            SELECT id, title, tags, caption, {albums_of.format(id="images.id")} FROM images""")
    FTS_OK = True


init_db()

# Daily automatic copy of the database to R2 (on by default in production,
# set AUTO_BACKUP=0 to turn it off).
if storage.kind == "r2" and os.environ.get("AUTO_BACKUP", "0" if IS_DEV else "1") == "1":
    start_backup_scheduler(DB_PATH, storage, app.logger)


# ---------------------------------------------------------------------------
# Security helpers: login, CSRF, rate limits
# ---------------------------------------------------------------------------
def client_ip():
    fly_ip = request.headers.get("Fly-Client-IP", "").strip()   # set by Fly's proxy
    if fly_ip and not TRUST_CLOUDFLARE:
        return fly_ip[:64]
    if TRUST_CLOUDFLARE:
        cf_ip = request.headers.get("CF-Connecting-IP", "").strip()
        if cf_ip:
            return cf_ip[:64]
    return (request.remote_addr or "unknown")[:64]


def rate_count(conn, kind, ip, window_seconds):
    row = conn.execute(
        "SELECT COUNT(*) FROM rate_events WHERE kind = ? AND ip = ? AND ts > ?",
        (kind, ip, time.time() - window_seconds),
    ).fetchone()
    return row[0]


def rate_record(conn, kind, ip):
    conn.execute(
        "INSERT INTO rate_events (kind, ip, ts) VALUES (?, ?, ?)", (kind, ip, time.time())
    )
    # Tidy up old entries now and then so the table stays tiny.
    if secrets.randbelow(50) == 0:
        conn.execute("DELETE FROM rate_events WHERE ts < ?", (time.time() - 24 * 3600,))
    conn.commit()


def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def safe_next_url(target):
    """Only allow redirects to pages on this same site (no open redirect)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        parsed = urlparse(target)
        if not parsed.scheme and not parsed.netloc:
            return target
    return url_for("upload")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.before_request
def check_origin_secret():
    if not ORIGIN_SECRET or request.path == "/healthz":
        return None
    sent = request.headers.get("X-Origin-Secret", "")
    if not hmac.compare_digest(sent, ORIGIN_SECRET):
        abort(403)
    return None


@app.before_request
def csrf_protect():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if request.path == "/api/hit":      # anonymous page-view signal; changes nothing but a counter
        return None
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("_csrf", "")
    if not expected or not hmac.compare_digest(sent, expected):
        if request.path.startswith(("/upload/file", "/api/")):
            return jsonify(ok=False, error="Session expired. Reload the page and try again."), 400
        abort(400, description="The page expired. Go back, reload it and try again.")
    return None


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    img_src = "'self' data: blob:"
    if storage.kind == "r2":
        parsed = urlparse(storage.public_base)
        img_src += f" {parsed.scheme}://{parsed.netloc}"
    resp.headers.setdefault(
        "Content-Security-Policy",
        f"default-src 'self'; img-src {img_src}; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    # Pages contain per-visitor tokens - never let a shared cache (e.g. a CDN) keep them.
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "private, no-cache"
    return resp


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def make_thumbnail(src_path, dst_path):
    with PILImage.open(src_path) as img:
        img = ImageOps.exif_transpose(img)  # respect phone/camera rotation
        icc = img.info.get("icc_profile")   # keep colours accurate
        img = img.convert("RGB")
        img.thumbnail(THUMB_SIZE, PILImage.Resampling.LANCZOS)
        save_args = {"quality": 85, "optimize": True, "progressive": True}
        if icc:
            save_args["icc_profile"] = icc
        img.save(dst_path, "JPEG", **save_args)


def _mozjpeg_pack(data):
    """Lossless re-pack of JPEG bytes (identical pixels, all metadata kept)."""
    try:
        import mozjpeg_lossless_optimization as mozjpeg
    except ImportError:
        return data
    return mozjpeg.optimize(data, mozjpeg.COPY_MARKERS.ALL)


def _downscale_jpeg(src_path):
    """Return JPEG bytes of the photo scaled to MAX_IMAGE_PX on its longest
    side (lossy, quality JPEG_QUALITY). Rotation is baked in, the colour
    profile and EXIF (camera, date ...) are kept, GPS position is dropped."""
    with PILImage.open(src_path) as img:
        keep_icc = img.mode in ("RGB", "L")
        icc = img.info.get("icc_profile") if keep_icc else None
        if img.format == "JPEG":
            img.draft("RGB", (MAX_IMAGE_PX, MAX_IMAGE_PX))
        img = ImageOps.exif_transpose(img)
        exif = img.getexif()
        exif.pop(0x8825, None)   # GPS
        exif.pop(274, None)      # orientation (already applied)
        img = img.convert("RGB")
        img.thumbnail((MAX_IMAGE_PX, MAX_IMAGE_PX), PILImage.Resampling.LANCZOS)
        buf = io.BytesIO()
        save_args = {"quality": JPEG_QUALITY, "subsampling": JPEG_SUBSAMPLING,
                     "optimize": True, "progressive": True}
        if icc:
            save_args["icc_profile"] = icc
        if len(exif):
            save_args["exif"] = exif.tobytes()
        img.save(buf, "JPEG", **save_args)
    return _mozjpeg_pack(buf.getvalue())


def optimize_image(src_path, dst_path, ext):
    """Make a photo smaller before it is stored. Returns True if a smaller
    file was written to dst_path; False means "store the upload untouched".

    - JPEG bigger than MAX_IMAGE_PX: downscaled to MAX_IMAGE_PX and re-encoded
      at JPEG_QUALITY (typically 60-75% smaller for camera files).
    - JPEG up to MAX_IMAGE_PX: lossless only (mozjpeg re-pack, identical
      pixels, all metadata kept, typically 5-15% smaller).
    - PNG: re-saved with maximum zlib compression, pixels verified identical.
    - everything else (GIF, WEBP, animated files): stored exactly as uploaded.
    A result that isn't smaller than the upload is never used.
    """
    original_size = os.path.getsize(src_path)

    if ext in ("jpg", "jpeg"):
        with PILImage.open(src_path) as probe:
            too_big = max(probe.size) > MAX_IMAGE_PX
        if too_big:
            out = _downscale_jpeg(src_path)
            if len(out) < original_size:
                with open(dst_path, "wb") as f:
                    f.write(out)
                return True
        with open(src_path, "rb") as f:
            out = _mozjpeg_pack(f.read())
        if len(out) >= original_size:
            return False
        with open(dst_path, "wb") as f:
            f.write(out)
        return True

    if ext == "png":
        with PILImage.open(src_path) as img:
            if getattr(img, "is_animated", False):
                return False
            img.load()
            args = {"optimize": True, "compress_level": 9}
            for key in ("icc_profile", "transparency", "dpi", "gamma"):
                if key in img.info:
                    args[key] = img.info[key]
            img.save(dst_path, "PNG", **args)
        if os.path.getsize(dst_path) >= original_size:
            return False
        with PILImage.open(src_path) as a, PILImage.open(dst_path) as b:
            same = a.mode == b.mode and a.size == b.size and ImageChops.difference(
                a.convert("RGBA"), b.convert("RGBA")).getbbox() is None
        return same

    return False


def extract_exif_date(path):
    """Return the photo's 'date taken' as 'YYYY-MM-DD', or None if the file
    has no usable EXIF date (e.g. a screenshot, or metadata stripped)."""
    try:
        with PILImage.open(path) as img:
            exif_data = None
            getexif_method = getattr(img, "_getexif", None)
            if getexif_method:
                exif_data = getexif_method()
            if not exif_data:
                exif = img.getexif()
                exif_data = dict(exif) if exif else None
            if not exif_data:
                return None
            for tag_id, value in exif_data.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag in ("DateTimeOriginal", "DateTime"):
                    if isinstance(value, bytes):
                        value = value.decode(errors="ignore")
                    date_part = str(value).split(" ")[0]
                    parts = date_part.replace("-", ":").split(":")
                    if len(parts) == 3 and all(p.strip().isdigit() for p in parts):
                        y, m, d = parts
                        return f"{y}-{m}-{d}"
    except Exception:
        pass
    return None


def normalize_tags(raw_tags):
    """Turn 'Bus, Volvo ,  B10M,sweden' into a clean 'bus, volvo, b10m, sweden'."""
    parts = [t.strip().lower() for t in raw_tags.replace(";", ",").split(",")]
    parts = [p for p in parts if p]
    seen = set()
    result = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return ", ".join(result)


def get_all_albums(conn):
    return conn.execute("SELECT * FROM albums ORDER BY name COLLATE NOCASE").fetchall()


def parse_album_id(raw_value):
    """Turn the album <select> value into an int id or None."""
    raw_value = (raw_value or "").strip()
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except ValueError:
        return None


def parse_album_ids(conn, raw_values):
    """Turn the album checkboxes into a de-duplicated list of ids that exist."""
    ids = []
    for raw in raw_values or []:
        album_id = parse_album_id(raw)
        if album_id is not None and album_id not in ids:
            ids.append(album_id)
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    existing = {r["id"] for r in conn.execute(f"SELECT id FROM albums WHERE id IN ({marks})", ids)}
    return [i for i in ids if i in existing]


def get_albums_for_image(conn, image_id):
    return conn.execute(
        "SELECT albums.id, albums.name FROM image_albums "
        "JOIN albums ON albums.id = image_albums.album_id "
        "WHERE image_albums.image_id = ? ORDER BY albums.name COLLATE NOCASE",
        (image_id,),
    ).fetchall()


# --- Searching / browsing ---------------------------------------------------
IMAGES_FROM = "FROM images LEFT JOIN albums ON images.album_id = albums.id"


def fts_query(text):
    """User text -> FTS5 query: every word must match, each as a word start."""
    parts = []
    for word in text.lower().split()[:8]:
        word = re.sub(r"[^\w]+", " ", word, flags=re.UNICODE).strip()
        for piece in word.split():
            parts.append('"' + piece.replace('"', '""') + '"*')
    return " ".join(parts)


def build_filters(category=None, album_id=None, query=None):
    """WHERE clause (+ params) shared by the category page and prev/next."""
    where = ["1 = 1"]
    params = []
    if category:
        where.append("images.category = ?")
        params.append(category)
    if album_id:
        where.append(
            "EXISTS (SELECT 1 FROM image_albums ia "
            "WHERE ia.image_id = images.id AND ia.album_id = ?)"
        )
        params.append(album_id)
    if query and FTS_OK:
        match = fts_query(query)
        if match:
            where.append("images.id IN (SELECT rowid FROM images_fts WHERE images_fts MATCH ?)")
            params.append(match)
        else:
            where.append("0")
    elif query:
        for term in query.lower().split():
            where.append(
                "(LOWER(images.title) LIKE ? OR LOWER(images.tags) LIKE ? "
                "OR LOWER(images.caption) LIKE ? OR EXISTS ("
                "SELECT 1 FROM image_albums ia JOIN albums a ON a.id = ia.album_id "
                "WHERE ia.image_id = images.id AND LOWER(a.name) LIKE ?))"
            )
            params.extend([f"%{term}%"] * 4)
    return " AND ".join(where), params


def get_neighbors(conn, image_id, category=None, album_id=None, query=None):
    """Previous/next photo and 'n of total' within the current browsing
    context (newest first). Uses small indexed queries instead of loading
    every id in the category - essential with tens of thousands of photos.
    Returns (prev_id, next_id, position, total)."""
    where, params = build_filters(category, album_id, query)
    base = f"{IMAGES_FROM} WHERE {where}"

    in_context = conn.execute(
        f"SELECT 1 {base} AND images.id = ?", params + [image_id]
    ).fetchone()
    if in_context is None:
        return None, None, 1, 1

    total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
    newer = conn.execute(
        f"SELECT COUNT(*) {base} AND images.id > ?", params + [image_id]
    ).fetchone()[0]
    prev_row = conn.execute(
        f"SELECT images.id {base} AND images.id > ? ORDER BY images.id ASC LIMIT 1",
        params + [image_id],
    ).fetchone()
    next_row = conn.execute(
        f"SELECT images.id {base} AND images.id < ? ORDER BY images.id DESC LIMIT 1",
        params + [image_id],
    ).fetchone()
    return (
        prev_row["id"] if prev_row else None,
        next_row["id"] if next_row else None,
        newer + 1,
        total,
    )


# Tag chips are worked out from every photo's tags; remember the result for a
# minute per category rather than recounting on every page view.
TAG_CACHE_SECONDS = 60
_tag_cache = {}


def invalidate_tag_cache():
    _tag_cache.clear()
    try:
        conn = get_db()
        conn.execute("DELETE FROM tag_cache")
        conn.commit()
        conn.close()
    except sqlite3.OperationalError:
        pass


def top_tags_for(conn, slug):
    """The 30 most used tags of a category. Counting needs every photo's tags,
    so the result is kept in memory for a minute AND saved in the database
    (so a restart doesn't make the first visitor wait). The saved copy is
    thrown away when photos change, and is ignored if the category's photo
    count no longer matches."""
    cached = _tag_cache.get(slug)
    if cached and cached[0] > time.time():
        return cached[1]
    n_images = conn.execute("SELECT COUNT(*) FROM images WHERE category = ?", (slug,)).fetchone()[0]
    try:
        row = conn.execute("SELECT n_images, tags FROM tag_cache WHERE category = ?", (slug,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is not None and row["n_images"] == n_images:
        top = [t for t in row["tags"].split("\n") if t]
        _tag_cache[slug] = (time.time() + TAG_CACHE_SECONDS, top)
        return top
    counter = Counter()
    for r in conn.execute("SELECT tags FROM images WHERE category = ?", (slug,)):
        for t in (r["tags"] or "").split(","):
            t = t.strip()
            if t:
                counter[t] += 1
    top = sorted(t for t, _ in counter.most_common(30))
    _tag_cache[slug] = (time.time() + TAG_CACHE_SECONDS, top)
    try:
        conn.execute("INSERT OR REPLACE INTO tag_cache (category, n_images, tags) VALUES (?, ?, ?)",
                     (slug, n_images, "\n".join(top)))
        conn.commit()
    except sqlite3.OperationalError:
        pass
    return top


def utc_now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def format_display_date(iso_date):
    """'2024-06-12T...' -> '12 Jun 2024' (unambiguous for an international audience)."""
    if not iso_date:
        return ""
    try:
        parsed = datetime.strptime(iso_date[:10], "%Y-%m-%d")
        return parsed.strftime("%d %b %Y")
    except Exception:
        return iso_date[:10]


app.jinja_env.filters["fmtdate"] = format_display_date


# --- SEO helpers: URL slugs, alt texts, absolute URLs -----------------------
_SLUG_TRANSLIT = {"æ": "ae", "ø": "oe", "å": "aa", "ß": "ss", "đ": "d", "ł": "l"}


def slugify(text, max_len=60):
    """'Volvo B10M, Århus 1998' -> 'volvo-b10m-aarhus-1998'."""
    text = (text or "").lower()
    for src, dst in _SLUG_TRANSLIT.items():
        text = text.replace(src, dst)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(text) > max_len:
        text = text[:max_len].rsplit("-", 1)[0] or text[:max_len]
    return text or "photo"


ALT_MAX_LEN = 125   # longer alt texts get cut off by screen readers and search engines
ALT_MAX_TAGS = 4


def image_alt(image):
    """'Title - tag1, tag2' built from the database row. Tags that are already
    part of the title are left out, so it doesn't say 'Volvo B10M - volvo, b10m'."""
    title = (image["title"] or "").strip()
    title_lower = title.lower()
    tags = []
    for tag in (image["tags"] or "").split(","):
        tag = tag.strip()
        if tag and tag.lower() not in title_lower and tag.lower() not in [t.lower() for t in tags]:
            tags.append(tag)
    alt = title
    used = 0
    for tag in tags[:ALT_MAX_TAGS]:
        candidate = f"{alt} - {tag}" if used == 0 else f"{alt}, {tag}"
        if len(candidate) > ALT_MAX_LEN:
            break
        alt, used = candidate, used + 1
    return alt


def image_url(image, **kwargs):
    """Clean URL of a photo page: /image/43418-volvo-b10m. Extra keyword
    arguments (cat=, q=, album=) become the query string, as before."""
    return url_for("image_detail", image_id=image["id"], slug=slugify(image["title"]),
                   **{k: v for k, v in kwargs.items() if v not in (None, "")})


def absolute_url(path_or_url):
    """Full https://... address for a path such as /albums."""
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    base = SITE_URL or request.url_root.rstrip("/")
    return base + path_or_url


app.jinja_env.globals.update(image_alt=image_alt, image_url=image_url,
                             absolute_url=absolute_url, slugify=slugify)


# --- Photo URLs (local folders or Cloudflare R2) -----------------------------
def media_url(area, filename):
    public = storage.public_url(area, filename)
    if public:
        return public
    return url_for("media_file", area=area, filename=filename)


def thumb_url(thumbnail, filename=None):
    """URL of a thumbnail. (If a thumbnail could not be generated when the
    photo was uploaded, the original is used in its place.)"""
    if filename and thumbnail == filename:
        return media_url("uploads", filename)
    return media_url("thumbnails", thumbnail)


_static_versions = {}


@app.url_defaults
def add_static_version(endpoint, values):
    """Append ?v=<content hash> to /static/ URLs.

    Static files are cached for a year (see SEND_FILE_MAX_AGE_DEFAULT), so
    without this a browser/CDN keeps serving an old style.css after a deploy.
    With it, a changed file gets a new URL and is fetched again, while an
    unchanged file stays cached.
    """
    if endpoint != "static" or "filename" not in values:
        return
    name = values["filename"]
    version = _static_versions.get(name)
    if version is None:
        path = os.path.join(app.static_folder, name)
        try:
            with open(path, "rb") as f:
                version = hashlib.md5(f.read()).hexdigest()[:10]
        except OSError:
            return
        _static_versions[name] = version
    values["v"] = version



# With R2 the photos come from another host; telling the browser early lets it
# open that connection while the page is still loading.
if storage.kind == "r2":
    _origin = urlparse(storage.public_base)
    IMAGE_ORIGIN = f"{_origin.scheme}://{_origin.netloc}"
else:
    IMAGE_ORIGIN = ""


@app.context_processor
def inject_globals():
    return {
        "CONTACT_EMAIL": CONTACT_EMAIL,
        "CATEGORIES": CATEGORIES,
        "csrf_token": csrf_token,
        "media_url": media_url,
        "thumb_url": thumb_url,
        "image_origin": IMAGE_ORIGIN,
    }


# --- View counting ----------------------------------------------------------
# One view per visitor per photo/page per 24 hours, remembered in a small
# cookie ("seen"). Reloading a page does not raise the numbers. Not counted:
# the owner (logged in), search engines/bots, and non-GET requests (HEAD etc.).
VIEW_COOKIE = "seen"
VIEW_WINDOW = 24 * 3600          # seconds before the same visitor counts again
VIEW_COOKIE_MAX_ENTRIES = 120    # keeps the cookie well below the 4 KB browser limit
BOT_WORDS = ("bot", "crawl", "spider", "slurp", "preview", "facebookexternalhit",
             "curl", "wget", "python-requests", "headless")


def is_countable_visitor():
    if request.method not in ("GET", "POST") or session.get("logged_in"):
        return False
    ua = request.headers.get("User-Agent", "").lower()
    return bool(ua) and not any(word in ua for word in BOT_WORDS)


def _seen_views():
    """{key: unix time} of what this visitor has viewed within the window."""
    if not hasattr(g, "seen_views"):
        seen = {}
        now = time.time()
        raw = request.cookies.get(VIEW_COOKIE, "")
        for part in raw.split(",")[:VIEW_COOKIE_MAX_ENTRIES * 2]:
            key, _, stamp = part.partition(".")
            if not key or len(key) > 20 or not stamp.isdigit():
                continue
            if now - int(stamp) < VIEW_WINDOW:
                seen[key] = int(stamp)
        g.seen_views = seen
    return g.seen_views


def first_view(key):
    """True the first time this visitor views `key` within the window. The
    caller counts it only when this returns True."""
    if not is_countable_visitor():
        return False
    seen = _seen_views()
    if key in seen:
        return False
    seen[key] = int(time.time())
    g.seen_views_changed = True
    return True


@app.after_request
def remember_views(resp):
    if getattr(g, "seen_views_changed", False):
        newest = sorted(g.seen_views.items(), key=lambda kv: kv[1], reverse=True)
        value = ",".join(f"{k}.{t}" for k, t in newest[:VIEW_COOKIE_MAX_ENTRIES])
        resp.set_cookie(VIEW_COOKIE, value, max_age=VIEW_WINDOW, httponly=True,
                        samesite="Lax", secure=not IS_DEV)
    return resp


# Page views are counted by a tiny script in the page (see base.html) that
# tells the server "this page was shown" once the page has loaded. Scrapers
# that only download the HTML never run it, so they don't inflate the numbers.
HIT_PATH = re.compile(
    r"^(?:/|/albums|/albums/\d+|/category/[a-z0-9-]+|/image/(?P<image>\d+)(?:-[a-z0-9-]*)?)$")


@app.route("/api/hit", methods=["POST"])
def api_hit():
    path = (request.get_data(as_text=True) or "")[:300].split("?")[0].split("#")[0]
    m = HIT_PATH.match(path)
    if not m or not is_countable_visitor():
        return "", 204
    image_id = m.group("image")
    try:
        conn = get_db()
        if image_id and first_view(f"i{image_id}"):
            conn.execute("UPDATE images SET views = views + 1 WHERE id = ?", (int(image_id),))
        if first_view("p" + format(zlib.crc32(path.encode("utf-8")), "x")):
            conn.execute("UPDATE site_stats SET value = value + 1 WHERE key = 'total_visits'")
        conn.commit()
        conn.close()
    except Exception:
        pass
    return "", 204


# --- Slowing down scrapers ----------------------------------------------------
# One address may load this many pages per minute (photos and thumbnails are
# not pages and are not counted). Search-engine crawlers that identify
# themselves are let through; everything else gets "429 Too Many Requests"
# for a while. The counters live in memory (the app runs as one process).
PAGE_LIMIT_PER_MIN = int(os.environ.get("PAGE_LIMIT_PER_MIN", "90"))
SEARCH_LIMIT_PER_MIN = int(os.environ.get("SEARCH_LIMIT_PER_MIN", "20"))
GOOD_BOTS = ("googlebot", "bingbot", "duckduckbot", "applebot")
_page_hits = {}


@app.before_request
def slow_down_scrapers():
    if request.method not in ("GET", "HEAD") or session.get("logged_in"):
        return None
    if request.path.startswith(("/media", "/static", "/download", "/healthz", "/sitemap", "/robots.txt")):
        return None
    ua = request.headers.get("User-Agent", "").lower()
    if any(b in ua for b in GOOD_BOTS):
        return None
    now = time.time()
    ip = client_ip()
    kinds = [("page", PAGE_LIMIT_PER_MIN)]
    if request.args.get("q"):
        kinds.append(("search", SEARCH_LIMIT_PER_MIN))
    if len(_page_hits) > 5000:
        _page_hits.clear()
    for kind, limit in kinds:
        hits = [t for t in _page_hits.get((kind, ip), ()) if now - t < 60]
        if len(hits) >= limit:
            _page_hits[(kind, ip)] = hits
            resp = Response("Too many requests - please slow down.", status=429, mimetype="text/plain")
            resp.headers["Retry-After"] = "60"
            return resp
        hits.append(now)
        _page_hits[(kind, ip)] = hits
    return None


@app.route("/healthz")
def healthz():
    conn = get_db()
    conn.execute("SELECT 1").fetchone()
    conn.close()
    return "ok", 200, {"Content-Type": "text/plain"}


# ---------------------------------------------------------------------------
# Routes: search engines (robots.txt + XML sitemap)
#
# /sitemap.xml is an index that points at smaller sitemaps - Google allows at
# most 50,000 addresses per file, and the archive already has ~43,000 photos.
# Photos are cut into fixed chunks ordered by id, so new uploads only ever
# change the last file. Everything is built from the database on request, so
# there is nothing to regenerate by hand.
# ---------------------------------------------------------------------------
SITEMAP_CHUNK = 10000


def xml_response(body):
    resp = Response(body, mimetype="application/xml")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/robots.txt")
def robots_txt():
    lines = [
        "User-agent: *",
        "Disallow: /login",
        "Disallow: /logout",
        "Disallow: /upload",
        "Disallow: /stats",
        "Disallow: /download/",
        "Disallow: /api/",
        "Disallow: /*?q=",
        "Disallow: /*&q=",
        "Crawl-delay: 5",
        "Disallow: /comment/",
        "Disallow: /image/*/edit",
        "Disallow: /image/*/comment",
        "Disallow: /image/*/delete",
        "",
        f"Sitemap: {absolute_url('/sitemap.xml')}",
        "",
    ]
    return Response("\n".join(lines), mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_index():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    conn.close()
    chunks = max(1, (total + SITEMAP_CHUNK - 1) // SITEMAP_CHUNK)
    locs = [absolute_url("/sitemap-pages.xml")]
    locs += [absolute_url(f"/sitemap-images-{n}.xml") for n in range(1, chunks + 1)]
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "".join(f"  <sitemap><loc>{xml_escape(loc)}</loc></sitemap>\n" for loc in locs)
            + "</sitemapindex>\n")
    return xml_response(body)


@app.route("/sitemap-pages.xml")
def sitemap_pages():
    conn = get_db()
    albums = conn.execute(
        "SELECT DISTINCT album_id FROM image_albums ORDER BY album_id").fetchall()
    conn.close()
    paths = ["/", "/albums"]
    paths += [url_for("category_view", slug=c["slug"]) for c in CATEGORIES]
    paths += [url_for("album_detail", album_id=a["album_id"]) for a in albums]
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "".join(f"  <url><loc>{xml_escape(absolute_url(p))}</loc></url>\n" for p in paths)
            + "</urlset>\n")
    return xml_response(body)


@app.route("/sitemap-images-<int:n>.xml")
def sitemap_images(n):
    if n < 1:
        abort(404)
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, filename FROM images ORDER BY id LIMIT ? OFFSET ?",
        (SITEMAP_CHUNK, (n - 1) * SITEMAP_CHUNK),
    ).fetchall()
    conn.close()
    if not rows and n > 1:
        abort(404)
    # Only <image:loc> is listed: Google ignores the old title/caption tags.
    # No <lastmod>: the table has no "edited" date, and a wrong lastmod is
    # worse than none.
    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n'
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
             'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">\n']
    for r in rows:
        page = absolute_url(url_for("image_detail", image_id=r["id"], slug=slugify(r["title"])))
        photo = absolute_url(media_url("uploads", r["filename"]))
        parts.append(
            f"  <url><loc>{xml_escape(page)}</loc>"
            f"<image:image><image:loc>{xml_escape(photo)}</image:loc></image:image></url>\n")
    parts.append("</urlset>\n")
    return xml_response("".join(parts))


# ---------------------------------------------------------------------------
# Routes: homepage (category tiles)
# ---------------------------------------------------------------------------
@app.route("/")
def gallery():
    conn = get_db()
    tiles = []
    for cat in CATEGORIES:
        cover = conn.execute(
            "SELECT thumbnail, filename FROM images WHERE category = ? ORDER BY id DESC LIMIT 1",
            (cat["slug"],),
        ).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) FROM images WHERE category = ?", (cat["slug"],)
        ).fetchone()[0]
        tiles.append({
            **cat,
            "cover_thumb": cover["thumbnail"] if cover else None,
            "cover_filename": cover["filename"] if cover else None,
            "count": count,
        })
    latest = conn.execute(
        """SELECT id, title, tags, thumbnail, filename, category
           FROM images ORDER BY id DESC LIMIT 8"""
    ).fetchall()
    album_count = conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
    conn.close()
    return render_template(
        "index.html", tiles=tiles, latest=latest, album_count=album_count,
        total_photos=sum(t["count"] for t in tiles),
    )


# ---------------------------------------------------------------------------
# Routes: category page (search + tag chips + grid)
# ---------------------------------------------------------------------------
@app.route("/category/<slug>")
def category_view(slug):
    if slug not in CATEGORY_MAP:
        abort(404)
    category = CATEGORY_MAP[slug]

    query = request.args.get("q", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    offset = (page - 1) * PER_PAGE

    where, params = build_filters(category=slug, query=query)

    conn = get_db()
    total = conn.execute(f"SELECT COUNT(*) FROM images WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        "SELECT images.*, albums.name AS album_name, "
        "(SELECT COUNT(*) FROM image_albums ia WHERE ia.image_id = images.id) AS album_total "
        f"{IMAGES_FROM} WHERE {where} "
        "ORDER BY images.id DESC LIMIT ? OFFSET ?",
        params + [PER_PAGE, offset],
    ).fetchall()
    top_tags = top_tags_for(conn, slug)
    conn.close()

    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

    return render_template(
        "category.html",
        category=category,
        images=rows,
        query=query,
        page=page,
        total_pages=total_pages,
        total=total,
        tags=top_tags,
    )


# ---------------------------------------------------------------------------
# Routes: single image detail (with browsing, comments, EXIF date)
# ---------------------------------------------------------------------------
@app.route("/image/<int:image_id>", defaults={"slug": None})
@app.route("/image/<int:image_id>-<slug>")
def image_detail(image_id, slug):
    conn = get_db()
    row = conn.execute(f"""
        SELECT images.*, albums.name AS album_name
        {IMAGES_FROM}
        WHERE images.id = ?
    """, (image_id,)).fetchone()
    if row is None:
        conn.close()
        abort(404)

    # One address per photo: /image/43418 and a stale or misspelt slug (e.g.
    # after the title was edited) are sent permanently (301) to the current
    # /image/43418-title. ?cat=/?q=/?album= are kept so browsing still works.
    expected_slug = slugify(row["title"])
    if slug != expected_slug:
        conn.close()
        target = url_for("image_detail", image_id=image_id, slug=expected_slug)
        if request.query_string:
            target += "?" + request.query_string.decode("utf-8", "replace")
        return redirect(target, code=301)

    album_param = request.args.get("album", type=int)
    cat_param = request.args.get("cat")
    q_param = request.args.get("q", "").strip()

    if album_param:
        prev_id, next_id, position, total_in_context = get_neighbors(
            conn, image_id, album_id=album_param)
    elif cat_param:
        prev_id, next_id, position, total_in_context = get_neighbors(
            conn, image_id, category=cat_param, query=q_param)
    else:
        cat_param = row["category"]
        prev_id, next_id, position, total_in_context = get_neighbors(
            conn, image_id, category=cat_param)

    comments = conn.execute(
        "SELECT * FROM comments WHERE image_id = ? ORDER BY created_at ASC", (image_id,)
    ).fetchall()
    image_albums = get_albums_for_image(conn, image_id)
    next_full_url = None
    prev_image = next_image = None
    if prev_id:
        prev_image = conn.execute("SELECT id, title FROM images WHERE id = ?", (prev_id,)).fetchone()
    if next_id:
        next_image = conn.execute(
            "SELECT id, title, filename FROM images WHERE id = ?", (next_id,)).fetchone()
        if next_image:
            next_full_url = media_url("uploads", next_image["filename"])
    conn.close()

    # The list page this photo sits on (24 per page, newest first - the same
    # order as the category page), so "Back" returns to e.g. page 4, not page 1.
    back_page = (position - 1) // PER_PAGE + 1 if not album_param else 1

    return render_template(
        "detail.html",
        back_page=back_page,
        next_full_url=next_full_url,
        prev_image=prev_image,
        next_image=next_image,
        image=row,
        image_albums=image_albums,
        comments=comments,
        prev_id=prev_id,
        next_id=next_id,
        position=position,
        total_in_context=total_in_context,
        cat_param=cat_param,
        q_param=q_param,
        album_param=album_param,
        category_label=CATEGORY_MAP.get(row["category"], {}).get("label", row["category"]),
    )


@app.route("/image/<int:image_id>/comment", methods=["POST"])
def add_comment(image_id):
    conn = get_db()
    exists = conn.execute("SELECT id FROM images WHERE id = ?", (image_id,)).fetchone()
    if exists is None:
        conn.close()
        abort(404)

    honeypot = request.form.get("website", "").strip()
    name = (request.form.get("name", "").strip() or "Anonym")[:80]
    comment_text = request.form.get("comment", "").strip()[:2000]

    if not honeypot and comment_text:
        ip = client_ip()
        if rate_count(conn, "comment", ip, COMMENT_WINDOW_SECONDS) >= COMMENT_MAX:
            flash("You are posting too quickly - please wait a few minutes and try again.")
        else:
            rate_record(conn, "comment", ip)
            conn.execute(
                "INSERT INTO comments (image_id, name, comment, created_at) VALUES (?, ?, ?, ?)",
                (image_id, name, comment_text, utc_now_iso()),
            )
            conn.commit()
    conn.close()

    kwargs = {"image_id": image_id}
    if request.form.get("cat"):
        kwargs["cat"] = request.form.get("cat")
    if request.form.get("q"):
        kwargs["q"] = request.form.get("q")
    if request.form.get("album"):
        kwargs["album"] = request.form.get("album")
    return redirect(url_for("image_detail", **kwargs) + "#comments")


@app.route("/comment/<int:comment_id>/delete", methods=["POST"])
@login_required
def delete_comment(comment_id):
    conn = get_db()
    row = conn.execute("SELECT image_id FROM comments WHERE id = ?", (comment_id,)).fetchone()
    if row is None:
        conn.close()
        abort(404)
    conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    conn.commit()
    conn.close()
    flash("Comment deleted.")
    return redirect(url_for("image_detail", image_id=row["image_id"]) + "#comments")


# ---------------------------------------------------------------------------
# Routes: albums
# ---------------------------------------------------------------------------
@app.route("/albums")
def albums_list():
    # Same search behaviour as the category pages: every word must match
    # (case-insensitive, partial words allowed).
    query = request.args.get("q", "").strip()
    where = ["1 = 1"]
    params = []
    for term in query.lower().split():
        where.append("LOWER(albums.name) LIKE ?")
        params.append(f"%{term}%")

    conn = get_db()
    albums = conn.execute(f"""
        SELECT albums.*,
               COUNT(image_albums.image_id) AS photo_count,
               (SELECT images.thumbnail FROM image_albums x
                JOIN images ON images.id = x.image_id
                WHERE x.album_id = albums.id
                ORDER BY x.image_id DESC LIMIT 1) AS cover_thumb,
               (SELECT images.filename FROM image_albums x
                JOIN images ON images.id = x.image_id
                WHERE x.album_id = albums.id
                ORDER BY x.image_id DESC LIMIT 1) AS cover_filename
        FROM albums
        LEFT JOIN image_albums ON image_albums.album_id = albums.id
        WHERE {" AND ".join(where)}
        GROUP BY albums.id
        ORDER BY albums.name COLLATE NOCASE
    """, params).fetchall()
    conn.close()
    return render_template("albums.html", albums=albums, query=query)


@app.route("/albums/new", methods=["POST"])
@login_required
def create_album():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Please enter an album name.")
        return redirect(url_for("albums_list"))
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO albums (name, created_at) VALUES (?, ?)",
            (name, utc_now_iso()),
        )
        conn.commit()
        flash(f'Album "{name}" created.')
    except sqlite3.IntegrityError:
        flash(f'An album named "{name}" already exists.')
    conn.close()
    return redirect(url_for("albums_list"))


@app.route("/albums/<int:album_id>")
def album_detail(album_id):
    conn = get_db()
    album = conn.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()
    if album is None:
        conn.close()
        abort(404)
    images = conn.execute(
        "SELECT images.* FROM images "
        "JOIN image_albums ia ON ia.image_id = images.id "
        "WHERE ia.album_id = ? ORDER BY images.id DESC", (album_id,)
    ).fetchall()
    conn.close()
    return render_template("album_detail.html", album=album, images=images)


@app.route("/albums/<int:album_id>/rename", methods=["POST"])
@login_required
def rename_album(album_id):
    name = request.form.get("name", "").strip()
    if not name:
        flash("Please enter an album name.")
        return redirect(url_for("album_detail", album_id=album_id))
    conn = get_db()
    try:
        conn.execute("UPDATE albums SET name = ? WHERE id = ?", (name, album_id))
        conn.commit()
        flash("Album renamed.")
    except sqlite3.IntegrityError:
        flash(f'An album named "{name}" already exists.')
    conn.close()
    return redirect(url_for("album_detail", album_id=album_id))


@app.route("/albums/<int:album_id>/delete", methods=["POST"])
@login_required
def delete_album(album_id):
    conn = get_db()
    conn.execute("DELETE FROM image_albums WHERE album_id = ?", (album_id,))
    # A photo whose main album was this one gets another of its albums (or none).
    conn.execute(
        "UPDATE images SET album_id = (SELECT MIN(album_id) FROM image_albums "
        "WHERE image_albums.image_id = images.id) WHERE album_id = ?", (album_id,))
    conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
    conn.commit()
    conn.close()
    flash("Album deleted. Its photos were kept, just unassigned.")
    return redirect(url_for("albums_list"))


# ---------------------------------------------------------------------------
# Routes: photo files
#
# With Cloudflare R2 the browser loads photos straight from R2 (media_url()
# returns an R2 address), so /media/... is only used for local storage.
# ---------------------------------------------------------------------------
@app.route("/media/<area>/<path:filename>")
def media_file(area, filename):
    if storage.kind != "local" or area not in ("uploads", "thumbnails"):
        abort(404)
    directory = UPLOAD_FOLDER if area == "uploads" else THUMB_FOLDER
    return send_from_directory(directory, filename)


@app.route("/download/<int:image_id>")
def download_image(image_id):
    conn = get_db()
    row = conn.execute(
        "SELECT title, filename FROM images WHERE id = ?", (image_id,)
    ).fetchone()
    conn.close()
    if row is None:
        abort(404)

    ext = os.path.splitext(row["filename"])[1].lower()
    download_name = (secure_filename(row["title"]) or "photo")[:80] + ext

    if storage.kind == "r2":
        try:
            return redirect(storage.download_url("uploads", row["filename"], download_name))
        except Exception:
            return redirect(storage.public_url("uploads", row["filename"]))
    return send_from_directory(
        UPLOAD_FOLDER, row["filename"], as_attachment=True, download_name=download_name
    )


# ---------------------------------------------------------------------------
# Routes: login / logout
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = client_ip()
        conn = get_db()
        try:
            if rate_count(conn, "login_fail", ip, LOGIN_WINDOW_SECONDS) >= LOGIN_MAX_FAILS:
                flash("Too many wrong attempts. Please wait 15 minutes and try again.")
                return render_template("login.html"), 429

            password = request.form.get("password", "")
            if check_password_hash(ADMIN_PASSWORD_HASH, password):
                session.clear()  # fresh session after login
                session["logged_in"] = True
                session.permanent = True
                csrf_token()  # new CSRF token for the new session
                return redirect(safe_next_url(request.args.get("next")))

            rate_record(conn, "login_fail", ip)
        finally:
            conn.close()
        flash("Wrong password, try again.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("gallery"))


# ---------------------------------------------------------------------------
# Routes: upload, edit, delete (password protected)
# ---------------------------------------------------------------------------
class _Stopwatch:
    """Tiny helper: seconds spent per named step of one upload (shown in the
    upload page and written to the server log, so slowness can be traced)."""
    def __init__(self):
        self.t0 = self.last = time.perf_counter()
        self.steps = {}

    def lap(self, name):
        now = time.perf_counter()
        self.steps[name] = self.steps.get(name, 0.0) + (now - self.last)
        self.last = now

    def skip(self):          # don't count the time since the last lap
        self.last = time.perf_counter()


def process_upload(file, thumb_file=None, exif_head=None):
    """Check ONE uploaded photo, make its thumbnail and put both files in
    storage (local folder or R2). Returns (info, error).

    The upload page normally shrinks the photo and makes the thumbnail in the
    visitor's own browser and sends them ready-made (thumb_file, plus the first
    bytes of the original in exif_head so the date taken can be read). Then
    this server only checks and stores them, which needs very little memory.
    Without thumb_file (old clients, PNG/GIF/WEBP) it does all the work itself."""
    if not file or not file.filename:
        return None, "No file received."
    if not allowed_file(file.filename):
        return None, "Unsupported file type (use JPG, PNG, GIF or WEBP)."

    ext = file.filename.rsplit(".", 1)[1].lower()
    unique_name = secure_filename(f"{uuid.uuid4().hex}.{ext}")
    thumb_name = f"{uuid.uuid4().hex}.jpg"
    tmp_path = os.path.join(TMP_FOLDER, unique_name)
    tmp_full = os.path.join(TMP_FOLDER, "opt_" + unique_name)
    tmp_thumb = os.path.join(TMP_FOLDER, thumb_name)
    tmp_head = os.path.join(TMP_FOLDER, "head_" + unique_name)
    prepared = bool(thumb_file and getattr(thumb_file, "filename", None) and ext in ("jpg", "jpeg"))
    stored = []
    watch = _Stopwatch()
    g.upload_watch = watch

    try:
        file.save(tmp_path)
        watch.lap("save")

        # Make sure it really is an image before we keep it.
        try:
            with PILImage.open(tmp_path) as probe:
                probe.verify()
        except Exception:
            return None, "That file doesn't look like a valid image."

        if prepared:
            try:
                thumb_file.save(tmp_thumb)
                with PILImage.open(tmp_thumb) as probe:
                    probe.verify()
            except Exception:
                return None, "The thumbnail doesn't look like a valid image."
            photo_taken_date = None
            if exif_head and getattr(exif_head, "filename", None):
                try:
                    exif_head.save(tmp_head)
                    photo_taken_date = extract_exif_date(tmp_head)
                except Exception:
                    photo_taken_date = None
            if not photo_taken_date:
                photo_taken_date = extract_exif_date(tmp_path)
        else:
            photo_taken_date = extract_exif_date(tmp_path)
        watch.lap("check")

        # Lossless size reduction. If it doesn't make the file smaller (or
        # anything goes wrong) the uploaded file is stored exactly as it came.
        full_path = tmp_path
        try:
            if not prepared and optimize_image(tmp_path, tmp_full, ext):
                full_path = tmp_full
        except Exception:
            app.logger.exception("Lossless optimization failed, storing original")
        watch.lap("optimize")

        thumb_ok = True
        if not prepared:
            try:
                make_thumbnail(full_path, tmp_thumb)
            except Exception:
                thumb_ok = False
        watch.lap("thumbnail")

        try:
            storage.put("uploads", unique_name, full_path)
            stored.append(("uploads", unique_name))
            if thumb_ok:
                storage.put("thumbnails", thumb_name, tmp_thumb)
                stored.append(("thumbnails", thumb_name))
            watch.lap("store")
        except Exception:
            app.logger.exception("Could not store uploaded photo")
            for area, name in stored:
                try:
                    storage.delete(area, name)
                except Exception:
                    pass
            return None, "Could not save the photo to storage. Please try again."

        return {
            "filename": unique_name,
            # If no thumbnail could be made, the original stands in for it.
            "thumbnail": thumb_name if thumb_ok else unique_name,
            "photo_taken_date": photo_taken_date,
        }, None
    finally:
        for leftover in (tmp_path, tmp_full, tmp_thumb, tmp_head):
            if os.path.exists(leftover):
                os.remove(leftover)


def delete_stored_files(filename, thumbnail):
    """Remove a photo's files from storage (never fails the request)."""
    try:
        storage.delete("uploads", filename)
        if thumbnail != filename:
            storage.delete("thumbnails", thumbnail)
    except Exception:
        app.logger.exception("Could not delete stored files %s / %s", filename, thumbnail)


def save_new_image(conn, file, title, caption, tags, category, album_ids, thumb_file=None, exif_head=None):
    """Validate and store ONE uploaded photo. `album_ids` is a list of album
    ids (the first one becomes the photo's main album). Returns (image_id, error)."""
    if not file or not file.filename:
        return None, "No file received."
    if not allowed_file(file.filename):
        return None, "Unsupported file type (use JPG, PNG, GIF or WEBP)."
    if category not in CATEGORY_MAP:
        return None, "Please choose a category."

    album_ids = parse_album_ids(conn, [str(a) for a in (album_ids or [])])   # only albums that exist
    album_id = album_ids[0] if album_ids else None

    title = (title or "").strip()[:200] or os.path.splitext(file.filename)[0][:200] or "Untitled"
    caption = (caption or "").strip()[:2000]
    tags = normalize_tags(tags or "")

    info, error = process_upload(file, thumb_file, exif_head)
    if error:
        return None, error

    try:
        cur = conn.execute(
            """INSERT INTO images
               (title, tags, filename, thumbnail, uploaded_at, album_id,
                category, caption, photo_taken_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, tags, info["filename"], info["thumbnail"],
             utc_now_iso(), album_id, category, caption,
             info["photo_taken_date"]),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO image_albums (image_id, album_id) VALUES (?, ?)",
            [(cur.lastrowid, a) for a in album_ids],
        )
        conn.commit()
    except Exception:
        delete_stored_files(info["filename"], info["thumbnail"])
        raise
    watch = getattr(g, "upload_watch", None)
    if watch:
        watch.lap("database")
    invalidate_tag_cache()
    return cur.lastrowid, None


@app.route("/upload")
@login_required
def upload():
    conn = get_db()
    albums = [{"id": a["id"], "name": a["name"]} for a in get_all_albums(conn)]
    conn.close()
    categories = [{"slug": c["slug"], "label": c["label"], "emoji": c["emoji"]} for c in CATEGORIES]
    return render_template("upload.html", albums_json=albums, categories_json=categories)


@app.route("/upload/file", methods=["POST"])
def upload_file():
    """Receives ONE photo from the bulk uploader (called once per photo)."""
    if not session.get("logged_in"):
        return jsonify(ok=False, error="You are logged out. Log in again, then retry."), 401

    # Several albums per photo: repeated "album_ids" fields. The old single
    # "album_id" field is still understood.
    raw_albums = request.form.getlist("album_ids") + request.form.getlist("album_id")

    conn = get_db()
    try:
        image_id, error = save_new_image(
            conn,
            request.files.get("photo"),
            request.form.get("title", ""),
            request.form.get("caption", ""),
            request.form.get("tags", ""),
            request.form.get("category", "").strip(),
            [a for a in (parse_album_id(r) for r in raw_albums) if a is not None],
            request.files.get("thumb"),
            request.files.get("exifhead"),
        )
    finally:
        conn.close()

    watch = getattr(g, "upload_watch", None)
    timing = {k: round(v, 2) for k, v in watch.steps.items()} if watch else {}
    if watch:
        timing["total"] = round(time.perf_counter() - watch.t0, 2)
        app.logger.info("upload %s: %s", "ok" if not error else "FAILED", timing)

    if error:
        return jsonify(ok=False, error=error), 400
    return jsonify(ok=True, id=image_id, timing=timing)


@app.route("/api/albums", methods=["POST"])
def api_create_album():
    """Create an album from the uploader without leaving the page."""
    if not session.get("logged_in"):
        return jsonify(ok=False, error="You are logged out."), 401
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:120]
    if not name:
        return jsonify(ok=False, error="Please enter an album name."), 400

    conn = get_db()
    row = conn.execute("SELECT id, name FROM albums WHERE LOWER(name) = LOWER(?)", (name,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO albums (name, created_at) VALUES (?, ?)",
            (name, utc_now_iso()),
        )
        conn.commit()
        row = {"id": cur.lastrowid, "name": name}
    conn.close()
    return jsonify(ok=True, id=row["id"], name=row["name"])


@app.route("/image/<int:image_id>/edit", methods=["GET", "POST"])
@login_required
def edit_image(image_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        conn.close()
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        caption = request.form.get("caption", "").strip()
        tags = normalize_tags(request.form.get("tags", ""))
        category = request.form.get("category", "").strip()
        album_ids = parse_album_ids(conn, request.form.getlist("album_ids"))
        # keep the current main album if it is still selected, otherwise the first one
        album_id = row["album_id"] if row["album_id"] in album_ids else (album_ids[0] if album_ids else None)
        file = request.files.get("photo")

        if not title:
            flash("Please give the photo a title.")
            conn.close()
            return redirect(url_for("edit_image", image_id=image_id))
        if category not in CATEGORY_MAP:
            flash("Please choose a category.")
            conn.close()
            return redirect(url_for("edit_image", image_id=image_id))

        filename = row["filename"]
        thumbnail = row["thumbnail"]
        photo_taken_date = row["photo_taken_date"]
        replaced = False

        if file and file.filename != "":
            info, error = process_upload(file)
            if error:
                flash(error)
                conn.close()
                return redirect(url_for("edit_image", image_id=image_id))
            filename = info["filename"]
            thumbnail = info["thumbnail"]
            photo_taken_date = info["photo_taken_date"]
            replaced = True

        conn.execute(
            """UPDATE images
               SET title = ?, caption = ?, tags = ?, category = ?, filename = ?,
                   thumbnail = ?, album_id = ?, photo_taken_date = ?
               WHERE id = ?""",
            (title, caption, tags, category, filename, thumbnail, album_id,
             photo_taken_date, image_id),
        )
        conn.execute("DELETE FROM image_albums WHERE image_id = ?", (image_id,))
        conn.executemany(
            "INSERT INTO image_albums (image_id, album_id) VALUES (?, ?)",
            [(image_id, a) for a in album_ids],
        )
        conn.commit()
        conn.close()

        # Only remove the old files once the database points at the new ones.
        if replaced:
            delete_stored_files(row["filename"], row["thumbnail"])
        invalidate_tag_cache()
        flash("Photo updated.")
        return redirect(url_for("image_detail", image_id=image_id))

    albums = get_all_albums(conn)
    selected_album_ids = {a["id"] for a in get_albums_for_image(conn, image_id)}
    conn.close()
    return render_template("edit.html", image=row, albums=albums, categories=CATEGORIES,
                           selected_album_ids=selected_album_ids)


@app.route("/image/<int:image_id>/delete", methods=["POST"])
@login_required
def delete_image(image_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if row:
        conn.execute("DELETE FROM comments WHERE image_id = ?", (image_id,))
        conn.execute("DELETE FROM image_albums WHERE image_id = ?", (image_id,))
        conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
        conn.commit()
    conn.close()
    if row:
        delete_stored_files(row["filename"], row["thumbnail"])
        invalidate_tag_cache()
    flash("Photo deleted.")
    return redirect(url_for("gallery"))


# ---------------------------------------------------------------------------
# Routes: stats (private)
# ---------------------------------------------------------------------------
@app.route("/stats")
@login_required
def stats():
    conn = get_db()
    visits_row = conn.execute("SELECT value FROM site_stats WHERE key = 'total_visits'").fetchone()
    total_visits = visits_row["value"] if visits_row else 0

    top_images = conn.execute(
        "SELECT * FROM images ORDER BY views DESC, id DESC LIMIT 20"
    ).fetchall()

    per_category = []
    for cat in CATEGORIES:
        count = conn.execute(
            "SELECT COUNT(*) FROM images WHERE category = ?", (cat["slug"],)
        ).fetchone()[0]
        views_sum = conn.execute(
            "SELECT COALESCE(SUM(views), 0) FROM images WHERE category = ?", (cat["slug"],)
        ).fetchone()[0]
        per_category.append({**cat, "count": count, "views": views_sum})

    total_images = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    total_comments = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    conn.close()

    return render_template(
        "stats.html",
        total_visits=total_visits,
        top_images=top_images,
        per_category=per_category,
        total_images=total_images,
        total_comments=total_comments,
    )


if __name__ == "__main__":
    app.run(debug=IS_DEV, host="127.0.0.1" if IS_DEV else "0.0.0.0",
            port=int(os.environ.get("PORT", 5000)))
