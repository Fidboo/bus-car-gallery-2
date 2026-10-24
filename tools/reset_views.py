"""Set every photo's view count and the site-wide page-view total to 0.

Run on the server (it first saves a copy of the database next to it):

    fly ssh console -C "python3 tools/reset_views.py"

Nothing else is touched. Use --dry-run to only show the current numbers.
"""
import os
import sqlite3
import sys
import time

data_dir = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
path = os.path.join(data_dir, "gallery.db")
if not os.path.exists(path):
    sys.exit(f"No database at {path}")

conn = sqlite3.connect(path, timeout=30)
photos, views = conn.execute("SELECT COUNT(*), COALESCE(SUM(views), 0) FROM images").fetchone()
total = conn.execute("SELECT value FROM site_stats WHERE key = 'total_visits'").fetchone()[0]
print(f"Now: {photos} photos, {views} photo views in total, {total} site page views")
if "--dry-run" in sys.argv:
    sys.exit(0)

backup = f"{path}.before-reset-{time.strftime('%Y%m%d-%H%M%S')}"
dst = sqlite3.connect(backup)
conn.backup(dst)
dst.close()
conn.execute("UPDATE images SET views = 0")
conn.execute("UPDATE site_stats SET value = 0 WHERE key = 'total_visits'")
conn.commit()
print(f"Done - all view counts are 0. Copy of the old database: {backup}")
