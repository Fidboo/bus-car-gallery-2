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
    assert c.get(f"/image/{image_id}", follow_redirects=True).status_code == 200


def test_upload_makes_thumbnail_and_keeps_pixels_identical(appmod):
    from PIL import Image, ImageChops
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

    # lossless: same size in pixels, every pixel identical, never bigger
    path = os.path.join(appmod.UPLOAD_FOLDER, row["filename"])
    stored = open(path, "rb").read()
    assert len(stored) <= len(original)
    with Image.open(io.BytesIO(original)) as a, Image.open(path) as b:
        assert a.size == b.size == (3000, 2000)
        assert ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox() is None
        assert b.getexif().get(306) == "2023:05:17 12:00:00"     # metadata kept
    assert os.listdir(appmod.TMP_FOLDER) == []

    r = c.get(f"/download/{image_id}")
    assert r.status_code == 200
    assert r.headers["Content-Disposition"].startswith("attachment")
    assert "Volvo_B10M.jpg" in r.headers["Content-Disposition"]
    assert r.data == stored

    page = c.get(f"/image/{image_id}", follow_redirects=True).get_data(as_text=True)
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
    visitor.get(f"/image/{image_id}", follow_redirects=True)
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
    page = admin.get(f"/image/{image_id}", follow_redirects=True).get_data(as_text=True)
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
    assert c.get(f"/image/{image_id}", follow_redirects=True).status_code == 404


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


def _stored(appmod, c, data, name):
    r = upload(c, data=data, name=name)
    assert r.get_json()["ok"], r.get_json()
    conn = appmod.get_db()
    row = conn.execute("SELECT filename FROM images WHERE id = ?", (r.get_json()["id"],)).fetchone()
    conn.close()
    return row["filename"], os.path.join(appmod.UPLOAD_FOLDER, row["filename"])


def _noisy_jpeg(size, quality=92, exif=None):
    from PIL import Image, ImageFilter
    img = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).filter(ImageFilter.GaussianBlur(1))
    buf = io.BytesIO()
    kwargs = {"exif": exif} if exif else {}
    img.save(buf, "JPEG", quality=quality, **kwargs)
    return buf.getvalue()


def test_jpeg_is_smaller_but_pixel_identical_and_rotation_kept(appmod):
    from PIL import Image, ImageChops
    c = appmod.app.test_client()
    login(c)
    exif = Image.Exif()
    exif[274] = 6                                   # "rotate 90 degrees"
    original = _noisy_jpeg((2400, 1600), exif=exif.tobytes())
    name, path = _stored(appmod, c, original, "r.jpg")
    assert name.endswith(".jpg")
    assert os.path.getsize(path) < len(original)    # lossless packing saves space
    with Image.open(io.BytesIO(original)) as a, Image.open(path) as b:
        assert a.size == b.size == (2400, 1600)     # no downscaling
        assert ImageChops.difference(a, b).getbbox() is None
        assert b.getexif().get(274) == 6            # orientation tag still there


def test_png_stays_png_and_pixels_identical(appmod):
    from PIL import Image, ImageChops
    c = appmod.app.test_client()
    login(c)
    img = Image.new("RGBA", (600, 400), (255, 0, 0, 0))
    img.putpixel((5, 5), (1, 2, 3, 200))
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=0)          # badly compressed on purpose
    name, path = _stored(appmod, c, buf.getvalue(), "t.png")
    assert name.endswith(".png")
    assert os.path.getsize(path) < len(buf.getvalue())
    with Image.open(path) as out:
        assert out.mode == "RGBA"
        assert ImageChops.difference(img, out).getbbox() is None


def test_other_formats_are_stored_untouched(appmod):
    from PIL import Image
    c = appmod.app.test_client()
    login(c)
    frames = [Image.new("RGB", (50, 50), col) for col in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    name, path = _stored(appmod, c, buf.getvalue(), "a.gif")
    assert name.endswith(".gif")
    assert open(path, "rb").read() == buf.getvalue()
    assert os.listdir(appmod.TMP_FOLDER) == []


def test_already_optimal_file_is_stored_byte_for_byte(appmod, monkeypatch):
    c = appmod.app.test_client()
    login(c)
    original = make_jpeg((200, 100))
    monkeypatch.setattr(appmod, "optimize_image", lambda *a, **k: False)
    _, path = _stored(appmod, c, original, "o.jpg")
    assert open(path, "rb").read() == original


def test_photo_above_limit_is_downscaled_rotated_and_gps_removed(appmod, monkeypatch):
    from PIL import Image
    monkeypatch.setattr(appmod, "MAX_IMAGE_PX", 1200)     # keeps the test fast
    c = appmod.app.test_client()
    login(c)
    exif = Image.Exif()
    exif[274] = 6                                          # "rotate 90 degrees"
    exif[306] = "2021:01:02 10:00:00"
    exif.get_ifd(0x8825)[1] = "N"                          # a GPS tag
    original = _noisy_jpeg((2400, 1600), quality=95, exif=exif.tobytes())
    name, path = _stored(appmod, c, original, "big.jpg")
    assert name.endswith(".jpg")
    with Image.open(path) as out:
        assert out.size == (800, 1200)                     # portrait now, long side = limit
        assert out.getexif().get(274) in (None, 1)         # rotation baked into the pixels
        assert not out.getexif().get_ifd(0x8825)           # GPS gone
        assert out.getexif().get(306) == "2021:01:02 10:00:00"
    assert os.path.getsize(path) < len(original) / 2
    assert os.listdir(appmod.TMP_FOLDER) == []


def test_photo_below_limit_keeps_its_resolution(appmod, monkeypatch):
    from PIL import Image
    monkeypatch.setattr(appmod, "MAX_IMAGE_PX", 3840)
    c = appmod.app.test_client()
    login(c)
    _, path = _stored(appmod, c, _noisy_jpeg((2400, 1600)), "ok.jpg")
    with Image.open(path) as out:
        assert out.size == (2400, 1600)


def test_album_search_filters_by_name(appmod):
    c = appmod.app.test_client()
    login(c)
    for name in ("Norway Trip 2024", "Greenland 2023", "Norway Winter"):
        with c.session_transaction() as s:
            token = s["_csrf"]
        assert c.post("/albums/new", data={"name": name, "csrf_token": token}).status_code == 302

    def names(html):
        return [n for n in ("Norway Trip 2024", "Greenland 2023", "Norway Winter")
                if f'<div class="card-title">{n}</div>' in html]

    # no query -> everything, and no "Clear" link
    html = c.get("/albums").get_data(as_text=True)
    assert len(names(html)) == 3 and "Clear" not in html

    # case-insensitive, partial words, like the category search
    html = c.get("/albums?q=NORW").get_data(as_text=True)
    assert names(html) == ["Norway Trip 2024", "Norway Winter"]
    assert "2 results for" in html and "Clear" in html

    # several words: every word must match
    html = c.get("/albums?q=norway+2024").get_data(as_text=True)
    assert names(html) == ["Norway Trip 2024"]
    assert "1 result for" in html

    # nothing found -> friendly message, query kept in the box
    html = c.get("/albums?q=zzz").get_data(as_text=True)
    assert names(html) == [] and 'No albums match "zzz"' in html
    assert 'value="zzz"' in html


def hit(client, path, headers=None):
    """What the script in every page does once the page has loaded."""
    return client.post("/api/hit", data=path, headers=headers or {}, content_type="text/plain")


def test_views_count_once_per_visitor_and_skip_bots_and_owner(appmod):
    import time
    owner = appmod.app.test_client()
    login(owner)
    image_id = upload(owner, title="Counted").get_json()["id"]
    page = f"/image/{image_id}-counted"

    def views():
        conn = appmod.get_db()
        n = conn.execute("SELECT views FROM images WHERE id = ?", (image_id,)).fetchone()[0]
        conn.close()
        return n

    def site_views():
        conn = appmod.get_db()
        n = conn.execute("SELECT value FROM site_stats WHERE key = 'total_visits'").fetchone()[0]
        conn.close()
        return n

    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/120"}

    # just downloading the page (a scraper) counts for nothing; the owner never counts
    appmod.app.test_client().get(page, headers=ua)
    hit(owner, page, ua)
    assert views() == 0 and site_views() == 0

    visitor = appmod.app.test_client()
    assert hit(visitor, page, ua).status_code == 204
    site_after_first = site_views()
    assert views() == 1 and site_after_first == 1

    # reloading / opening it again does not count - neither photo nor page
    for _ in range(3):
        hit(visitor, page, ua)
    assert views() == 1 and site_views() == site_after_first

    # a different photo/page from the same visitor does count
    other = upload(owner, title="Other", name="b.jpg").get_json()["id"]
    hit(visitor, f"/image/{other}-other", ua)
    assert site_views() == site_after_first + 1

    # a visitor without the cookie (new browser) counts as a new person
    hit(appmod.app.test_client(), page, ua)
    assert views() == 2

    # search engines and scripts are never counted; neither are made-up addresses
    for agent in ("Googlebot/2.1 (+http://www.google.com/bot.html)", "python-requests/2.31", ""):
        hit(appmod.app.test_client(), page, {"User-Agent": agent})
    hit(appmod.app.test_client(), "/etc/passwd", ua)
    hit(appmod.app.test_client(), "/image/999999-nothing", ua)
    assert views() == 2

    # after 24 hours the same visitor counts again
    old = int(time.time()) - appmod.VIEW_WINDOW - 60
    stale = appmod.app.test_client()
    stale.set_cookie(appmod.VIEW_COOKIE, f"i{image_id}.{old}")
    hit(stale, page, ua)
    assert views() == 3


def test_view_cookie_stays_small_and_ignores_garbage(appmod):
    owner = appmod.app.test_client()
    login(owner)
    ua = {"User-Agent": "Mozilla/5.0 Chrome/120"}
    c = appmod.app.test_client()
    c.set_cookie(appmod.VIEW_COOKIE, ",,x,.5,i1.abc,%s" % ("z" * 50 + ".1"))
    assert hit(c, "/", ua).status_code == 204
    ids = [upload(owner, title=f"t{i}", name=f"{i}.jpg", data=make_jpeg((400, 300))).get_json()["id"]
           for i in range(3)]
    for i in ids:
        hit(c, f"/image/{i}", ua)
    cookie = c.get_cookie(appmod.VIEW_COOKIE).value
    assert len(cookie) < 3000


def test_scrapers_get_slowed_down_but_search_engines_and_owner_do_not(appmod, monkeypatch):
    owner = appmod.app.test_client()
    login(owner)
    monkeypatch.setattr(appmod, "PAGE_LIMIT_PER_MIN", 5)
    monkeypatch.setattr(appmod, "SEARCH_LIMIT_PER_MIN", 3)
    appmod._page_hits.clear()
    ua = {"User-Agent": "Mozilla/5.0 Chrome/120"}
    c = appmod.app.test_client()
    codes = [c.get("/category/bus?q=a%d" % i, headers=ua).status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]                       # searches: 3 a minute
    r = c.get("/category/bus?q=zzz", headers=ua)
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"
    for _ in range(3):
        assert c.get("/", headers=ua).status_code in (200, 429)
    assert c.get("/static/style.css", headers=ua).status_code == 200      # files are never limited
    assert c.get("/robots.txt", headers=ua).status_code == 200
    google = {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"}
    assert all(c.get("/category/bus?q=x%d" % i, headers=google).status_code == 200 for i in range(10))
    assert all(owner.get("/category/bus?q=x%d" % i, headers=ua).status_code == 200 for i in range(10))
    appmod._page_hits.clear()


def test_photo_page_shows_thumbnail_first_and_prefetches_next(appmod):
    owner = appmod.app.test_client()
    login(owner)
    ids = [upload(owner, title=f"p{i}", name=f"{i}.jpg", data=make_jpeg((1200, 800))).get_json()["id"]
           for i in range(2)]
    newer, older = ids[1], ids[0]          # newest first: "next" from the newer one is the older
    c = appmod.app.test_client()
    html = c.get(f"/image/{newer}?cat=bus", follow_redirects=True).get_data(as_text=True)
    stored = {}
    conn = appmod.get_db()
    for i in ids:
        stored[i] = conn.execute("SELECT filename, thumbnail FROM images WHERE id=?", (i,)).fetchone()
    conn.close()
    with appmod.app.test_request_context():
        full = appmod.media_url("uploads", stored[newer]["filename"])
        thumb = appmod.thumb_url(stored[newer]["thumbnail"], stored[newer]["filename"])
        next_full = appmod.media_url("uploads", stored[older]["filename"])
    assert f'src="{thumb}"' in html                       # thumbnail is what paints first
    assert f'data-full="{full}"' in html                  # full photo is swapped in afterwards
    assert f'rel="preload" as="image" href="{full}"' in html
    assert f'data-next="{next_full}"' in html
    # the oldest photo has no "next", so nothing to prefetch
    assert 'data-next="' not in c.get(f"/image/{older}?cat=bus", follow_redirects=True).get_data(as_text=True)


def test_thumbnail_cards_know_their_full_photo_and_script_is_served(appmod):
    owner = appmod.app.test_client()
    login(owner)
    image_id = upload(owner, title="card", category="bus").get_json()["id"]
    c = appmod.app.test_client()
    conn = appmod.get_db()
    fn = conn.execute("SELECT filename FROM images WHERE id=?", (image_id,)).fetchone()[0]
    conn.close()
    with appmod.app.test_request_context():
        marker = f'data-full="{appmod.media_url("uploads", fn)}"'
    assert marker in c.get("/category/bus").get_data(as_text=True)
    assert marker in c.get("/").get_data(as_text=True)
    page = c.get("/category/bus").get_data(as_text=True)
    assert "prefetch.js" in page
    assert c.get("/static/prefetch.js").status_code == 200


def test_back_link_returns_to_the_list_page_the_photo_is_on(appmod):
    import re
    conn = appmod.get_db()
    conn.executemany(
        "INSERT INTO images (title, tags, filename, thumbnail, uploaded_at, category) VALUES (?,?,?,?,?,?)",
        [(f"Bus {i}", "volvo" if i % 2 else "scania", f"f{i}.jpg", f"t{i}.jpg", "2024-01-01T00:00:00", "bus")
         for i in range(1, 61)])
    conn.commit()
    conn.close()
    per_page = appmod.PER_PAGE
    c = appmod.app.test_client()

    def back(image_id, query=""):
        html = c.get(f"/image/{image_id}?cat=bus&q={query}", follow_redirects=True).get_data(as_text=True)
        return re.search(r'id="backLink" href="([^"]+)"', html).group(1).replace("&amp;", "&")

    # newest first: id 60 is position 1. Page 1 holds positions 1..per_page.
    assert back(60) == "/category/bus?q="                       # first photo -> page 1 (no page param)
    assert back(60 - per_page + 1) == "/category/bus?q="        # last photo on page 1
    assert back(60 - per_page) == "/category/bus?q=&page=2"     # first photo on page 2
    assert back(60 - 2 * per_page) == "/category/bus?q=&page=3"
    assert back(1) == "/category/bus?q=&page=3"                 # oldest photo (position 60) -> page 3

    # the same holds inside a search: position counts only the matching photos
    html = c.get("/category/bus?q=volvo&page=2").get_data(as_text=True)
    on_page_two = [int(m) for m in re.findall(r"/image/(\d+)-[^?\"]*\?", html)]
    assert on_page_two and all(back(i, "volvo") == "/category/bus?q=volvo&page=2" for i in on_page_two)

    # photos in an album have no pages: back goes to the album
    conn = appmod.get_db()
    conn.execute("INSERT INTO albums (name, created_at) VALUES ('A', '2024-01-01')")
    album_id = conn.execute("SELECT id FROM albums WHERE name='A'").fetchone()[0]
    conn.execute("UPDATE images SET album_id = ? WHERE id = 1", (album_id,))
    conn.commit()
    conn.close()
    html = c.get(f"/image/1?album={album_id}", follow_redirects=True).get_data(as_text=True)
    assert f'id="backLink" href="/albums/{album_id}"' in html
