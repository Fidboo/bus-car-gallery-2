import hmac
import os
import secrets
import sqlite3
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, send_from_directory, flash, abort, jsonify
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from PIL import Image as PILImage, ImageOps
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
            plain = os.environ.get("UPLOAD_PASSWORD", "changeme")
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

# Optional contact email. If left empty, the "Contact us" link is hidden.
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "").strip()

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TMP_FOLDER, exist_ok=True)

# Where the photo files live: local folders, or Cloudflare R2 (see storage.py).
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

    # Indexes keep category pages, album pages, prev/next and comment lookups
    # fast when the collection grows to tens of thousands of photos.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_images_category_id ON images(category, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_images_album ON images(album_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_image ON comments(image_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_events ON rate_events(kind, ip, ts)")

    conn.commit()
    conn.close()


init_db()

# Daily automatic copy of the database to R2 (on by default in production,
# set AUTO_BACKUP=0 to turn it off).
if storage.kind == "r2" and os.environ.get("AUTO_BACKUP", "0" if IS_DEV else "1") == "1":
    start_backup_scheduler(DB_PATH, storage, app.logger)


# ---------------------------------------------------------------------------
# Security helpers: login, CSRF, rate limits
# ---------------------------------------------------------------------------
def client_ip():
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


# --- Searching / browsing ---------------------------------------------------
IMAGES_FROM = "FROM images LEFT JOIN albums ON images.album_id = albums.id"


def build_filters(category=None, album_id=None, query=None):
    """WHERE clause (+ params) shared by the category page and prev/next."""
    where = ["1 = 1"]
    params = []
    if category:
        where.append("images.category = ?")
        params.append(category)
    if album_id:
        where.append("images.album_id = ?")
        params.append(album_id)
    if query:
        for term in query.lower().split():
            where.append(
                "(LOWER(images.title) LIKE ? OR LOWER(images.tags) LIKE ? "
                "OR LOWER(images.caption) LIKE ? OR LOWER(albums.name) LIKE ?)"
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


def top_tags_for(conn, slug):
    cached = _tag_cache.get(slug)
    if cached and cached[0] > time.time():
        return cached[1]
    counter = Counter()
    for r in conn.execute("SELECT tags FROM images WHERE category = ?", (slug,)):
        for t in (r["tags"] or "").split(","):
            t = t.strip()
            if t:
                counter[t] += 1
    top = sorted(t for t, _ in counter.most_common(30))
    _tag_cache[slug] = (time.time() + TAG_CACHE_SECONDS, top)
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


@app.context_processor
def inject_globals():
    return {
        "CONTACT_EMAIL": CONTACT_EMAIL,
        "CATEGORIES": CATEGORIES,
        "csrf_token": csrf_token,
        "media_url": media_url,
        "thumb_url": thumb_url,
    }


@app.before_request
def count_visit():
    # Simple site-wide visit counter. Only counts page loads, not image/
    # style file requests or form submissions, so it stays a meaningful
    # "how many times has someone loaded a page" number.
    if request.method == "GET" and not request.path.startswith(
        ("/media", "/static", "/download", "/healthz")
    ):
        try:
            conn = get_db()
            conn.execute("UPDATE site_stats SET value = value + 1 WHERE key = 'total_visits'")
            conn.commit()
            conn.close()
        except Exception:
            pass


@app.route("/healthz")
def healthz():
    conn = get_db()
    conn.execute("SELECT 1").fetchone()
    conn.close()
    return "ok", 200, {"Content-Type": "text/plain"}


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
    conn.close()
    return render_template("index.html", tiles=tiles)


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
    total = conn.execute(f"SELECT COUNT(*) {IMAGES_FROM} WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT images.*, albums.name AS album_name {IMAGES_FROM} WHERE {where} "
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
@app.route("/image/<int:image_id>")
def image_detail(image_id):
    conn = get_db()
    row = conn.execute(f"""
        SELECT images.*, albums.name AS album_name
        {IMAGES_FROM}
        WHERE images.id = ?
    """, (image_id,)).fetchone()
    if row is None:
        conn.close()
        abort(404)

    conn.execute("UPDATE images SET views = views + 1 WHERE id = ?", (image_id,))
    conn.commit()

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
    conn.close()

    return render_template(
        "detail.html",
        image=row,
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
    conn = get_db()
    albums = conn.execute("""
        SELECT albums.*,
               COUNT(images.id) AS photo_count,
               (SELECT thumbnail FROM images
                WHERE images.album_id = albums.id
                ORDER BY images.id DESC LIMIT 1) AS cover_thumb,
               (SELECT filename FROM images
                WHERE images.album_id = albums.id
                ORDER BY images.id DESC LIMIT 1) AS cover_filename
        FROM albums
        LEFT JOIN images ON images.album_id = albums.id
        GROUP BY albums.id
        ORDER BY albums.name COLLATE NOCASE
    """).fetchall()
    conn.close()
    return render_template("albums.html", albums=albums)


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
        "SELECT * FROM images WHERE album_id = ? ORDER BY id DESC", (album_id,)
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
    conn.execute("UPDATE images SET album_id = NULL WHERE album_id = ?", (album_id,))
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
def process_upload(file):
    """Check ONE uploaded photo, make its thumbnail and put both files in
    storage (local folder or R2). Returns (info, error)."""
    if not file or not file.filename:
        return None, "No file received."
    if not allowed_file(file.filename):
        return None, "Unsupported file type (use JPG, PNG, GIF or WEBP)."

    ext = file.filename.rsplit(".", 1)[1].lower()
    unique_name = secure_filename(f"{uuid.uuid4().hex}.{ext}")
    thumb_name = f"{uuid.uuid4().hex}.jpg"
    tmp_path = os.path.join(TMP_FOLDER, unique_name)
    tmp_thumb = os.path.join(TMP_FOLDER, thumb_name)
    stored = []

    try:
        file.save(tmp_path)

        # Make sure it really is an image before we keep it.
        try:
            with PILImage.open(tmp_path) as probe:
                probe.verify()
        except Exception:
            return None, "That file doesn't look like a valid image."

        photo_taken_date = extract_exif_date(tmp_path)

        thumb_ok = True
        try:
            make_thumbnail(tmp_path, tmp_thumb)
        except Exception:
            thumb_ok = False

        try:
            storage.put("uploads", unique_name, tmp_path)
            stored.append(("uploads", unique_name))
            if thumb_ok:
                storage.put("thumbnails", thumb_name, tmp_thumb)
                stored.append(("thumbnails", thumb_name))
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
        for leftover in (tmp_path, tmp_thumb):
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


def save_new_image(conn, file, title, caption, tags, category, album_id):
    """Validate and store ONE uploaded photo. Returns (image_id, error)."""
    if not file or not file.filename:
        return None, "No file received."
    if not allowed_file(file.filename):
        return None, "Unsupported file type (use JPG, PNG, GIF or WEBP)."
    if category not in CATEGORY_MAP:
        return None, "Please choose a category."

    if album_id is not None:
        exists = conn.execute("SELECT id FROM albums WHERE id = ?", (album_id,)).fetchone()
        if exists is None:
            album_id = None

    title = (title or "").strip()[:200] or os.path.splitext(file.filename)[0][:200] or "Untitled"
    caption = (caption or "").strip()[:2000]
    tags = normalize_tags(tags or "")

    info, error = process_upload(file)
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
        conn.commit()
    except Exception:
        delete_stored_files(info["filename"], info["thumbnail"])
        raise
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

    conn = get_db()
    try:
        image_id, error = save_new_image(
            conn,
            request.files.get("photo"),
            request.form.get("title", ""),
            request.form.get("caption", ""),
            request.form.get("tags", ""),
            request.form.get("category", "").strip(),
            parse_album_id(request.form.get("album_id")),
        )
    finally:
        conn.close()

    if error:
        return jsonify(ok=False, error=error), 400
    return jsonify(ok=True, id=image_id)


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
        album_id = parse_album_id(request.form.get("album_id"))
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
        conn.commit()
        conn.close()

        # Only remove the old files once the database points at the new ones.
        if replaced:
            delete_stored_files(row["filename"], row["thumbnail"])
        invalidate_tag_cache()
        flash("Photo updated.")
        return redirect(url_for("image_detail", image_id=image_id))

    albums = get_all_albums(conn)
    conn.close()
    return render_template("edit.html", image=row, albums=albums, categories=CATEGORIES)


@app.route("/image/<int:image_id>/delete", methods=["POST"])
@login_required
def delete_image(image_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if row:
        conn.execute("DELETE FROM comments WHERE image_id = ?", (image_id,))
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
