"""Find (and optionally remove) photos that are in the gallery twice.

Why it can happen: before the upload page fingerprinted files, a "retry" after
an upload the server had in fact saved (e.g. the server ran out of memory just
before answering) stored the photo a second time. The same file could also be
added to the upload list twice.

How duplicates are recognised (all must hold):
  * same category and the same title (ignoring upper/lower case and spaces)
  * NOT two different "taken" times (two shots of the same bus taken at
    different moments are different photos, even if they look alike)
  * the thumbnails look the same: both are shrunk to 16x16 grey pixels and
    compared; the average difference must be tiny

Run on the server. By default it only REPORTS (nothing is changed):

    fly ssh console -C "python3 /code/tools/find_duplicates.py"

The report is printed and saved as a CSV next to the database. To remove the
duplicates - the OLDEST copy of each photo is kept and gets the albums,
comments and views of the copies - add --delete (a copy of the database is
saved first):

    fly ssh console -C "python3 /code/tools/find_duplicates.py --delete"
"""
import argparse
import csv
import io
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor


def load_app_environment():
    """`fly ssh console` doesn't always get the app's settings (R2 keys,
    DATA_DIR). Borrow any that are missing from the running app process."""
    for pid in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ")
            if b"gunicorn" not in cmd and b"flask" not in cmd:
                continue
            with open(f"/proc/{pid}/environ", "rb") as f:
                for item in f.read().split(b"\0"):
                    key, _, value = item.decode("utf-8", "replace").partition("=")
                    if key and key not in os.environ:
                        os.environ[key] = value
            return
        except OSError:
            continue


load_app_environment()
os.environ.setdefault("DATA_DIR", "/var/data" if os.path.isdir("/var/data") else "data")
os.environ["AUTO_BACKUP"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import app  # noqa: E402
from PIL import Image  # noqa: E402

MAX_DIFF = 3.0          # average grey-level difference (0-255) to count as "the same picture"


def norm_title(t):
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def read_thumbnail(row):
    area, name = ("uploads", row["filename"]) if row["thumbnail"] == row["filename"] else ("thumbnails", row["thumbnail"])
    st = app.storage
    if hasattr(st, "client"):
        return st.client.get_object(Bucket=st.bucket, Key=st.key(area, name))["Body"].read()
    with open(st.path(area, name), "rb") as f:
        return f.read()


def fingerprint(row):
    """(aspect, 256 grey values) or None if the thumbnail can't be read."""
    try:
        with Image.open(io.BytesIO(read_thumbnail(row))) as im:
            aspect = round(im.width / max(1, im.height), 2)
            small = im.convert("L").resize((16, 16), Image.BILINEAR)
            return aspect, bytes(small.getdata())
    except Exception:
        return None


def same_picture(a, b):
    if a is None or b is None or a[0] != b[0]:
        return False
    return sum(abs(x - y) for x, y in zip(a[1], b[1])) / 256 <= MAX_DIFF


def different_moments(a, b):
    ta, tb = (a["photo_taken_date"] or "")[:19], (b["photo_taken_date"] or "")[:19]
    return bool(ta and tb and ta != tb)


def find(conn):
    rows = conn.execute("SELECT id, title, category, filename, thumbnail, photo_taken_date, uploaded_at "
                        "FROM images ORDER BY id").fetchall()
    groups = {}
    for r in rows:
        groups.setdefault((r["category"], norm_title(r["title"])), []).append(r)
    candidates = [g for g in groups.values() if len(g) > 1]
    need = [r for g in candidates for r in g]
    print(f"{len(rows)} photos; {len(candidates)} titles occur more than once ({len(need)} photos to compare)")

    prints, done, t0 = {}, 0, time.time()
    with ThreadPoolExecutor(max_workers=8) as pool:
        for r, fp in zip(need, pool.map(fingerprint, need)):
            prints[r["id"]] = fp
            done += 1
            if done % 500 == 0:
                print(f"  compared {done}/{len(need)} thumbnails ({time.time() - t0:.0f} s)")
    unreadable = sum(1 for v in prints.values() if v is None)

    clusters = []
    for g in candidates:
        used = set()
        for i, a in enumerate(g):
            if a["id"] in used:
                continue
            copies = [b for b in g[i + 1:] if b["id"] not in used and not different_moments(a, b)
                      and same_picture(prints.get(a["id"]), prints.get(b["id"]))]
            if copies:
                used.update(b["id"] for b in copies)
                clusters.append((a, copies))
    return clusters, unreadable


def remove(conn, clusters):
    """Keep the oldest copy; move albums, comments and views to it; delete the rest."""
    files_to_delete = []
    with conn:
        for keep, copies in clusters:
            for c in copies:
                conn.execute("INSERT OR IGNORE INTO image_albums (image_id, album_id) "
                             "SELECT ?, album_id FROM image_albums WHERE image_id = ?", (keep["id"], c["id"]))
                conn.execute("UPDATE comments SET image_id = ? WHERE image_id = ?", (keep["id"], c["id"]))
                conn.execute("UPDATE images SET views = views + (SELECT views FROM images WHERE id = ?) WHERE id = ?",
                             (c["id"], keep["id"]))
                conn.execute("DELETE FROM image_albums WHERE image_id = ?", (c["id"],))
                conn.execute("DELETE FROM images WHERE id = ?", (c["id"],))
                files_to_delete.append((c["filename"], c["thumbnail"]))
    gone = 0
    for filename, thumbnail in files_to_delete:
        still_used = conn.execute("SELECT 1 FROM images WHERE filename = ? OR thumbnail = ? LIMIT 1",
                                  (filename, thumbnail)).fetchone()
        if not still_used:
            app.delete_stored_files(filename, thumbnail)
            gone += 1
    app.invalidate_tag_cache()
    return gone


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--delete", action="store_true", help="remove the duplicates (keeps the oldest copy)")
    args = ap.parse_args()

    db_path = os.path.join(app.DATA_DIR, "gallery.db")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    print(f"Database: {db_path}   storage: {app.storage.kind}")

    clusters, unreadable = find(conn)
    extra = sum(len(c) for _, c in clusters)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = os.path.join(app.DATA_DIR, f"duplicates-{stamp}.csv")
    with open(report, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["keep_id", "duplicate_id", "category", "title", "taken", "keep_url", "duplicate_url"])
        for keep, copies in clusters:
            for c in copies:
                w.writerow([keep["id"], c["id"], keep["category"], keep["title"], keep["photo_taken_date"] or "",
                            f"{app.CANONICAL_ORIGIN}/image/{keep['id']}", f"{app.CANONICAL_ORIGIN}/image/{c['id']}"])

    print(f"\nFound {extra} duplicate photo(s) of {len(clusters)} photo(s).")
    if unreadable:
        print(f"({unreadable} thumbnails could not be read - those photos were not compared.)")
    for keep, copies in clusters[:15]:
        print(f"  #{keep['id']} '{keep['title']}'  duplicates: {', '.join('#' + str(c['id']) for c in copies)}")
    if len(clusters) > 15:
        print(f"  ... and {len(clusters) - 15} more")
    print(f"Full list: {report}")

    if not args.delete:
        print("\nNothing was changed. Check a few in the browser, then run again with --delete to remove them.")
        return
    if not clusters:
        return
    backup = f"{db_path}.before-dedupe-{stamp}"
    dst = sqlite3.connect(backup)
    conn.backup(dst)
    dst.close()
    gone = remove(conn, clusters)
    print(f"\nRemoved {extra} duplicate(s) ({gone} file pairs deleted from storage). "
          f"Copy of the old database: {backup}")


if __name__ == "__main__":
    main()
