"""Make a safe copy of the database and store it in the R2 bucket right now.

    python tools/backup_db.py

(The running app already does this by itself once a day when R2 is in use;
use this for an extra copy, e.g. before a big change.) Keeps the newest 14.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from backup import run_backup  # noqa: E402
from storage import R2Storage, storage_from_env  # noqa: E402


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.environ.get("DATA_DIR", os.path.join(here, "..", "data"))
    store = storage_from_env("/tmp/unused-uploads", "/tmp/unused-thumbs")
    if not isinstance(store, R2Storage):
        sys.exit("R2_BUCKET and the other R2_* settings must be set.")
    print("Uploaded", run_backup(os.path.join(data_dir, "gallery.db"), store))


if __name__ == "__main__":
    main()
