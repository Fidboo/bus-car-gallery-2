import io
import os
import subprocess
import sys

from conftest import ROOT, csrf_from, login, make_jpeg, upload


def test_production_refuses_to_start_without_secrets():
    env = {k: v for k, v in os.environ.items()
           if k not in ("GALLERY_DEV", "SECRET_KEY", "ADMIN_PASSWORD_HASH")}
    env["DATA_DIR"] = "/tmp/gallery-prod-check"
    out = subprocess.run([sys.executable, "-c", "import app"], cwd=ROOT, env=env,
                         capture_output=True, text=True)
    assert out.returncode != 0
    assert "SECRET_KEY" in out.stderr and "ADMIN_PASSWORD_HASH" in out.stderr


def test_production_rejects_old_default_secret():
    env = {k: v for k, v in os.environ.items() if k != "GALLERY_DEV"}
    env.update(DATA_DIR="/tmp/gallery-prod-check", SECRET_KEY="change-this-secret-key",
               ADMIN_PASSWORD_HASH="x")
    out = subprocess.run([sys.executable, "-c", "import app"], cwd=ROOT, env=env,
                         capture_output=True, text=True)
    assert out.returncode != 0


def test_login_requires_csrf_token(appmod):
    c = appmod.app.test_client()
    c.get("/login")
    r = c.post("/login", data={"password": "correct-horse-battery"})
    assert r.status_code == 400


def test_login_ok_and_wrong_password(appmod):
    c = appmod.app.test_client()
    assert login(c, "nope").status_code == 200  # page shown again
    r = login(c)
    assert r.status_code == 302
    assert c.get("/upload").status_code == 200


def test_brute_force_lockout_even_for_correct_password(appmod):
    c = appmod.app.test_client()
    for _ in range(appmod.LOGIN_MAX_FAILS):
        assert login(c, "wrong").status_code == 200
    r = login(c, "correct-horse-battery")
    assert r.status_code == 429
    assert c.get("/upload").status_code == 302  # still not logged in


def test_lockout_is_per_visitor_ip_behind_cloudflare(appmod, monkeypatch):
    monkeypatch.setattr(appmod, "TRUST_CLOUDFLARE", True)
    attacker = appmod.app.test_client()
    for _ in range(appmod.LOGIN_MAX_FAILS):
        token = csrf_from(attacker)
        attacker.post("/login", data={"password": "x", "csrf_token": token},
                      headers={"CF-Connecting-IP": "203.0.113.9"})
    token = csrf_from(attacker)
    assert attacker.post("/login", data={"password": "x", "csrf_token": token},
                         headers={"CF-Connecting-IP": "203.0.113.9"}).status_code == 429
    father = appmod.app.test_client()
    token = csrf_from(father)
    r = father.post("/login", data={"password": "correct-horse-battery", "csrf_token": token},
                    headers={"CF-Connecting-IP": "198.51.100.7"})
    assert r.status_code == 302


def test_open_redirect_is_blocked(appmod):
    c = appmod.app.test_client()
    token = csrf_from(c)
    r = c.post("/login?next=//evil.example", data={"password": "correct-horse-battery",
                                                   "csrf_token": token})
    assert r.status_code == 302 and "evil.example" not in r.headers["Location"]
    c2 = appmod.app.test_client()
    token = csrf_from(c2)
    r = c2.post("/login?next=/stats", data={"password": "correct-horse-battery",
                                            "csrf_token": token})
    assert r.headers["Location"].endswith("/stats")


def test_upload_requires_login_and_csrf(appmod):
    c = appmod.app.test_client()
    token = csrf_from(c)
    assert upload(c, token=token).status_code == 401            # not logged in
    login(c)
    with c.session_transaction() as s:
        token = s["_csrf"]
    r = c.post("/upload/file", data={"photo": (io.BytesIO(make_jpeg()), "a.jpg"), "category": "bus"},
               content_type="multipart/form-data")                  # no CSRF header
    assert r.status_code == 400
    assert upload(c, token=token).status_code == 200


def test_delete_needs_csrf(appmod):
    c = appmod.app.test_client()
    login(c)
    image_id = upload(c).get_json()["id"]
    assert c.post(f"/image/{image_id}/delete").status_code == 400
    assert c.get(f"/image/{image_id}").status_code == 200


def test_upload_makes_thumbnail_and_download_is_original(appmod):
    from PIL import Image
    c = appmod.app.test_client()
    login(c)
    original = make_jpeg()
    r = upload(c, data=original, title="Volvo B10M", tags="Bus, Volvo")
    assert r.get_json()["ok"]
    image_id = r.get_json()["id"]

    conn = appmod.get_db()
    row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    conn.close()
    assert row["photo_taken_date"] == "2023-05-17"
    assert row["tags"] == "bus, volvo"
    with Image.open(os.path.join(appmod.THUMB_FOLDER, row["thumbnail"])) as t:
        assert max(t.size) <= 500
    # originals are stored untouched (byte for byte)
    assert open(os.path.join(appmod.UPLOAD_FOLDER, row["filename"]), "rb").read() == original
    assert os.listdir(appmod.TMP_FOLDER) == []

    r = c.get(f"/download/{image_id}")
    assert r.status_code == 200
    assert r.headers["Content-Disposition"].startswith("attachment")
    assert "Volvo_B10M.jpg" in r.headers["Content-Disposition"]
    assert r.data == original

    page = c.get(f"/image/{image_id}").get_data(as_text=True)
    assert f"/media/uploads/{row['filename']}" in page
    assert f"/download/{image_id}" in page
    cat = c.get("/category/bus").get_data(as_text=True)
    assert f"/media/thumbnails/{row['thumbnail']}" in cat
    assert c.get(f"/media/thumbnails/{row['thumbnail']}").status_code == 200


def test_invalid_and_wrong_type_files_rejected(appmod):
    c = appmod.app.test_client()
    login(c)
    assert upload(c, data=b"not an image at all", name="x.jpg").status_code == 400
    assert upload(c, data=b"hello", name="x.txt").status_code == 400
    assert upload(c, category="spaceship").status_code == 400
    assert os.listdir(appmod.TMP_FOLDER) == []
    assert os.listdir(appmod.UPLOAD_FOLDER) == []


def test_prev_next_and_position(appmod):
    c = appmod.app.test_client()
    login(c)
    ids = [upload(c, title=f"p{i}").get_json()["id"] for i in range(4)]  # oldest .. newest
    conn = appmod.get_db()
    # middle photo: newest-first order is ids[3], ids[2], ids[1], ids[0]
    prev_id, next_id, pos, total = appmod.get_neighbors(conn, ids[1], category="bus")
    assert (prev_id, next_id, pos, total) == (ids[2], ids[0], 3, 4)
    assert appmod.get_neighbors(conn, ids[3], category="bus")[:2] == (None, ids[2])
    assert appmod.get_neighbors(conn, ids[0], category="bus")[:2] == (ids[1], None)
    # an image outside the context behaves like the old code: alone
    assert appmod.get_neighbors(conn, ids[0], category="car") == (None, None, 1, 1)
    conn.close()


def test_search_matches_all_words(appmod):
    c = appmod.app.test_client()
    login(c)
    upload(c, title="Toyota Land Cruiser", tags="toyota, greenland")
    upload(c, title="Toyota Hilux", tags="toyota, norway")
    page = c.get("/category/bus?q=toyota+greenland").get_data(as_text=True)
    assert "Land Cruiser" in page and "Hilux" not in page


def test_tag_chips_refresh_after_upload(appmod):
    c = appmod.app.test_client()
    login(c)
    upload(c, tags="alpha")
    assert "alpha" in c.get("/category/bus").get_data(as_text=True)
    upload(c, tags="beta")
    assert "beta" in c.get("/category/bus").get_data(as_text=True)


def test_comment_rate_limit_and_admin_delete(appmod):
    admin = appmod.app.test_client()
    login(admin)
    image_id = upload(admin).get_json()["id"]

    visitor = appmod.app.test_client()
    visitor.get(f"/image/{image_id}")
    with visitor.session_transaction() as s:
        token = s["_csrf"]
    for i in range(appmod.COMMENT_MAX + 2):
        visitor.post(f"/image/{image_id}/comment",
                     data={"name": "A", "comment": f"hej {i}", "csrf_token": token})
    conn = appmod.get_db()
    assert conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == appmod.COMMENT_MAX
    comment_id = conn.execute("SELECT id FROM comments LIMIT 1").fetchone()[0]
    conn.close()

    # visitors cannot delete comments, the admin can
    assert visitor.post(f"/comment/{comment_id}/delete", data={"csrf_token": token}).status_code == 302
    conn = appmod.get_db()
    assert conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == appmod.COMMENT_MAX
    conn.close()
    with admin.session_transaction() as s:
        atoken = s["_csrf"]
    admin.post(f"/comment/{comment_id}/delete", data={"csrf_token": atoken})
    conn = appmod.get_db()
    assert conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == appmod.COMMENT_MAX - 1
    conn.close()


def test_comment_is_escaped(appmod):
    admin = appmod.app.test_client()
    login(admin)
    image_id = upload(admin).get_json()["id"]
    with admin.session_transaction() as s:
        token = s["_csrf"]
    admin.post(f"/image/{image_id}/comment",
               data={"name": "<b>x</b>", "comment": "<script>alert(1)</script>", "csrf_token": token})
    page = admin.get(f"/image/{image_id}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_delete_removes_files_and_rows(appmod):
    c = appmod.app.test_client()
    login(c)
    image_id = upload(c).get_json()["id"]
    assert len(os.listdir(appmod.UPLOAD_FOLDER)) == 1
    with c.session_transaction() as s:
        token = s["_csrf"]
    assert c.post(f"/image/{image_id}/delete", data={"csrf_token": token}).status_code == 302
    assert os.listdir(appmod.UPLOAD_FOLDER) == [] and os.listdir(appmod.THUMB_FOLDER) == []
    assert c.get(f"/image/{image_id}").status_code == 404


def test_edit_replaces_photo_and_cleans_old_files(appmod):
    c = appmod.app.test_client()
    login(c)
    image_id = upload(c).get_json()["id"]
    old = os.listdir(appmod.UPLOAD_FOLDER)
    with c.session_transaction() as s:
        token = s["_csrf"]
    r = c.post(f"/image/{image_id}/edit", data={
        "title": "New", "caption": "", "tags": "x", "category": "bus", "album_id": "",
        "photo": (io.BytesIO(make_jpeg((1200, 800))), "new.jpg"), "csrf_token": token},
        content_type="multipart/form-data")
    assert r.status_code == 302
    new = os.listdir(appmod.UPLOAD_FOLDER)
    assert len(new) == 1 and new != old
    assert len(os.listdir(appmod.THUMB_FOLDER)) == 1


def test_security_headers_and_no_origin_bypass(appmod, monkeypatch):
    c = appmod.app.test_client()
    r = c.get("/")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert "no-cache" in r.headers["Cache-Control"]

    monkeypatch.setattr(appmod, "ORIGIN_SECRET", "s3cret")
    assert c.get("/").status_code == 403
    assert c.get("/", headers={"X-Origin-Secret": "wrong"}).status_code == 403
    assert c.get("/", headers={"X-Origin-Secret": "s3cret"}).status_code == 200
    assert c.get("/healthz").status_code == 200   # health checks come without the secret


def test_old_database_is_migrated(tmp_path, monkeypatch):
    """The app must open a database made by the previous version."""
    import shutil
    from conftest import load_app
    old_db = os.path.join(ROOT, "..", "..", "gallery", "bus-car-gallery", "data", "gallery.db")
    if not os.path.exists(old_db):
        import pytest
        pytest.skip("original database not available")
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(old_db, data / "gallery.db")
    appmod = load_app(tmp_path, monkeypatch)
    c = appmod.app.test_client()
    assert c.get("/").status_code == 200
    assert c.get("/category/bus").status_code == 200
    assert c.get("/albums").status_code == 200
