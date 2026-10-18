"""Saet nye kategorier paa den KOERENDE database - og kun kategorierne.

Bruges efter `import_flickr.py reclassify`, som skriver data/reclassify_preview.csv.
Dette vaerktoej aendrer KUN kolonnen images.category, og kun paa billeder hvor
kategorien stadig er den gamle ("fra"). Uploads, views, kommentarer, albums og
alt det din far har rettet siden, bliver ikke rort. Man behoever IKKE sende en
hel database til serveren.

    python tools/apply_categories.py reclassify_preview.csv            # kun forhaandsvisning
    python tools/apply_categories.py reclassify_preview.csv --apply    # gem (tager sikkerhedskopi foerst)

Paa Fly:  --db /var/data/gallery.db  (eller lad DATA_DIR vaere sat - det er den paa serveren).
Kun Pythons standardbibliotek - kan koeres paa serveren.
"""
import argparse
import csv
import os
import re
import sqlite3
import sys
import time
from collections import Counter

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def valid_categories():
    """De kategorier appen kender (laeses fra app.py, saa listen aldrig kommer ud af trit)."""
    with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as fh:
        found = set(re.findall(r'\{"slug":\s*"(\w+)"', fh.read()))
    if not found:
        sys.exit("Kunne ikke laese kategorierne fra app.py.")
    return found


def read_changes(path, allowed):
    changes, ignored = [], 0
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh, delimiter=";"):
            image_id, old, new = (row.get("image_id") or "").strip(), row.get("fra", ""), row.get("til", "")
            if not image_id.isdigit():
                ignored += 1                      # raekke uden billede i galleriet
                continue
            if new not in allowed:
                sys.exit(f"Ukendt kategori '{new}' i CSV'en (billede {image_id}). Stoppet - intet er aendret.")
            changes.append((int(image_id), old, new))
    return changes, ignored


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", help="reclassify_preview.csv")
    ap.add_argument("--db", help="sti til gallery.db (standard: $DATA_DIR/gallery.db eller data/gallery.db)")
    ap.add_argument("--apply", action="store_true", help="gem aendringerne (uden: kun forhaandsvisning)")
    args = ap.parse_args()

    data_dir = os.environ.get("DATA_DIR") or os.path.join(ROOT, "data")
    db_path = args.db or os.path.join(data_dir, "gallery.db")
    if not os.path.exists(db_path):
        sys.exit(f"Finder ikke databasen: {db_path}")

    changes, ignored = read_changes(args.csv, valid_categories())
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")

    applied, skipped = Counter(), Counter()
    conn.execute("BEGIN")
    for image_id, old, new in changes:
        cur = conn.execute("UPDATE images SET category = ? WHERE id = ? AND category = ?", (new, image_id, old))
        if cur.rowcount == 1:
            applied[(old, new)] += 1
            continue
        row = conn.execute("SELECT category FROM images WHERE id = ?", (image_id,)).fetchone()
        if row is None:
            skipped["billedet findes ikke i databasen"] += 1
        elif row[0] == new:
            skipped["har allerede den nye kategori"] += 1
        else:
            skipped[f"har nu '{row[0]}' i stedet for '{old}' (rettet siden - roeres ikke)"] += 1

    print(f"Database: {db_path}")
    print(f"CSV: {len(changes)} billeder at ændre ({ignored} raekker uden billede sprunget over)")
    print(f"\n{sum(applied.values())} kan aendres:")
    for (old, new), n in applied.most_common():
        print(f"  {old:7} -> {new:7} {n:6}")
    if skipped:
        print(f"\n{sum(skipped.values())} roeres IKKE:")
        for why, n in skipped.most_common():
            print(f"  {n:6}  {why}")

    if not args.apply:
        conn.rollback()
        conn.close()
        print("\nINTET ER AENDRET. Er du tilfreds, koer igen med --apply")
        return
    if not applied:
        conn.rollback()
        conn.close()
        print("\nIntet at gemme.")
        return

    conn.rollback()                                    # luk forsoeget, tag sikkerhedskopi, og koer saa rigtigt
    backup = os.path.join(os.path.dirname(os.path.abspath(db_path)),
                          "gallery.db.before-categories-" + time.strftime("%Y%m%d-%H%M%S"))
    dst = sqlite3.connect(backup)
    with dst:
        conn.backup(dst)
    dst.close()
    print(f"\nSikkerhedskopi: {backup}")
    with conn:
        for image_id, old, new in changes:
            conn.execute("UPDATE images SET category = ? WHERE id = ? AND category = ?", (new, image_id, old))
    conn.close()
    print(f"Gemt: {sum(applied.values())} billeder fik ny kategori. Intet andet er aendret.")
    print("Fortryd: kopier sikkerhedskopien tilbage (sig til, hvis du har brug for hjaelp).")


if __name__ == "__main__":
    main()
