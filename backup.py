"""Database backups to the R2 bucket (backups/gallery-YYYYMMDD-HHMMSS.db).

The database holds everything the photographer has typed (titles, captions,
tags, albums), so it is copied to R2 once a day and the newest 14 copies are
kept. (The photos themselves need their own second copy - see OPSAETNING.md.)
"""
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone

PREFIX = "backups/"
KEEP = 14
MAX_AGE_SECONDS = 23 * 3600   # take a new backup when the newest is older than this


def _parse_time(key):
    """backups/gallery-20261101-030000.db -> unix time (or None)."""
    name = os.path.basename(key)
    try:
        stamp = name[len("gallery-"):-len(".db")]
        return datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def backup_due(store, now=None):
    now = now or time.time()
    times = [t for t in (_parse_time(k) for k in store.list_keys(PREFIX)) if t]
    return not times or now - max(times) > MAX_AGE_SECONDS


def run_backup(db_path, store, keep=KEEP):
    """Copy the live database safely and upload it. Returns the new key."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    key = f"{PREFIX}gallery-{stamp}.db"
    with tempfile.TemporaryDirectory() as tmp:
        copy_path = os.path.join(tmp, "copy.db")
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(copy_path)
        with dst:
            src.backup(dst)   # consistent copy even while the site is running
        src.close()
        dst.close()
        store.upload_raw(key, copy_path, content_type="application/octet-stream", cache=False)
    for old in sorted(store.list_keys(PREFIX))[:-keep]:
        store.client.delete_object(Bucket=store.bucket, Key=old)
    return key


def start_scheduler(db_path, store, log, first_delay=60, check_every=3600):
    """Background thread: backs up when the newest backup is over ~a day old."""
    def loop():
        time.sleep(first_delay)
        while True:
            try:
                if backup_due(store):
                    log.info("Database backup saved: %s", run_backup(db_path, store))
            except Exception:
                log.exception("Automatic database backup failed")
            time.sleep(check_every)

    thread = threading.Thread(target=loop, name="db-backup", daemon=True)
    thread.start()
    return thread
