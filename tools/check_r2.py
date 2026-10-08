"""Check that every photo in the database exists in the R2 bucket.

Run it after copying data/uploads and data/thumbnails to R2 (see OPSAETNING.md):

    python tools/check_r2.py --db data/gallery.db
    python tools/check_r2.py --db data/gallery.db --upload-missing data

It reads the same R2_* settings as the app (environment variables).
--upload-missing <data folder> uploads any missing files from that folder
(it contains the uploads/ and thumbnails/ sub-folders).
"""
import argparse
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from storage import R2Storage, storage_from_env  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="path to gallery.db")
    ap.add_argument("--upload-missing", metavar="DATA_DIR",
                    help="upload missing files from this local data folder")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    store = storage_from_env("/tmp/unused-uploads", "/tmp/unused-thumbs")
    if not isinstance(store, R2Storage):
        sys.exit("R2_BUCKET and the other R2_* settings must be set.")

    conn = sqlite3.connect(args.db)
    rows = conn.execute("SELECT id, filename, thumbnail FROM images").fetchall()
    conn.close()

    print(f"Database has {len(rows)} photos. Listing the bucket ...")
    existing = store.list_keys("uploads/") | store.list_keys("thumbnails/")
    print(f"Bucket has {len(existing)} files.")

    missing = []  # (image_id, key)
    for image_id, filename, thumbnail in rows:
        if f"uploads/{filename}" not in existing:
            missing.append((image_id, f"uploads/{filename}"))
        if thumbnail != filename and f"thumbnails/{thumbnail}" not in existing:
            missing.append((image_id, f"thumbnails/{thumbnail}"))

    if not missing:
        print("OK - every photo and thumbnail in the database is in the bucket.")
        return

    print(f"{len(missing)} files are missing from the bucket, e.g.:")
    for image_id, key in missing[:10]:
        print(f"  photo #{image_id}: {key}")

    if not args.upload_missing:
        print("\nRun again with --upload-missing <data folder> to upload them.")
        sys.exit(1)

    def upload(item):
        image_id, key = item
        local = os.path.join(args.upload_missing, key)
        if not os.path.exists(local):
            return f"not found locally: {local}"
        store.upload_raw(key, local)
        return None

    problems = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for n, result in enumerate(pool.map(upload, missing), 1):
            if result:
                problems.append(result)
            if n % 200 == 0:
                print(f"  {n}/{len(missing)} ...")
    print(f"Uploaded {len(missing) - len(problems)} files.")
    if problems:
        print(f"{len(problems)} problems, e.g.:")
        for p in problems[:10]:
            print("  " + p)
        sys.exit(1)


if __name__ == "__main__":
    main()
