"""Put a database copy from the R2 bucket onto the server's disk.

    python tools/restore_db.py backups/gallery-20261101-030000.db --force
    python tools/restore_db.py --latest --force

Use it (1) to move the existing gallery.db onto the server the first time, and
(2) to recover after a mistake. Afterwards RESTART the web service so the app
checks/updates the database layout.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from storage import R2Storage, storage_from_env  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("key", nargs="?", help="object key in the bucket, e.g. backups/gallery-....db")
    ap.add_argument("--latest", action="store_true", help="use the newest file under backups/")
    ap.add_argument("--force", action="store_true", help="overwrite an existing gallery.db")
    args = ap.parse_args()

    store = storage_from_env("/tmp/unused-uploads", "/tmp/unused-thumbs")
    if not isinstance(store, R2Storage):
        sys.exit("R2_BUCKET and the other R2_* settings must be set.")

    key = args.key
    if args.latest:
        found = sorted(store.list_keys("backups/"))
        if not found:
            sys.exit("No backups found in the bucket.")
        key = found[-1]
    if not key:
        sys.exit("Give a key, or use --latest.")

    data_dir = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
    os.makedirs(data_dir, exist_ok=True)
    target = os.path.join(data_dir, "gallery.db")
    if os.path.exists(target) and not args.force:
        sys.exit(f"{target} already exists. Add --force to replace it.")

    tmp = target + ".incoming"
    store.client.download_file(store.bucket, key, tmp)
    for suffix in ("-wal", "-shm"):          # leftovers of the old database
        if os.path.exists(target + suffix):
            os.remove(target + suffix)
    os.replace(tmp, target)
    print(f"Restored {key} -> {target}. Now restart the web service.")


if __name__ == "__main__":
    main()
