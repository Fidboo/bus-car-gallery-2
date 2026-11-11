"""Duplicate photos: the uploader refuses a file it already has, and
tools/find_duplicates.py finds/removes copies that are already there."""
import importlib
import io
import json
import os
import sys

from PIL import Image

from conftest import login


def _jpeg(color, size=(800, 600)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def _up(client, data, title, sha=None):
    form = {"photo": (io.BytesIO(data), "x.jpg"), "title": title, "category": "car"}
    if sha:
        form["orig_sha"] = sha
    r = client.post("/upload/file", data=form, content_type="multipart/form-data",
                    headers={"X-CSRF-Token": _token(client)})
    return r.get_json()


def _token(client):
    with client.session_transaction() as s:
        return s["_csrf"]


def test_same_file_is_not_uploaded_twice(appmod):
    c = appmod.app.test_client()
    login(c)
    sha = "a" * 64
    first = _up(c, _jpeg((200, 10, 10)), "Volvo 240", sha)
    assert first["ok"] and not first.get("duplicate")
    again = _up(c, _jpeg((200, 10, 10)), "Volvo 240", sha)       # e.g. "retry" after a saved upload
    assert again == {"ok": True, "id": first["id"], "duplicate": True, "timing": {}}
    other = _up(c, _jpeg((10, 200, 10)), "Volvo 240", "b" * 64)
    assert other["ok"] and other["id"] != first["id"]
    no_hash = _up(c, _jpeg((10, 10, 200)), "Old browser")         # no fingerprint: uploads as before
    assert no_hash["ok"]
    conn = appmod.get_db()
    assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 3
    conn.close()


def test_find_duplicates_tool(appmod, capsys):
    c = appmod.app.test_client()
    login(c)
    red, blue = _jpeg((200, 10, 10)), _jpeg((10, 10, 200))
    keep = _up(c, red, "1987 Volvo 740 GL")["id"]
    copy = _up(c, red, "1987  volvo 740 gl")["id"]               # same picture, same title -> duplicate
    look_alike = _up(c, blue, "1987 Volvo 740 GL")["id"]         # same title, other picture -> keep
    other_moment = _up(c, red, "1987 Volvo 740 GL")["id"]        # same picture but taken at another time -> keep
    conn = appmod.get_db()
    conn.execute("UPDATE images SET photo_taken_date = '2020-01-01T10:00:00' WHERE id IN (?, ?)", (keep, copy))
    conn.execute("UPDATE images SET photo_taken_date = '2021-05-05T12:00:00' WHERE id = ?", (other_moment,))
    conn.execute("UPDATE images SET views = 5 WHERE id = ?", (copy,))
    alb = conn.execute("INSERT INTO albums (name, created_at) VALUES ('Trip', 'x')").lastrowid
    conn.execute("INSERT INTO image_albums (image_id, album_id) VALUES (?, ?)", (copy, alb))
    conn.execute("INSERT INTO comments (image_id, name, comment, created_at) VALUES (?, 'A', 'Nice', 'x')", (copy,))
    conn.commit()
    conn.close()

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    tool = importlib.import_module("find_duplicates")
    tool.app = appmod
    sys.argv = ["find_duplicates.py"]
    tool.main()
    out = capsys.readouterr().out
    assert "Found 1 duplicate photo(s) of 1 photo(s)" in out and "Nothing was changed" in out
    conn = appmod.get_db()
    assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 4

    sys.argv = ["find_duplicates.py", "--delete"]
    tool.main()
    ids = {r[0] for r in conn.execute("SELECT id FROM images")}
    assert ids == {keep, look_alike, other_moment}
    assert conn.execute("SELECT views FROM images WHERE id = ?", (keep,)).fetchone()[0] == 5
    assert conn.execute("SELECT image_id FROM comments").fetchone()[0] == keep
    assert conn.execute("SELECT image_id FROM image_albums WHERE album_id = ?", (alb,)).fetchone()[0] == keep
    assert any(f.startswith("gallery.db.before-dedupe-") for f in os.listdir(appmod.DATA_DIR))
    conn.close()
