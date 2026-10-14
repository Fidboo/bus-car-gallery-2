#!/usr/bin/env python3
"""Flickr -> Cloudflare R2 + SQLite import (genoptagelses-sikker).

Foerste gang (i denne rækkefølge):

    python tools/import_flickr.py metadata          # fase 1: ~2 min, kun Flickr API
    python tools/import_flickr.py report            # tjek albums, udelukkelser, kategorier
    caffeinate -i python tools/import_flickr.py images    # fase 2: timer, kan afbrydes og genoptages
    python tools/import_flickr.py verify            # tjek at alt ligger i R2
    python tools/import_flickr.py push-db           # laegger databasen i R2 (se bunden af filen)

Andre kommandoer:
    reclassify        koer regelmotoren (tools/flickr_rules.py) igen
    purge-excluded    slet evt. allerede importerede billeder fra udelukkede albums

Indstillinger (i .env eller som miljoevariabler):
    FLICKR_API_KEY   din Flickr API key (kun key, secret skal IKKE bruges til offentlige billeder)
    R2_*             de samme R2-indstillinger som appen bruger (R2_BUCKET, R2_ACCOUNT_ID, ...)
DATA_DIR i .env ignoreres bevidst (den peger paa serveren); brug --data-dir.

Databasen: scriptet skriver til data/gallery.db (samme skema som appen) plus tre
hjaelpetabeller (flickr_albums, flickr_excluded, flickr_photos) der holder styr
paa hvad der er hentet. Hvert billede faar faste filnavne (fl<flickr-id>.jpg), saa
en afbrudt koersel aldrig efterlader dubletter eller forladte filer i R2.
"""
import argparse
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

import flickr_rules  # noqa: E402

# ---------------------------------------------------------------------------
# Udelukkelser - DISSE MÅ ALDRIG IMPORTERES
# ---------------------------------------------------------------------------
EXCLUDED_ALBUM_IDS = {
    "72157666732780926",   # "ikke mine"
    "72157711114322671",   # "ikke mine"
}
# Alle albums hvis titel/beskrivelse indeholder "ikke mine" udelukkes ogsaa.
EXCLUDE_TITLE_RE = re.compile(r"ikke[\s_\-]*mine", re.IGNORECASE)

DEFAULT_USER_ID = "32322040@N06"      # kimb10m
API_URL = os.environ.get("FLICKR_API_URL", "https://api.flickr.com/services/rest/")
PER_PAGE = 500
EXTRAS = ("description,date_upload,date_taken,original_format,tags,media,"
          "url_o,url_6k,url_5k,url_4k,url_3k,url_k,url_h,url_l,url_c,url_z")
# Foretraekkes i denne raekkefoelge (stoerst foerst)
SIZE_ORDER = ["o", "6k", "5k", "4k", "3k", "k", "h", "l", "c", "z"]

_stop = threading.Event()


# ---------------------------------------------------------------------------
# Opsaetning
# ---------------------------------------------------------------------------
def load_env_file(path):
    """Minimal .env-laeser. Returnerer kun FLICKR_* og R2_* - aldrig DATA_DIR."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*)$", line.rstrip("\n"))
            if not m:
                continue
            key, val = m.group(1), m.group(2).strip()
            if not (key.startswith("FLICKR_") or key.startswith("R2_")):
                continue
            if val[:1] in "'\"":
                q = val[0]
                end = val.find(q, 1)
                val = val[1:end] if end > 0 else val[1:]
            else:
                val = re.split(r"\s+#", val, 1)[0].strip()
            out[key] = val
    return out


class Ctx:
    """Holder app-modulet (din optimize_image mv.) og sti til databasen."""
    app = None
    db_path = None
    data_dir = None
    r2_env = {}


def init_ctx(args):
    env = load_env_file(os.path.join(ROOT, ".env"))
    for k, v in env.items():
        if k.startswith("FLICKR_"):
            os.environ.setdefault(k, v)
    # R2-vaerdier gemmes til senere; app.py maa IKKE selv oprette R2-forbindelse.
    for k in list(os.environ):
        if k.startswith("R2_"):
            Ctx.r2_env[k] = os.environ.pop(k)
    for k, v in env.items():
        if k.startswith("R2_"):
            Ctx.r2_env.setdefault(k, v)

    Ctx.data_dir = os.path.abspath(args.data_dir)
    os.environ["DATA_DIR"] = Ctx.data_dir
    os.environ["GALLERY_DEV"] = "1"          # spring serverens sikkerhedstjek over
    os.environ["AUTO_BACKUP"] = "0"
    os.environ.setdefault("ADMIN_PASSWORD_HASH", "unused-by-importer")   # holder app.py stille
    import app as app_module                  # genbruger din egen billedkode
    Ctx.app = app_module
    Ctx.db_path = app_module.DB_PATH


def make_r2():
    for k, v in Ctx.r2_env.items():
        os.environ[k] = v
    from storage import R2Storage, storage_from_env
    store = storage_from_env("/tmp/unused-uploads", "/tmp/unused-thumbs")
    if not isinstance(store, R2Storage):
        sys.exit("R2 er ikke sat op: R2_BUCKET m.fl. mangler i .env")
    return store


def db_connect():
    conn = sqlite3.connect(Ctx.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


STAGING_SQL = """
CREATE TABLE IF NOT EXISTS flickr_albums (
    flickr_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    photo_count INTEGER NOT NULL DEFAULT 0,
    excluded INTEGER NOT NULL DEFAULT 0,
    exclude_reason TEXT,
    local_album_id INTEGER
);
CREATE TABLE IF NOT EXISTS flickr_excluded (
    photo_id TEXT PRIMARY KEY,
    album_flickr_id TEXT
);
CREATE TABLE IF NOT EXISTS flickr_photos (
    flickr_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    date_taken TEXT,
    date_upload INTEGER,
    album_flickr_id TEXT,
    album_title TEXT NOT NULL DEFAULT '',
    source_url TEXT,
    source_label TEXT,
    width INTEGER,
    height INTEGER,
    category TEXT NOT NULL DEFAULT 'misc',
    category_reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    image_id INTEGER,
    done_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_flickr_photos_status ON flickr_photos(status);
"""


# ---------------------------------------------------------------------------
# Flickr API
# ---------------------------------------------------------------------------
class Flickr:
    def __init__(self, api_key, min_interval=1.1):
        self.key = api_key
        self.min_interval = min_interval     # ~3300 kald/time max
        self.last = 0.0
        self.calls = 0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "bus-car-gallery-importer/1.0"

    def call(self, method, **params):
        params.update(method=method, api_key=self.key, format="json", nojsoncallback=1)
        for attempt in range(1, 7):
            wait = self.min_interval - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self.calls += 1
            try:
                r = self.session.get(API_URL, params=params, timeout=(15, 60))
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                data = r.json()
            except (requests.RequestException, ValueError) as e:
                delay = min(60, 2 ** attempt)
                print(f"  ! {method}: {e} - proever igen om {delay}s ({attempt}/6)")
                time.sleep(delay)
                continue
            if data.get("stat") == "ok":
                return data
            code = data.get("code")
            if code in (105, 106):                # service midlertidigt nede
                time.sleep(min(60, 2 ** attempt))
                continue
            raise RuntimeError(f"Flickr {method} fejlede: {data.get('message')} (kode {code})")
        raise RuntimeError(f"Flickr {method} fejlede efter 6 forsoeg")

    def pages(self, method, container, item_key, **params):
        page = 1
        while True:
            data = self.call(method, page=page, per_page=PER_PAGE, **params)
            box = data[container]
            for item in box.get(item_key, []):
                yield item
            if page >= int(box.get("pages", 1)):
                return
            page += 1


def text(v):
    return v.get("_content", "") if isinstance(v, dict) else (v or "")


def pick_source(p):
    for label in SIZE_ORDER:
        url = p.get(f"url_{label}")
        if url:
            try:
                w, h = int(p.get(f"width_{label}", 0)), int(p.get(f"height_{label}", 0))
            except (TypeError, ValueError):
                w = h = 0
            return url, label, w, h
    return None, None, 0, 0


# ---------------------------------------------------------------------------
# Fase 1: metadata
# ---------------------------------------------------------------------------
def cmd_metadata(args):
    key = os.environ.get("FLICKR_API_KEY", "").strip()
    if not key or "PASTE" in key.upper():
        sys.exit("FLICKR_API_KEY mangler. Saet den i .env: FLICKR_API_KEY=din-noegle")
    fl = Flickr(key)
    conn = db_connect()
    conn.executescript(STAGING_SQL)
    user = args.user

    print("1/4 Henter albumliste ...")
    albums = {}
    for a in fl.pages("flickr.photosets.getList", "photosets", "photoset", user_id=user):
        albums[str(a["id"])] = {
            "title": text(a.get("title")), "description": text(a.get("description")),
            "count": int(a.get("photos", 0)) + int(a.get("videos", 0) or 0),
        }
    for aid in EXCLUDED_ALBUM_IDS:                 # med selv om de ikke staar i listen
        if aid not in albums:
            try:
                info = fl.call("flickr.photosets.getInfo", photoset_id=aid, user_id=user)["photoset"]
                albums[aid] = {"title": text(info.get("title")),
                               "description": text(info.get("description")),
                               "count": int(info.get("photos", 0))}
            except RuntimeError as e:
                print(f"  (kunne ikke slaa udelukket album {aid} op: {e})")
                albums[aid] = {"title": "", "description": "", "count": 0}

    excluded = {}
    for aid, a in albums.items():
        if aid in EXCLUDED_ALBUM_IDS:
            excluded[aid] = "fast liste (dit eget valg)"
        elif EXCLUDE_TITLE_RE.search(a["title"]) or EXCLUDE_TITLE_RE.search(a["description"]):
            excluded[aid] = 'titel/beskrivelse indeholder "ikke mine"'
    print(f"   {len(albums)} albums, heraf {len(excluded)} udelukket:")
    for aid, why in excluded.items():
        print(f"   x  {albums[aid]['title'] or '(ukendt titel)'}  [{aid}]  {albums[aid]['count']} billeder - {why}")

    print("2/4 Henter hvilke billeder der hoerer til de udelukkede albums ...")
    excluded_photos = {}
    for aid in excluded:
        for p in fl.pages("flickr.photosets.getPhotos", "photoset", "photo", photoset_id=aid, user_id=user):
            excluded_photos[str(p["id"])] = aid
    print(f"   {len(excluded_photos)} billeder er spaerret (uanset om de ogsaa ligger i andre albums)")

    print("3/4 Henter album-medlemskab for de oevrige albums ...")
    membership = {}                                 # photo id -> (album id, album title)
    included = [aid for aid in albums if aid not in excluded]
    for i, aid in enumerate(included, 1):
        if albums[aid]["count"] == 0:
            continue
        for p in fl.pages("flickr.photosets.getPhotos", "photoset", "photo", photoset_id=aid, user_id=user):
            membership.setdefault(str(p["id"]), (aid, albums[aid]["title"]))
        if i % 25 == 0:
            print(f"   {i}/{len(included)} albums ...")

    print("4/4 Henter alle offentlige billeder med metadata ...")
    expected = None
    try:
        info = fl.call("flickr.people.getInfo", user_id=user)["person"]
        expected = int(info["photos"]["count"]["_content"])
    except Exception:
        pass

    conn.execute("DELETE FROM flickr_excluded")
    conn.executemany("INSERT INTO flickr_excluded(photo_id, album_flickr_id) VALUES (?, ?)",
                     list(excluded_photos.items()))
    for aid, a in albums.items():
        conn.execute(
            """INSERT INTO flickr_albums(flickr_id, title, description, photo_count, excluded, exclude_reason)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(flickr_id) DO UPDATE SET title=excluded.title,
                 description=excluded.description, photo_count=excluded.photo_count,
                 excluded=excluded.excluded, exclude_reason=excluded.exclude_reason""",
            (aid, a["title"], a["description"], a["count"], 1 if aid in excluded else 0, excluded.get(aid)))
    conn.commit()

    seen = skipped_excl = skipped_video = no_url = 0
    labels = Counter()
    normalize_tags = Ctx.app.normalize_tags
    for p in fl.pages("flickr.people.getPublicPhotos", "photos", "photo", user_id=user, extras=EXTRAS):
        pid = str(p["id"])
        seen += 1
        if pid in excluded_photos:
            skipped_excl += 1
            continue
        if p.get("media") == "video":
            skipped_video += 1
            continue
        url, label, w, h = pick_source(p)
        if not url:
            no_url += 1
            continue
        labels[label] += 1
        album_id, album_title = membership.get(pid, (None, ""))
        title = text(p.get("title")).strip()
        desc = text(p.get("description")).strip()
        tags = normalize_tags((p.get("tags") or "").replace(" ", ", "))
        taken = (p.get("datetaken") or "")[:10]
        if p.get("datetakenunknown") == "1" or not re.match(r"^\d{4}-\d{2}-\d{2}$", taken) or taken.startswith("0000"):
            taken = None
        cat, why = flickr_rules.classify(album_title, title, tags, desc)
        conn.execute(
            """INSERT INTO flickr_photos(flickr_id, title, description, tags, date_taken, date_upload,
                   album_flickr_id, album_title, source_url, source_label, width, height,
                   category, category_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(flickr_id) DO UPDATE SET title=excluded.title, description=excluded.description,
                   tags=excluded.tags, date_taken=excluded.date_taken, date_upload=excluded.date_upload,
                   album_flickr_id=excluded.album_flickr_id, album_title=excluded.album_title,
                   source_url=excluded.source_url, source_label=excluded.source_label,
                   width=excluded.width, height=excluded.height""",
            (pid, title, desc, tags, taken, int(p.get("dateupload") or 0), album_id, album_title,
             url, label, w, h, cat, why))
        if seen % 2000 == 0:
            conn.commit()
            print(f"   {seen} billeder ...")
    conn.commit()

    # Sikkerhedsnet: ryd alt der skulle vaere udelukket men alligevel er i koeen
    conn.execute("""DELETE FROM flickr_photos WHERE status != 'done'
                    AND flickr_id IN (SELECT photo_id FROM flickr_excluded)""")
    conn.commit()
    already = conn.execute("""SELECT COUNT(*) FROM flickr_photos WHERE status='done'
                              AND flickr_id IN (SELECT photo_id FROM flickr_excluded)""").fetchone()[0]

    print("\n=== Faerdig ===")
    print(f"API-kald brugt: {fl.calls}")
    print(f"Offentlige billeder set: {seen}" + (f" (Flickr siger {expected} offentlige)" if expected else ""))
    if expected and seen < expected:
        print(f"  ! {expected - seen} faerre end Flickr angiver. Uden login ser API'et kun 'safe' indhold -"
              " moderate/restricted billeder mangler. Saet dem til 'safe' paa Flickr, eller sig til, saa laver vi OAuth-login.")
    print(f"Sprunget over (udelukkede albums): {skipped_excl}")
    print(f"Sprunget over (video): {skipped_video}   uden download-URL: {no_url}")
    print("Kilde-stoerrelser: " + ", ".join(f"{k}={v}" for k, v in labels.most_common()))
    if labels.get("o", 0) < 0.8 * max(1, sum(labels.values())):
        print("  Tip: faa 'o' (original) med ved at saette Flickr -> Indstillinger -> Privatliv ->"
              " 'Hvem kan downloade dine billeder' til 'Alle', og koer metadata igen.")
    if already:
        print(f"  ! {already} allerede importerede billeder hoerer til udelukkede albums - koer 'purge-excluded'.")
    cmd_report(args, conn=conn)


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------
def cmd_report(args, conn=None):
    conn = conn or db_connect()
    print("\n--- Status ---")
    for row in conn.execute("SELECT status, COUNT(*) n FROM flickr_photos GROUP BY status"):
        print(f"  {row['status']:8} {row['n']}")
    print("--- Kategorier ---")
    for row in conn.execute("SELECT category, COUNT(*) n FROM flickr_photos GROUP BY category ORDER BY n DESC"):
        print(f"  {row['category']:6} {row['n']}")
    print("--- Udelukkede albums ---")
    for row in conn.execute("SELECT flickr_id, title, photo_count, exclude_reason FROM flickr_albums WHERE excluded=1"):
        print(f"  x {row['title'] or '(ukendt)'} [{row['flickr_id']}] {row['photo_count']} billeder - {row['exclude_reason']}")
    print("--- 15 stoerste albums (importeres) -> hyppigste kategori ---")
    rows = conn.execute("""SELECT album_title, COUNT(*) n FROM flickr_photos
                           WHERE album_flickr_id IS NOT NULL GROUP BY album_flickr_id
                           ORDER BY n DESC LIMIT 15""").fetchall()
    for r in rows:
        cats = conn.execute("SELECT category, COUNT(*) n FROM flickr_photos WHERE album_title=? GROUP BY category ORDER BY n DESC",
                            (r["album_title"],)).fetchall()
        print(f"  {r['album_title'][:40]:40} {r['n']:5}  " + ", ".join(f"{c['category']}={c['n']}" for c in cats[:3]))
    misc = conn.execute("SELECT title, album_title, tags FROM flickr_photos WHERE category='misc' ORDER BY RANDOM() LIMIT 8").fetchall()
    if misc:
        print("--- Eksempler paa 'misc' (hjaelper dig at forbedre tools/flickr_rules.py) ---")
        for m in misc:
            print(f"  [{m['album_title'][:25]}] {m['title'][:50]}  ({m['tags'][:40]})")


# ---------------------------------------------------------------------------
# Fase 2: billeder
# ---------------------------------------------------------------------------
_local = threading.local()


def http():
    s = getattr(_local, "s", None)
    if s is None:
        s = _local.s = requests.Session()
        s.headers["User-Agent"] = "bus-car-gallery-importer/1.0"
    return s


def download(url, dst):
    last = None
    for attempt in range(1, 6):
        try:
            with http().get(url, stream=True, timeout=(15, 90)) as r:
                if r.status_code in (403, 404, 410):
                    raise PermissionError(f"HTTP {r.status_code}")   # varigt - proev ikke igen
                r.raise_for_status()
                expected = int(r.headers.get("Content-Length", 0))
                got = 0
                with open(dst, "wb") as fh:
                    for chunk in r.iter_content(256 * 1024):
                        fh.write(chunk)
                        got += len(chunk)
                if got == 0 or (expected and got != expected):
                    raise IOError(f"ufuldstaendig download ({got}/{expected} bytes)")
                return got
        except PermissionError:
            raise
        except (requests.RequestException, IOError) as e:
            last = e
            time.sleep(min(30, 2 ** attempt))
    raise IOError(f"download fejlede: {last}")


def ext_of(url):
    m = re.search(r"\.([A-Za-z0-9]{3,4})(?:\?|$)", url or "")
    e = (m.group(1).lower() if m else "jpg")
    return "jpg" if e == "jpeg" else e


def process_one(row, r2, tmp_dir):
    """Download -> verificer -> optimer -> thumbnail -> upload. Ingen DB her."""
    from PIL import Image as PILImage
    app = Ctx.app
    fid = row["flickr_id"]
    ext = ext_of(row["source_url"])
    if ext not in app.ALLOWED_EXTENSIONS:
        ext = "jpg"
    raw = os.path.join(tmp_dir, f"fl{fid}.{ext}")
    opt = os.path.join(tmp_dir, f"fl{fid}_opt.{ext}")
    thumb = os.path.join(tmp_dir, f"fl{fid}_t.jpg")
    fname, tname = f"fl{fid}.{ext}", f"fl{fid}_t.jpg"
    try:
        size_in = download(row["source_url"], raw)
        with PILImage.open(raw) as probe:
            probe.verify()
        taken = row["date_taken"] or app.extract_exif_date(raw)
        full = raw
        try:
            if app.optimize_image(raw, opt, ext):
                full = opt
        except Exception:
            full = raw                               # som i appen: gem originalen
        app.make_thumbnail(full, thumb)
        size_out = os.path.getsize(full)
        # Upload. Faste noegler -> en gentagelse overskriver blot samme fil.
        r2.put("uploads", fname, full)               # put() sletter den lokale fil bagefter
        r2.put("thumbnails", tname, thumb)
        return {"ok": True, "filename": fname, "thumbnail": tname, "taken": taken,
                "bytes_in": size_in, "bytes_out": size_out}
    except PermissionError as e:
        return {"ok": False, "permanent": True, "error": str(e)}
    except Exception as e:                           # noqa: BLE001 - alt skal fanges pr. billede
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    finally:
        for p in (raw, opt, thumb):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


def local_album_id(conn, flickr_album_id):
    if not flickr_album_id:
        return None
    row = conn.execute("SELECT title, local_album_id, excluded FROM flickr_albums WHERE flickr_id=?",
                       (flickr_album_id,)).fetchone()
    if row is None or row["excluded"]:
        return None
    if row["local_album_id"]:
        return row["local_album_id"]
    base = (row["title"] or "Uden navn").strip()[:100] or "Uden navn"
    name, n = base, 1
    while True:
        try:
            cur = conn.execute("INSERT INTO albums(name, created_at) VALUES (?, ?)",
                               (name, Ctx.app.utc_now_iso()))
            break
        except sqlite3.IntegrityError:
            existing = conn.execute("SELECT id FROM albums WHERE name=?", (name,)).fetchone()
            used = conn.execute("SELECT 1 FROM flickr_albums WHERE local_album_id=?",
                                (existing["id"],)).fetchone() if existing else True
            if existing and not used:                # fx dit eget testalbum med samme navn
                conn.execute("UPDATE flickr_albums SET local_album_id=? WHERE flickr_id=?",
                             (existing["id"], flickr_album_id))
                return existing["id"]
            n += 1
            name = f"{base} ({n})"
    conn.execute("UPDATE flickr_albums SET local_album_id=? WHERE flickr_id=?",
                 (cur.lastrowid, flickr_album_id))
    return cur.lastrowid


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024


def cmd_images(args):
    conn = db_connect()
    conn.executescript(STAGING_SQL)
    if conn.execute("SELECT COUNT(*) FROM flickr_photos").fetchone()[0] == 0:
        sys.exit("Ingen metadata endnu. Koer foerst:  python tools/import_flickr.py metadata")

    if args.retry_failed:
        conn.execute("UPDATE flickr_photos SET attempts=0 WHERE status='failed'")
        conn.commit()

    where = """status IN ('pending','failed') AND attempts < ?
               AND NOT EXISTS (SELECT 1 FROM flickr_excluded e WHERE e.photo_id = flickr_photos.flickr_id)
               AND (album_flickr_id IS NULL OR NOT EXISTS
                    (SELECT 1 FROM flickr_albums a WHERE a.flickr_id = flickr_photos.album_flickr_id AND a.excluded = 1))"""
    todo = conn.execute(f"SELECT COUNT(*) FROM flickr_photos WHERE {where}", (args.max_attempts,)).fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM flickr_photos WHERE status='done'").fetchone()[0]
    excl = conn.execute("SELECT COUNT(*) FROM flickr_excluded").fetchone()[0]
    print(f"Allerede importeret: {done}   mangler: {todo}   spaerret (ikke mine): {excl}")
    for r in conn.execute("SELECT title, flickr_id, photo_count FROM flickr_albums WHERE excluded=1"):
        print(f"  x udelukket: {r['title'] or '(ukendt)'} [{r['flickr_id']}] ({r['photo_count']} billeder)")
    if todo == 0:
        print("Intet at goere.")
        return
    if not args.yes:
        if input("Fortsaet? [j/N] ").strip().lower() not in ("j", "ja", "y", "yes"):
            return

    r2 = make_r2()
    tmp_dir = os.path.join(Ctx.data_dir, "tmp", "flickr")
    os.makedirs(tmp_dir, exist_ok=True)
    order = "date_taken, date_upload" if args.order == "taken" else "date_upload, flickr_id"
    limit = f"LIMIT {int(args.limit)}" if args.limit else ""
    rows = conn.execute(f"SELECT * FROM flickr_photos WHERE {where} ORDER BY {order} {limit}",
                        (args.max_attempts,)).fetchall()

    signal.signal(signal.SIGINT, lambda *_: (_stop.set(), signal.signal(signal.SIGINT, signal.default_int_handler),
                                             print("\nStopper efter nuvaerende gruppe ... (Ctrl-C igen = hard stop)")))

    t0 = time.time()
    ok = failed = bytes_in = bytes_out = 0
    window = max(args.workers * 3, 4)
    total = len(rows)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for start in range(0, total, window):
            if _stop.is_set():
                break
            batch = rows[start:start + window]
            # Ekstra sikkerhedstjek lige foer upload: er noget i gruppen spaerret?
            ids = [b["flickr_id"] for b in batch]
            blocked = {r[0] for r in conn.execute(
                f"SELECT photo_id FROM flickr_excluded WHERE photo_id IN ({','.join('?' * len(ids))})", ids)}
            batch = [b for b in batch if b["flickr_id"] not in blocked]
            results = list(ex.map(lambda b: process_one(b, r2, tmp_dir), batch))   # bevarer raekkefoelgen
            for row, res in zip(batch, results):
                if res["ok"]:
                    try:
                        with conn:                  # EN transaktion: billede + checkpoint
                            album_id = local_album_id(conn, row["album_flickr_id"])
                            uploaded = datetime.fromtimestamp(row["date_upload"] or time.time(), timezone.utc
                                                              ).replace(tzinfo=None).isoformat()
                            cur = conn.execute(
                                """INSERT INTO images (title, tags, filename, thumbnail, uploaded_at, album_id,
                                       category, caption, photo_taken_date)
                                   VALUES (?,?,?,?,?,?,?,?,?)""",
                                ((row["title"] or row["flickr_id"])[:200], row["tags"], res["filename"],
                                 res["thumbnail"], uploaded, album_id, row["category"],
                                 (row["description"] or "")[:2000], res["taken"]))
                            conn.execute(
                                """UPDATE flickr_photos SET status='done', image_id=?, error=NULL, done_at=?
                                   WHERE flickr_id=?""",
                                (cur.lastrowid, Ctx.app.utc_now_iso(), row["flickr_id"]))
                        ok += 1
                        bytes_in += res["bytes_in"]
                        bytes_out += res["bytes_out"]
                        continue
                    except Exception as e:           # noqa: BLE001
                        res = {"ok": False, "error": f"DB: {e}"[:300]}
                failed += 1
                attempts = args.max_attempts if res.get("permanent") else row["attempts"] + 1
                with conn:
                    conn.execute("UPDATE flickr_photos SET status='failed', attempts=?, error=? WHERE flickr_id=?",
                                 (attempts, res["error"], row["flickr_id"]))
                print(f"  ! {row['flickr_id']} {row['title'][:30]!r}: {res['error']}")
            n = ok + failed
            el = time.time() - t0
            rate = n / el if el else 0
            eta = (total - n) / rate if rate else 0
            saved = (1 - bytes_out / bytes_in) * 100 if bytes_in else 0
            print(f"[{n}/{total}] ok={ok} fejl={failed}  hentet {fmt_bytes(bytes_in)} -> R2 {fmt_bytes(bytes_out)}"
                  f" (-{saved:.0f}%)  {rate * 60:.0f}/min  ca. {eta / 3600:.1f} t tilbage", flush=True)

    print(f"\nStoppet. ok={ok}, fejl={failed}." + ("  Koer samme kommando igen for at fortsaette." if _stop.is_set() or failed else ""))
    if failed:
        print("Fejlede billeder proeves automatisk igen ved naeste koersel (op til --max-attempts).")


# ---------------------------------------------------------------------------
# Vedligehold
# ---------------------------------------------------------------------------
def cmd_reclassify(args):
    conn = db_connect()
    changed = 0
    rows = conn.execute("""SELECT p.flickr_id, p.album_title, p.title, p.tags, p.description, p.category,
                                  p.status, p.image_id, i.category AS img_cat
                           FROM flickr_photos p LEFT JOIN images i ON i.id = p.image_id""").fetchall()
    with conn:
        for r in rows:
            cat, why = flickr_rules.classify(r["album_title"], r["title"], r["tags"], r["description"])
            if cat == r["category"]:
                continue
            if r["status"] == "done":
                if r["img_cat"] != r["category"]:     # du har rettet den i hånden - roer den ikke
                    continue
                conn.execute("UPDATE images SET category=? WHERE id=?", (cat, r["image_id"]))
            conn.execute("UPDATE flickr_photos SET category=?, category_reason=? WHERE flickr_id=?",
                         (cat, why, r["flickr_id"]))
            changed += 1
    print(f"{changed} billeder skiftede kategori.")
    cmd_report(args, conn=conn)


def cmd_purge_excluded(args):
    conn = db_connect()
    rows = conn.execute("""SELECT p.flickr_id, p.image_id, i.filename, i.thumbnail FROM flickr_photos p
                           JOIN images i ON i.id = p.image_id
                           WHERE p.flickr_id IN (SELECT photo_id FROM flickr_excluded)""").fetchall()
    print(f"{len(rows)} importerede billeder hoerer til udelukkede albums.")
    if not rows:
        return
    if not args.yes and input("Slet dem fra R2 og databasen? [j/N] ").strip().lower() not in ("j", "ja"):
        return
    r2 = make_r2()
    for r in rows:
        r2.delete("uploads", r["filename"])
        if r["thumbnail"] != r["filename"]:
            r2.delete("thumbnails", r["thumbnail"])
        with conn:
            conn.execute("DELETE FROM comments WHERE image_id=?", (r["image_id"],))
            conn.execute("DELETE FROM images WHERE id=?", (r["image_id"],))
            conn.execute("DELETE FROM flickr_photos WHERE flickr_id=?", (r["flickr_id"],))
    print("Slettet.")


def cmd_verify(args):
    conn = db_connect()
    r2 = make_r2()
    print("Lister R2 ...")
    existing = r2.list_keys("uploads/") | r2.list_keys("thumbnails/")
    rows = conn.execute("""SELECT p.flickr_id, i.filename, i.thumbnail FROM flickr_photos p
                           JOIN images i ON i.id=p.image_id WHERE p.status='done'""").fetchall()
    missing = [(r["flickr_id"], k) for r in rows
               for k in (f"uploads/{r['filename']}", f"thumbnails/{r['thumbnail']}") if k not in existing]
    leaked = conn.execute("""SELECT COUNT(*) FROM flickr_photos
                             WHERE flickr_id IN (SELECT photo_id FROM flickr_excluded)""").fetchone()[0]
    print(f"{len(rows)} importerede billeder tjekket, {len(missing)} filer mangler i R2.")
    print(f"Udelukkede billeder i importen: {leaked}  (skal vaere 0)")
    for fid, key in missing[:10]:
        print(f"  mangler: {key} (flickr {fid})")
    if missing:
        print("Saet dem til pending:  UPDATE flickr_photos SET status='pending' WHERE flickr_id IN (...) og koer 'images' igen.")
    if missing or leaked:
        sys.exit(1)
    print("OK.")


def cmd_push_db(args):
    """Laegger en konsistent kopi af databasen i R2 som backups/gallery-import.db.
    Paa serveren: python tools/restore_db.py backups/gallery-import.db --force  (derefter genstart)."""
    import tempfile
    r2 = make_r2()
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "copy.db")
        src, dst = sqlite3.connect(Ctx.db_path), sqlite3.connect(copy)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        r2.upload_raw("backups/gallery-import.db", copy, content_type="application/octet-stream", cache=False)
    print("Uploadet til R2: backups/gallery-import.db\n"
          "Naeste skridt paa Fly:\n"
          "  fly ssh console -a bus-car-gallery-2\n"
          "  python tools/restore_db.py backups/gallery-import.db --force\n"
          "  exit  og derefter:  fly machine restart -a bus-car-gallery-2")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.path.join(ROOT, "data"), help="lokal datamappe (indeholder gallery.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("metadata", help="fase 1: hent alle metadata fra Flickr")
    m.add_argument("--user", default=DEFAULT_USER_ID)
    m.set_defaults(fn=cmd_metadata)

    i = sub.add_parser("images", help="fase 2: download, komprimer, upload til R2")
    i.add_argument("--workers", type=int, default=4, help="samtidige billeder (standard 4)")
    i.add_argument("--limit", type=int, help="kun de foerste N (til en proeve, fx --limit 20)")
    i.add_argument("--order", choices=["upload", "taken"], default="upload",
                   help="raekkefoelge; galleriet viser hoejeste id foerst, saa den sidst importerede staar oeverst")
    i.add_argument("--max-attempts", type=int, default=5)
    i.add_argument("--retry-failed", action="store_true", help="nulstil fejltaeller og proev alle fejlede igen")
    i.add_argument("--yes", action="store_true", help="spoerg ikke om bekraeftelse")
    i.set_defaults(fn=cmd_images)

    for name, fn, h in (("report", cmd_report, "vis status og kategorier"),
                        ("reclassify", cmd_reclassify, "koer kategori-reglerne igen"),
                        ("verify", cmd_verify, "tjek at alt ligger i R2"),
                        ("push-db", cmd_push_db, "laeg databasen i R2 til flytning til Fly")):
        sub.add_parser(name, help=h).set_defaults(fn=fn)
    p = sub.add_parser("purge-excluded", help="slet importerede billeder fra udelukkede albums")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_purge_excluded)

    args = ap.parse_args()
    init_ctx(args)
    args.fn(args)


if __name__ == "__main__":
    main()
