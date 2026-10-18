#!/usr/bin/env python3
"""Flickr -> Cloudflare R2 + SQLite import (genoptagelses-sikker).

Foerste gang (i denne rækkefølge):

    python tools/import_flickr.py metadata          # fase 1: ~2 min, kun Flickr API

Fra Flickrs eksport (zip-filer; ingen CDN-graense) - anbefalet naar Flickr blokerer:
    python tools/import_flickr.py export-metadata --source ~/Downloads/flickr-export
    python tools/import_flickr.py images --source ~/Downloads/flickr-export --workers 4
    python tools/import_flickr.py report            # tjek albums, udelukkelser, kategorier
    caffeinate -i python tools/import_flickr.py images    # fase 2: timer, kan afbrydes og genoptages
    python tools/import_flickr.py verify            # tjek at alt ligger i R2
    python tools/import_flickr.py push-db           # laegger databasen i R2 (se bunden af filen)

Andre kommandoer:
    reclassify        koer kategori-reglerne igen - viser kun en forhaandsvisning; gem med --apply
    purge-excluded    slet evt. allerede importerede billeder fra udelukkede albums
    link-albums       laeg ALLE albums paa billederne (kraever --source)
    check-urls        test om billedernes adresser svarer (404 = filen mangler, 429 = begraenset)
    repair-files      genskab filer der mangler i R2 (kraever --source; databasen aendres ikke)
    reset-import      slet ALT importeret fra Flickr (R2-filer, billeder, albums, koe) og start forfra

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
import shutil
import threading
import time
import zipfile
import json
import calendar
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


class RateLimited(Exception):
    def __init__(self, retry_after=None):
        super().__init__("429/503 fra Flickrs CDN")
        self.retry_after = retry_after


def nap(seconds):
    """Soev i smaa stykker, saa Ctrl-C virker."""
    end = time.time() + seconds
    while time.time() < end and not _stop.is_set():
        time.sleep(min(1.0, end - time.time()))


class Gate:
    """Faelles bremse for alle traade: jaevnt tempo + automatisk pause ved 429."""
    MAX_INTERVAL = 4.0

    def __init__(self, base):
        self.base = base
        self.interval = base
        self.next = 0.0
        self.cool_until = 0.0
        self.streak = 0
        self.ok_count = 0
        self.cooldowns = 0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            t = max(self.next, self.cool_until, now)
            self.next = t + self.interval
        nap(t - now)

    def penalize(self, retry_after=None):
        with self.lock:
            now = time.time()
            if now >= self.cool_until:               # ny blokering (ikke 4 traade om samme)
                self.streak += 1
                self.cooldowns += 1
                cool = retry_after or min(900, 60 * 2 ** (self.streak - 1))
                self.cool_until = now + cool
                if self.streak == 1:                  # kun en gang pr. blokering, ikke ved hver gentagelse
                    self.interval = min(max(self.interval * 1.5, 1.0), self.MAX_INTERVAL)
                print(f"  ~ Flickrs CDN siger 'for hurtigt' - pauser {cool:.0f}s "
                      f"(tempo: 1 billede pr. {self.interval:.1f}s)", flush=True)
            return max(0.0, self.cool_until - now)

    def success(self):
        with self.lock:
            self.streak = 0
            self.ok_count += 1
            if self.ok_count % 10 == 0 and self.interval > self.base:
                self.interval = max(self.base, self.interval * 0.75)


GATE = Gate(1.5)
MAX_COOLDOWNS = 6          # efter 6 pauser i traek gives op for denne koersel (intet markeres som fejl)


def download(url, dst):
    last = None
    for attempt in range(1, 6):
        try:
            with http().get(url, stream=True, timeout=(15, 90)) as r:
                if r.status_code in (403, 404, 410):
                    raise PermissionError(f"HTTP {r.status_code}")   # varigt - proev ikke igen
                if r.status_code in (429, 503):
                    ra = r.headers.get("Retry-After", "")
                    raise RateLimited(int(ra) if ra.isdigit() else None)
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
        except (PermissionError, RateLimited):
            raise
        except (requests.RequestException, IOError) as e:
            last = e
            time.sleep(min(30, 2 ** attempt))
    raise IOError(f"download fejlede: {last}")


def ext_of(url):
    m = re.search(r"\.([A-Za-z0-9]{3,4})(?:\?|$)", url or "")
    e = (m.group(1).lower() if m else "jpg")
    return "jpg" if e == "jpeg" else e


def raw_path(row, tmp_dir):
    ext = ext_of(row["source_url"])
    if ext not in Ctx.app.ALLOWED_EXTENSIONS:
        ext = "jpg"
    return os.path.join(tmp_dir, f"fl{row['flickr_id']}.{ext}"), ext


UPLOAD_POOL = None          # saettes i cmd_images: lader hovedbillede og thumbnail uploade samtidig


def process_file(row, r2, tmp_dir, fetch):
    """fetch(raw) laegger kildefilen paa disken -> verificer -> optimer -> thumbnail -> upload.
    Ingen DB her. fetch kan returnere en dict ({'deferred': True}) for at afbryde.
    Resultatet indeholder 't' = sekunder brugt pr. trin, saa man kan se hvad der er flaskehalsen."""
    from PIL import Image as PILImage
    app = Ctx.app
    fid = row["flickr_id"]
    raw, ext = raw_path(row, tmp_dir)
    opt = os.path.join(tmp_dir, f"fl{fid}_opt.{ext}")
    thumb = os.path.join(tmp_dir, f"fl{fid}_t.jpg")
    fname, tname = f"fl{fid}.{ext}", f"fl{fid}_t.jpg"
    t = {}
    clock = time.perf_counter
    try:
        t0 = clock()
        size_in = fetch(raw)
        if isinstance(size_in, dict):
            return size_in
        t["hent/udpak"] = clock() - t0

        t0 = clock()
        with PILImage.open(raw) as probe:
            probe.verify()
        taken = row["date_taken"] or app.extract_exif_date(raw)
        t["tjek"] = clock() - t0

        t0 = clock()
        full = raw
        try:
            if app.optimize_image(raw, opt, ext):
                full = opt
        except Exception:
            full = raw                               # som i appen: gem originalen
        t["optimer"] = clock() - t0

        t0 = clock()
        app.make_thumbnail(full, thumb)
        t["thumbnail"] = clock() - t0
        size_out = os.path.getsize(full)

        # Upload. Faste noegler -> en gentagelse overskriver blot samme fil.
        t0 = clock()
        if UPLOAD_POOL is not None:
            f_full = UPLOAD_POOL.submit(r2.put, "uploads", fname, full)   # put() sletter lokal fil bagefter
            r2.put("thumbnails", tname, thumb)
            f_full.result()
        else:
            r2.put("uploads", fname, full)
            r2.put("thumbnails", tname, thumb)
        t["upload"] = clock() - t0
        return {"ok": True, "filename": fname, "thumbnail": tname, "taken": taken,
                "bytes_in": size_in, "bytes_out": size_out, "t": t}
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


def process_one(row, r2, tmp_dir):
    """Download fra Flickrs CDN (med faelles bremse)."""
    def fetch(raw):
        for _ in range(MAX_COOLDOWNS):
            if _stop.is_set():
                return {"ok": False, "deferred": True}
            GATE.wait()
            try:
                size = download(row["source_url"], raw)
                GATE.success()
                return size
            except RateLimited as e:
                GATE.penalize(e.retry_after)
        _stop.set()                                   # CDN'en bliver ved - stop paent, intet er markeret som fejl
        return {"ok": False, "deferred": True, "blocked": True}
    return process_file(row, r2, tmp_dir, fetch)


def process_local(row, r2, tmp_dir, ref):
    """Pakker billedet ud af zip-filen (i denne traad) og behandler det."""
    def fetch(raw):
        extract_member(ref, raw)
        return os.path.getsize(raw)
    return process_file(row, r2, tmp_dir, fetch)


# ---------------------------------------------------------------------------
# Flickr-eksport (zip-filer) som kilde
# ---------------------------------------------------------------------------
PHOTO_JSON_RE = re.compile(r"^photo_(\d+)\.json$")
IMG_RE = re.compile(r"_(\d+)_o\.([A-Za-z0-9]{3,4})$")
IMG_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "tif", "tiff"}
_tl_zips = threading.local()
MAX_OPEN_ZIPS = 6          # pr. traad - macOS tillader kun 256 aabne filer som standard


def _zip(path):
    """ZipFile-haandtag er IKKE traadsikre, saa hver traad har sine egne (og kun nogle faa ad gangen)."""
    cache = getattr(_tl_zips, "c", None)
    if cache is None:
        cache = _tl_zips.c = {}
    if path in cache:
        zf = cache.pop(path)
        cache[path] = zf                              # frisk op (LRU)
        return zf
    while len(cache) >= MAX_OPEN_ZIPS:
        old = next(iter(cache))
        cache.pop(old).close()
    cache[path] = zipfile.ZipFile(path)
    return cache[path]


def scan_source(source):
    """Gennemsoeger en mappe med eksport-zipfiler (og/eller udpakkede filer).
    Returnerer (albums_ref, photo_json_refs, images{id: ref}). ref = (zip-sti eller None, medlemsnavn/filsti)."""
    albums_ref, photo_json, images = None, [], {}

    def handle(zp, name):
        nonlocal albums_ref
        base = name.rsplit("/", 1)[-1]
        if base == "albums.json":
            albums_ref = albums_ref or (zp, name)
        elif PHOTO_JSON_RE.match(base):
            photo_json.append((zp, name))
        else:
            m = IMG_RE.search(base)
            if m and m.group(2).lower() in IMG_EXTS:
                images.setdefault(m.group(1), (zp, name))

    n_zips = 0
    for dirpath, _, files in os.walk(source):
        for fn in sorted(files):
            path = os.path.join(dirpath, fn)
            if fn.lower().endswith(".zip"):
                n_zips += 1
                try:
                    for n in _zip(path).namelist():
                        if not n.endswith("/"):
                            handle(path, n)
                except zipfile.BadZipFile:
                    print(f"  ! {fn} er ikke en gyldig zip-fil (download den igen?)")
            elif not fn.startswith("."):
                handle(None, path)
    print(f"Gennemsoegte {n_zips} zip-filer: {len(photo_json)} billed-JSON'er, {len(images)} billedfiler")
    return albums_ref, photo_json, images


def read_ref(ref):
    zp, name = ref
    if zp:
        with _zip(zp).open(name) as fh:
            return fh.read()
    with open(name, "rb") as fh:
        return fh.read()


def extract_member(ref, dst):
    zp, name = ref
    if zp:
        with _zip(zp).open(name) as src, open(dst, "wb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
    else:
        shutil.copyfile(name, dst)


def cmd_export_metadata(args):
    """Bygger koeen ud fra eksportens JSON-filer (titel, beskrivelse, tags, dato, album, privatliv)."""
    conn = db_connect()
    conn.executescript(STAGING_SQL)
    albums_ref, photo_refs, images = scan_source(args.source)
    if not albums_ref:
        sys.exit("Fandt ingen albums.json. Peg --source paa mappen med ALLE eksport-zipfilerne.")
    if not photo_refs:
        sys.exit("Fandt ingen photo_*.json. Er konto-data-zippen (den med JSON-filerne) i mappen?")

    albums = json.loads(read_ref(albums_ref).decode("utf-8"))["albums"]
    excluded = {}
    for a in albums:
        if a["id"] in EXCLUDED_ALBUM_IDS:
            excluded[a["id"]] = "fast liste (dit eget valg)"
        elif EXCLUDE_TITLE_RE.search(a.get("title", "")) or EXCLUDE_TITLE_RE.search(a.get("description", "")):
            excluded[a["id"]] = 'titel/beskrivelse indeholder "ikke mine"'
    for aid in EXCLUDED_ALBUM_IDS:
        if aid not in excluded:
            print(f"  ! udelukket album {aid} findes ikke i albums.json - er det det rigtige eksport?")
    excluded_photos, album_photo_ids = {}, set()
    for a in albums:
        album_photo_ids.update(a.get("photos", []))
        if a["id"] in excluded:
            for pid in a.get("photos", []):
                excluded_photos[str(pid)] = a["id"]
    print(f"{len(albums)} albums, heraf {len(excluded)} udelukket:")
    for a in albums:
        if a["id"] in excluded:
            print(f"   x  {a['title']!r} [{a['id']}]  {a['photo_count']} billeder - {excluded[a['id']]}")
    print(f"   {len(excluded_photos)} billeder er spaerret (uanset om de ogsaa ligger i andre albums)")

    conn.execute("DELETE FROM flickr_excluded")
    conn.executemany("INSERT INTO flickr_excluded(photo_id, album_flickr_id) VALUES (?, ?)", list(excluded_photos.items()))
    for a in albums:
        conn.execute(
            """INSERT INTO flickr_albums(flickr_id, title, description, photo_count, excluded, exclude_reason)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(flickr_id) DO UPDATE SET title=excluded.title, description=excluded.description,
                 photo_count=excluded.photo_count, excluded=excluded.excluded, exclude_reason=excluded.exclude_reason""",
            (a["id"], a.get("title", ""), a.get("description", ""), int(a.get("photo_count") or 0),
             1 if a["id"] in excluded else 0, excluded.get(a["id"])))
    conn.commit()

    normalize_tags = Ctx.app.normalize_tags
    seen, private, skipped_excl, queued = set(), 0, 0, 0
    for i, ref in enumerate(photo_refs, 1):
        d = json.loads(read_ref(ref).decode("utf-8"))
        pid = str(d["id"])
        seen.add(pid)
        if d.get("privacy") != "public":
            private += 1
            continue
        if pid in excluded_photos or any(al["id"] in excluded for al in d.get("albums", [])):
            skipped_excl += 1
            continue
        first = next(iter(d.get("albums", [])), None)
        album_id, album_title = (first["id"], first["title"]) if first else (None, "")
        title = (d.get("name") or "").strip()
        desc = (d.get("description") or "").strip()
        tags = normalize_tags(", ".join(t["tag"] for t in d.get("tags", [])))
        taken = (d.get("date_taken") or "")[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", taken) or taken.startswith("0000"):
            taken = None
        try:
            up = calendar.timegm(time.strptime(d["date_imported"], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            up = 0
        cat, why = flickr_rules.classify([al["title"] for al in d.get("albums", [])], title, tags, desc)
        conn.execute(
            """INSERT INTO flickr_photos(flickr_id, title, description, tags, date_taken, date_upload,
                   album_flickr_id, album_title, source_url, source_label, category, category_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(flickr_id) DO UPDATE SET title=excluded.title, description=excluded.description,
                   tags=excluded.tags, date_taken=excluded.date_taken, date_upload=excluded.date_upload,
                   album_flickr_id=excluded.album_flickr_id, album_title=excluded.album_title,
                   source_url=excluded.source_url, source_label='export',
                   category=CASE WHEN flickr_photos.status='done' THEN flickr_photos.category ELSE excluded.category END,
                   category_reason=CASE WHEN flickr_photos.status='done' THEN flickr_photos.category_reason
                                        ELSE excluded.category_reason END""",
            (pid, title, desc, tags, taken, up, album_id, album_title, d.get("original"), "export", cat, why))
        queued += 1
        if i % 2000 == 0:
            conn.commit()
    conn.commit()

    conn.execute("""DELETE FROM flickr_photos WHERE status != 'done'
                    AND flickr_id IN (SELECT photo_id FROM flickr_excluded)""")
    conn.commit()
    if args.update_imported:
        n = 0
        with conn:
            for r in conn.execute("""SELECT image_id, title, tags, description FROM flickr_photos
                                     WHERE status='done' AND image_id IS NOT NULL""").fetchall():
                conn.execute("UPDATE images SET title=?, tags=?, caption=? WHERE id=?",
                             ((r["title"] or "")[:200] or "Uden titel", r["tags"], (r["description"] or "")[:2000], r["image_id"]))
                n += 1
        print(f"Opdaterede titel/tags/beskrivelse paa {n} allerede importerede billeder.")

    missing_json = len({str(x) for x in album_photo_ids} - seen)
    in_zip = sum(1 for pid in seen if pid in images)
    print("\n=== Faerdig ===")
    print(f"Billed-JSON'er: {len(seen)}   privat (springes over): {private}   udelukket (ikke mine): {skipped_excl}"
          f"   i koeen (offentlige): {queued}")
    print(f"Billedfiler fundet i zip-filerne: {len(images)}  (heraf til billeder i JSON'erne: {in_zip})")
    if missing_json > 50:
        print(f"  ! {missing_json} billed-id'er staar i albums.json men har ingen JSON-fil her - mangler du en eller flere"
              " konto-data-zipfiler? Hent dem alle, og koer kommandoen igen (den er sikker at gentage).")
    cmd_report(args, conn=conn)


def cmd_images_source_check(conn, rows, index):
    have = [r for r in rows if r["flickr_id"] in index]
    status = {r[0]: r[1] for r in conn.execute("SELECT flickr_id, status FROM flickr_photos")}
    excluded = {r[0] for r in conn.execute("SELECT photo_id FROM flickr_excluded")}
    done_in = sum(1 for i in index if status.get(i) == "done")
    excl_in = sum(1 for i in index if i in excluded)
    other_skipped = sum(1 for i in index if i not in status and i not in excluded)
    other = len(index) - done_in - excl_in - other_skipped - len(have)
    print(f"Billedfiler i zip-filerne: {len(index)} = {done_in} allerede importeret + {len(have)} nye"
          f" + {excl_in} 'ikke mine' (springes over; i alt {len(excluded)} paa listen)"
          f" + {other_skipped} andre private / uden metadata (springes over)"
          + (f" + {other} fejlede for mange gange" if other > 0 else ""))
    if len(have) < len(rows):
        print(f"Der er {len(rows) - len(have)} ventende billeder mere i koeen. De ligger i zip-filer, "
              "du endnu ikke har lagt i mappen.")
    return have


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


def record_result(conn, row, res, max_attempts):
    """Gemmer et resultat. Billede + checkpoint i EN transaktion. True hvis billedet er gemt."""
    if res["ok"]:
        try:
            with conn:
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
                if album_id is not None:                 # hovedalbummet; resten kommer med 'link-albums'
                    conn.execute("INSERT OR IGNORE INTO image_albums(image_id, album_id) VALUES (?, ?)",
                                 (cur.lastrowid, album_id))
                conn.execute(
                    """UPDATE flickr_photos SET status='done', image_id=?, error=NULL, done_at=?
                       WHERE flickr_id=?""",
                    (cur.lastrowid, Ctx.app.utc_now_iso(), row["flickr_id"]))
            return True
        except Exception as e:                       # noqa: BLE001
            res = {"ok": False, "error": f"DB: {e}"[:300]}
    attempts = max_attempts if res.get("permanent") else row["attempts"] + 1
    with conn:
        conn.execute("UPDATE flickr_photos SET status='failed', attempts=?, error=? WHERE flickr_id=?",
                     (attempts, res["error"], row["flickr_id"]))
    print(f"  ! {row['flickr_id']} {row['title'][:30]!r}: {res['error']}")
    return False


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

    args.workers = args.workers or (6 if args.source else 2)
    r2 = make_r2()
    GATE.base = GATE.interval = max(0.0, args.delay)
    tmp_dir = os.path.join(Ctx.data_dir, "tmp", "flickr")
    os.makedirs(tmp_dir, exist_ok=True)
    order = "date_taken, date_upload" if args.order == "taken" else "date_upload, flickr_id"
    # Med --source skal --limit gælde EFTER at have fundet de billeder der faktisk ligger i zip-filerne
    limit = f"LIMIT {int(args.limit)}" if (args.limit and not args.source) else ""
    rows = conn.execute(f"SELECT * FROM flickr_photos WHERE {where} ORDER BY {order} {limit}",
                        (args.max_attempts,)).fetchall()

    index = None
    if args.source:
        _, _, index = scan_source(args.source)
        rows = cmd_images_source_check(conn, rows, index)
        if args.limit:
            rows = rows[:args.limit]
        if not rows:
            print("Intet at importere fra zip-filerne.")
            return
    signal.signal(signal.SIGINT, lambda *_: (_stop.set(), signal.signal(signal.SIGINT, signal.default_int_handler),
                                             print("\nStopper efter nuvaerende gruppe ... (Ctrl-C igen = hard stop)")))

    t0 = time.time()
    ok = failed = deferred = bytes_in = bytes_out = 0
    total = len(rows)
    stage = Counter()                     # samlet tid pr. trin (sekunder, summeret over alle billeder)
    timed = 0
    global UPLOAD_POOL
    UPLOAD_POOL = ThreadPoolExecutor(max_workers=args.workers)
    if index is not None:
        work = lambda b: process_local(b, r2, tmp_dir, index[b["flickr_id"]])        # noqa: E731
    else:
        work = lambda b: process_one(b, r2, tmp_dir)                                  # noqa: E731
    source_iter = iter(rows)
    in_flight = []                        # FIFO: resultater gemmes i samme raekkefoelge som de blev sendt af sted
    max_in_flight = max(args.workers * 3, 4)

    def submit_next(ex):
        for b in source_iter:
            # Sikkerhedstjek lige foer behandling: er billedet spaerret ('ikke mine')?
            if conn.execute("SELECT 1 FROM flickr_excluded WHERE photo_id=?", (b["flickr_id"],)).fetchone():
                continue
            in_flight.append((b, ex.submit(work, b)))
            return True
        return False

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        while len(in_flight) < max_in_flight and not _stop.is_set() and submit_next(ex):
            pass
        done_count = 0
        while in_flight:
            row, fut = in_flight.pop(0)
            res = fut.result()
            if not _stop.is_set():
                submit_next(ex)           # hold koeen fyldt, saa ingen traad venter paa de andre
            if res.get("deferred"):
                deferred += 1
                continue
            if record_result(conn, row, res, args.max_attempts):
                ok += 1
                bytes_in += res["bytes_in"]
                bytes_out += res["bytes_out"]
                stage.update(res.get("t", {}))
                timed += 1
            else:
                failed += 1
            done_count += 1
            n = ok + failed
            if n and (n % args.report_every == 0 or not in_flight):
                el = time.time() - t0
                rate = n / el if el else 0
                eta = (total - n) / rate if rate else 0
                saved = (1 - bytes_out / bytes_in) * 100 if bytes_in else 0
                print(f"[{n}/{total}] ok={ok} fejl={failed}  hentet {fmt_bytes(bytes_in)} -> R2 {fmt_bytes(bytes_out)}"
                      f" (-{saved:.0f}%)  {rate * 60:.0f}/min  ca. {eta / 3600:.1f} t tilbage", flush=True)
                if timed and (n % (args.report_every * 4) == 0 or not in_flight):
                    parts = ", ".join(f"{k} {v / timed:.2f}s" for k, v in stage.items())
                    print(f"      snit pr. billede (pr. traad): {parts}", flush=True)
    UPLOAD_POOL.shutdown(wait=True)
    UPLOAD_POOL = None

    print(f"\nStoppet. ok={ok}, fejl={failed}, udskudt={deferred}." +
          ("  Koer samme kommando igen for at fortsaette." if _stop.is_set() or failed else ""))
    if deferred and GATE.cooldowns:
        print("Flickrs CDN blokerede (429). Ingenting er tabt - vent ca. 30-60 min og koer kommandoen igen,"
              " eller brug en hoejere --delay (fx 3).")
    if failed:
        print("Fejlede billeder proeves automatisk igen ved naeste koersel (op til --max-attempts).")


# ---------------------------------------------------------------------------
# Vedligehold
# ---------------------------------------------------------------------------
def cmd_reclassify(args):
    """Koerer kategori-reglerne igen. SOM STANDARD AENDRES INTET - du faar en forhaandsvisning
    (data/reclassify_preview.csv). Foerst naar du koerer med --apply gemmes det, og der tages
    en sikkerhedskopi af databasen foerst. Billeder du selv har rettet i hånden rores ikke."""
    conn = db_connect()
    albums_of = {}
    for r in conn.execute("""SELECT ia.image_id, a.name FROM image_albums ia JOIN albums a ON a.id = ia.album_id
                             ORDER BY a.name"""):
        albums_of.setdefault(r["image_id"], []).append(r["name"])
    rows = conn.execute("""SELECT p.flickr_id, p.album_title, p.title, p.tags, p.description, p.category,
                                  p.status, p.image_id, i.category AS img_cat
                           FROM flickr_photos p LEFT JOIN images i ON i.id = p.image_id""").fetchall()
    changes, kept_manual, same = [], 0, 0
    for r in rows:
        albums = albums_of.get(r["image_id"]) or ([r["album_title"]] if r["album_title"] else [])
        cat, why = flickr_rules.classify(albums, r["title"], r["tags"], r["description"])
        if cat == r["category"]:
            same += 1
            continue
        if r["status"] == "done" and r["img_cat"] != r["category"]:
            kept_manual += 1                          # du har rettet den i hånden - roer den ikke
            continue
        changes.append((r, albums, cat, why))

    matrix = Counter((r["category"], cat) for r, _, cat, _ in changes)
    print(f"\n{len(rows)} billeder: {same} uaendrede, {len(changes)} skifter kategori, "
          f"{kept_manual} har du selv rettet (rores ikke)")
    if matrix:
        print("\n  fra     -> til       antal")
        for (old, new_), n in matrix.most_common():
            print(f"  {old:7} -> {new_:7} {n:6}")
    csv_path = os.path.join(Ctx.data_dir, "reclassify_preview.csv")
    import csv as _csv
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        w = _csv.writer(fh, delimiter=";")
        w.writerow(["image_id", "flickr_id", "titel", "albums", "fra", "til", "begrundelse"])
        for r, albums, cat, why in sorted(changes, key=lambda c: (c[0]["category"], c[2], c[0]["title"])):
            w.writerow([r["image_id"] or "", r["flickr_id"], r["title"], " | ".join(albums), r["category"], cat, why])
    print(f"\nAlle aendringer er skrevet til {csv_path} - aabn den i Excel/Numbers og kig dem igennem.")

    if not args.apply:
        print("INTET ER AENDRET. Er du tilfreds, koer igen med  --apply")
        return
    if not changes:
        print("Intet at gemme.")
        return
    backup = os.path.join(Ctx.data_dir, "gallery.db.before-reclassify-" + time.strftime("%Y%m%d-%H%M%S"))
    src, dst = sqlite3.connect(Ctx.db_path), sqlite3.connect(backup)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    print(f"Sikkerhedskopi: {backup}")
    with conn:
        for r, _, cat, why in changes:
            if r["status"] == "done":
                conn.execute("UPDATE images SET category=? WHERE id=?", (cat, r["image_id"]))
            conn.execute("UPDATE flickr_photos SET category=?, category_reason=? WHERE flickr_id=?",
                         (cat, why, r["flickr_id"]))
    print(f"Gemt: {len(changes)} billeder fik ny kategori. Fortryd ved at kopiere sikkerhedskopien tilbage over data/gallery.db.")
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


FL_KEY_RE = re.compile(r"^(uploads|thumbnails)/fl\d+(_t)?\.[A-Za-z0-9]+$")


def cmd_reset_import(args):
    """Sletter ALT der er importeret fra Flickr (filer i R2, billeder, albums og koeen), saa du kan starte forfra.
    Roerer kun filer der hedder fl<tal>.jpg / fl<tal>_t.jpg - dine egne testbilleder bliver liggende."""
    conn = db_connect()
    r2 = make_r2()
    print("Lister R2 ...")
    keys = sorted(k for k in (r2.list_keys("uploads/") | r2.list_keys("thumbnails/")) if FL_KEY_RE.match(k))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ids = {r[0] for r in conn.execute("SELECT id FROM images WHERE filename GLOB 'fl[0-9]*'")}
    album_ids = set()
    if "flickr_photos" in tables:
        ids |= {r[0] for r in conn.execute("SELECT image_id FROM flickr_photos WHERE image_id IS NOT NULL")}
    if "flickr_albums" in tables:
        album_ids = {r[0] for r in conn.execute("SELECT local_album_id FROM flickr_albums WHERE local_album_id IS NOT NULL")}
    print(f"Vil slette: {len(keys)} filer i R2, {len(ids)} billeder i databasen, "
          f"op til {len(album_ids)} albums, og hele importkoeen.")
    print("(Billeder uden for importen, fx dine testbilleder, bliver liggende.)")
    if not keys and not ids and not album_ids and "flickr_photos" not in tables:
        print("Intet at slette.")
        return
    if not args.yes and input('Skriv SLET for at fortsaette: ').strip() != "SLET":
        print("Afbrudt.")
        return
    for i, k in enumerate(keys, 1):
        area, name = k.split("/", 1)
        r2.delete(area, name)
        if i % 500 == 0:
            print(f"  slettet {i}/{len(keys)} fra R2 ...")
    with conn:
        for chunk in [sorted(ids)[i:i + 500] for i in range(0, len(ids), 500)]:
            q = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM comments WHERE image_id IN ({q})", chunk)
            conn.execute(f"DELETE FROM images WHERE id IN ({q})", chunk)
        for aid in album_ids:
            if not conn.execute("SELECT 1 FROM images WHERE album_id=? LIMIT 1", (aid,)).fetchone():
                conn.execute("DELETE FROM albums WHERE id=?", (aid,))
        for t in ("flickr_photos", "flickr_albums", "flickr_excluded"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        if conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0:
            conn.execute("DELETE FROM sqlite_sequence WHERE name='images'")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    left = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    print(f"Faerdig. Billeder tilbage i databasen: {left} (dine egne). Start forfra med export-metadata.")


def cmd_link_albums(args):
    """Laegger ALLE albums paa de importerede billeder (ikke kun det foerste).
    Laeser albums.json fra eksporten, opretter manglende albums og fylder tabellen image_albums.
    Sikker at koere igen - der oprettes aldrig dubletter. Udelukkede albums og billeder rores ikke."""
    conn = db_connect()
    conn.executescript(STAGING_SQL)
    albums_ref, _, _ = scan_source(args.source)
    if not albums_ref:
        sys.exit("Fandt ingen albums.json. Peg --source paa mappen med eksport-zipfilerne.")
    albums = json.loads(read_ref(albums_ref).decode("utf-8"))["albums"]
    done = {r["flickr_id"]: r["image_id"] for r in conn.execute(
        "SELECT flickr_id, image_id FROM flickr_photos WHERE status='done' AND image_id IS NOT NULL")}
    print(f"{len(done)} importerede billeder, {len(albums)} albums i eksporten")

    created = skipped_excl = skipped_empty = 0
    before = conn.total_changes
    with conn:
        for a in albums:
            if (a["id"] in EXCLUDED_ALBUM_IDS or EXCLUDE_TITLE_RE.search(a.get("title", ""))
                    or EXCLUDE_TITLE_RE.search(a.get("description", ""))):
                skipped_excl += 1
                continue
            image_ids = [done[str(p)] for p in a.get("photos", []) if str(p) in done]
            if not image_ids:
                skipped_empty += 1                 # ingen importerede billeder -> intet tomt album
                continue
            conn.execute(
                """INSERT OR IGNORE INTO flickr_albums(flickr_id, title, description, photo_count, excluded)
                   VALUES (?, ?, ?, ?, 0)""",
                (a["id"], a.get("title", ""), a.get("description", ""), int(a.get("photo_count") or 0)))
            had = conn.execute("SELECT local_album_id FROM flickr_albums WHERE flickr_id=?", (a["id"],)).fetchone()[0]
            local = local_album_id(conn, a["id"])
            if local is None:
                continue
            if not had:
                created += 1
            conn.executemany("INSERT OR IGNORE INTO image_albums(image_id, album_id) VALUES (?, ?)",
                             [(i, local) for i in image_ids])
    multi = conn.execute("""SELECT COUNT(*) FROM (SELECT image_id FROM image_albums
                            GROUP BY image_id HAVING COUNT(*) > 1)""").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM image_albums").fetchone()[0]
    print(f"Nye albums oprettet: {created}   albums uden importerede billeder (sprunget over): {skipped_empty}"
          f"   udelukkede albums: {skipped_excl}")
    print(f"Album-tilknytninger i alt: {total}   billeder i mere end ét album: {multi}")
    print("Udeladte billeder (private/udelukkede) har ingen tilknytning, saa de kan ikke komme med her.")


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


def cmd_check_urls(args):
    """Tester om billedernes ADRESSER svarer, som en browser ville se dem (HTTP-status).
    Skelner mellem: filen mangler (404), for mange forespoergsler/blokeret (429/403) og alt ok (200)."""
    conn = db_connect()
    r2 = make_r2()
    rows = conn.execute("""SELECT id, title, filename, thumbnail FROM images WHERE filename GLOB 'fl[0-9]*'
                           ORDER BY RANDOM() LIMIT ?""", (args.count,)).fetchall()
    if not rows:
        sys.exit("Ingen importerede billeder i databasen.")
    sample_url = r2.public_url("thumbnails", rows[0]["thumbnail"])
    print(f"Tester {len(rows)} tilfaeldige billeder (hovedbillede + thumbnail) mod  {sample_url.rsplit('/', 2)[0]}/ ...")
    if ".r2.dev" in sample_url:
        print("  ! Adressen er en r2.dev-adresse. Cloudflare begraenser dem og anbefaler dem kun til test.")
    session = requests.Session()
    session.headers["User-Agent"] = "bus-car-gallery-check/1.0"
    results = Counter()
    bad = []
    for r in rows:
        for kind, area, name in (("thumbnail", "thumbnails", r["thumbnail"]), ("billede", "uploads", r["filename"])):
            url = r2.public_url(area, name)
            try:
                resp = session.head(url, timeout=15, allow_redirects=True)
                if resp.status_code == 405:
                    resp = session.get(url, timeout=15, stream=True)
                code = str(resp.status_code)
            except requests.RequestException as e:
                code = type(e).__name__
            results[code] += 1
            if code != "200" and len(bad) < 10:
                bad.append((code, kind, r["id"], r["title"], url))
            time.sleep(args.delay)
    total = sum(results.values())
    print("\nSvar:  " + ",  ".join(f"{k}: {v} ({v * 100 // total} %)" for k, v in results.most_common()))
    for code, kind, iid, title, url in bad:
        print(f"  {code}  {kind:9} id {iid} {title[:30]!r}\n        {url}")
    if results.get("404"):
        print("\n404 = filen findes IKKE i R2. Koer:  python tools/import_flickr.py repair-files --source <mappen med zip-filerne>")
    if results.get("429") or results.get("403") or results.get("1015"):
        print("\n429/403 = Cloudflare begraenser eller blokerer. Det er ikke filerne der mangler, men leveringen: "
              "brug en rigtig domaene til R2 (fx billeder.dit-domaene.dk) i stedet for r2.dev.")
    if results.get("200") == total:
        print("\nAlt svarer 200. Hvis billeder alligevel mangler i browseren, er det cache (proev et privat vindue / hard refresh).")


def cmd_repair_files(args):
    """Genskaber billedfiler der staar i databasen men mangler i R2. Laeser originalerne fra zip-filerne og
    uploader til de SAMME filnavne. Databasen aendres IKKE (ingen nye raekker, ingen dubletter)."""
    conn = db_connect()
    r2 = make_r2()
    print("Lister R2 ...")
    existing = r2.list_keys("uploads/") | r2.list_keys("thumbnails/")
    rows = conn.execute("""SELECT p.*, i.filename AS db_filename, i.thumbnail AS db_thumbnail
                           FROM flickr_photos p JOIN images i ON i.id = p.image_id
                           WHERE p.status = 'done'""").fetchall()
    missing = [r for r in rows if f"uploads/{r['db_filename']}" not in existing
               or f"thumbnails/{r['db_thumbnail']}" not in existing]
    print(f"{len(rows)} importerede billeder, {len(missing)} mangler en eller begge filer i R2")
    if not missing:
        print("Intet at reparere.")
        return
    _, _, index = scan_source(args.source)
    todo = [r for r in missing if r["flickr_id"] in index]
    print(f"{len(todo)} af dem findes i zip-filerne og kan genskabes"
          + (f" ({len(missing) - len(todo)} findes ikke i zip-filerne)" if len(todo) < len(missing) else ""))
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        return
    if not args.yes and input("Upload de manglende filer til R2? [j/N] ").strip().lower() not in ("j", "ja", "y", "yes"):
        return
    tmp_dir = os.path.join(Ctx.data_dir, "tmp", "flickr")
    os.makedirs(tmp_dir, exist_ok=True)
    args.workers = args.workers or 4
    fixed = failed = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [(r, ex.submit(process_local, r, r2, tmp_dir, index[r["flickr_id"]])) for r in todo]
        for r, fut in futures:
            res = fut.result()
            if res.get("ok") and (res["filename"], res["thumbnail"]) == (r["db_filename"], r["db_thumbnail"]):
                fixed += 1
            else:
                failed += 1
                print(f"  ! {r['flickr_id']} {r['title'][:30]!r}: "
                      f"{res.get('error') or 'filnavnet afviger fra databasen: ' + str(res.get('filename'))}")
            n = fixed + failed
            if n % 100 == 0 or n == len(todo):
                print(f"[{n}/{len(todo)}] genskabt={fixed} fejl={failed}  {n / max(time.time() - t0, 1) * 60:.0f}/min", flush=True)
    print(f"\nFaerdig: {fixed} billeder genskabt i R2, {failed} fejlede. Koer  verify  for at tjekke.")


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
    i.add_argument("--workers", type=int, default=None,
                   help="samtidige billeder (standard: 6 fra zip-filer, 2 fra Flickrs CDN)")
    i.add_argument("--report-every", type=int, default=25, help="skriv status for hver N billeder (standard 25)")
    i.add_argument("--delay", type=float, default=1.5,
                   help="mindste sekunder mellem start af to downloads (standard 1.5 = max ca. 40/min). "
                        "Stiger automatisk ved 429.")
    i.add_argument("--limit", type=int, help="kun de foerste N (til en proeve, fx --limit 20)")
    i.add_argument("--order", choices=["upload", "taken"], default="upload",
                   help="raekkefoelge; galleriet viser hoejeste id foerst, saa den sidst importerede staar oeverst")
    i.add_argument("--max-attempts", type=int, default=5)
    i.add_argument("--retry-failed", action="store_true", help="nulstil fejltaeller og proev alle fejlede igen")
    i.add_argument("--yes", action="store_true", help="spoerg ikke om bekraeftelse")
    i.add_argument("--source", help="mappe med Flickr-eksportens zip-filer: laes billederne derfra i stedet for at "
                                    "hente fra Flickrs CDN (ingen hastighedsgraense). Brug ogsaa --workers 4")
    i.set_defaults(fn=cmd_images)

    e = sub.add_parser("export-metadata", help="fase 1 fra Flickr-eksport: titler, tags, albums, privatliv")
    e.add_argument("--source", required=True, help="mappe med ALLE eksport-zipfilerne")
    e.add_argument("--update-imported", action="store_true",
                   help="opdater ogsaa titel/tags/beskrivelse paa billeder der allerede er importeret")
    e.set_defaults(fn=cmd_export_metadata)

    rc = sub.add_parser("reclassify", help="koer kategori-reglerne igen (forhaandsvisning; gem med --apply)")
    rc.add_argument("--apply", action="store_true", help="gem aendringerne (tager sikkerhedskopi foerst)")
    rc.set_defaults(fn=cmd_reclassify)
    for name, fn, h in (("report", cmd_report, "vis status og kategorier"),
                        ("verify", cmd_verify, "tjek at alt ligger i R2"),
                        ("push-db", cmd_push_db, "laeg databasen i R2 til flytning til Fly")):
        sub.add_parser(name, help=h).set_defaults(fn=fn)
    r = sub.add_parser("reset-import", help="slet ALT importeret fra Flickr (R2 + database) og start forfra")
    r.add_argument("--yes", action="store_true")
    r.set_defaults(fn=cmd_reset_import)
    la = sub.add_parser("link-albums", help="laeg ALLE albums paa de importerede billeder (fra albums.json)")
    la.add_argument("--source", required=True, help="mappen med eksport-zipfilerne (der skal bruges albums.json)")
    la.set_defaults(fn=cmd_link_albums)
    cu = sub.add_parser("check-urls", help="test om billedernes adresser svarer (404 = mangler, 429 = begraenset)")
    cu.add_argument("--count", type=int, default=100, help="antal tilfaeldige billeder (standard 100)")
    cu.add_argument("--delay", type=float, default=0.05, help="pause mellem kald i sekunder")
    cu.set_defaults(fn=cmd_check_urls)
    rf = sub.add_parser("repair-files", help="genskab billedfiler der mangler i R2 (databasen aendres ikke)")
    rf.add_argument("--source", required=True, help="mappen med eksport-zipfilerne")
    rf.add_argument("--workers", type=int, default=None)
    rf.add_argument("--limit", type=int)
    rf.add_argument("--yes", action="store_true")
    rf.set_defaults(fn=cmd_repair_files)
    p = sub.add_parser("purge-excluded", help="slet importerede billeder fra udelukkede albums")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_purge_excluded)

    args = ap.parse_args()
    init_ctx(args)
    args.fn(args)


if __name__ == "__main__":
    main()
