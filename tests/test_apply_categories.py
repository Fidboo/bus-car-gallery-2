import csv
import os
import sqlite3
import subprocess
import sys

from conftest import ROOT

TOOL = os.path.join(ROOT, "tools", "apply_categories.py")


def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE images (id INTEGER PRIMARY KEY, title TEXT, category TEXT, views INTEGER DEFAULT 0)")
    conn.executemany("INSERT INTO images (id, title, category, views) VALUES (?,?,?,?)", [
        (1, "Volvo bus", "car", 5),       # should become bus
        (2, "DAB tram", "bus", 7),        # should become tram
        (3, "Hand fixed", "truck", 9),    # father changed it since -> must NOT be touched
        (4, "Already", "bus", 1),         # already correct
        (5, "Fathers new upload", "misc", 42),   # not in CSV at all -> untouched
    ])
    conn.commit()
    conn.close()


def make_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["image_id", "flickr_id", "titel", "albums", "fra", "til", "begrundelse"])
        w.writerows(rows)


def run(*args):
    return subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True)


def rows(db):
    conn = sqlite3.connect(db)
    out = {r[0]: r[1:] for r in conn.execute("SELECT id, category, views, title FROM images")}
    conn.close()
    return out


def test_only_listed_categories_change_and_nothing_else(tmp_path):
    db, csv_path = str(tmp_path / "gallery.db"), str(tmp_path / "p.csv")
    make_db(db)
    make_csv(csv_path, [
        (1, "f1", "t", "a", "car", "bus", "x"),
        (2, "f2", "t", "a", "bus", "tram", "x"),
        (3, "f3", "t", "a", "bus", "tram", "x"),     # DB says truck now, not bus
        (4, "f4", "t", "a", "car", "bus", "x"),      # already bus
        (99, "f9", "t", "a", "car", "bus", "x"),     # no such image
        ("", "f0", "t", "a", "car", "bus", "x"),     # not in the gallery
    ])
    before = rows(db)

    preview = run(csv_path, "--db", db)
    assert preview.returncode == 0, preview.stderr
    assert rows(db) == before                          # preview changes nothing
    assert "INTET ER AENDRET" in preview.stdout and "2 kan aendres" in preview.stdout

    done = run(csv_path, "--db", db, "--apply")
    assert done.returncode == 0, done.stderr
    after = rows(db)
    assert after[1][0] == "bus" and after[2][0] == "tram"
    assert after[3] == before[3]                       # hand-fixed photo untouched
    assert after[4] == before[4] and after[5] == before[5]
    for i in after:                                    # titles and views never change
        assert after[i][1:] == before[i][1:]
    assert any(n.startswith("gallery.db.before-categories-") for n in os.listdir(tmp_path))


def test_unknown_category_stops_before_changing_anything(tmp_path):
    db, csv_path = str(tmp_path / "gallery.db"), str(tmp_path / "p.csv")
    make_db(db)
    make_csv(csv_path, [(1, "f1", "t", "a", "car", "spaceship", "x")])
    before = rows(db)
    out = run(csv_path, "--db", db, "--apply")
    assert out.returncode != 0 and "Ukendt kategori" in (out.stderr + out.stdout)
    assert rows(db) == before
