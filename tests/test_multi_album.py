"""A photo can be in several albums (table image_albums)."""
import io

from conftest import csrf_from, login, make_jpeg


def _db(appmod):
    return appmod.get_db()


def _album(conn, name):
    album_id = conn.execute("INSERT INTO albums (name, created_at) VALUES (?, '2026-01-01')", (name,)).lastrowid
    conn.commit()
    return album_id


def _image(conn, title, album_ids=(), category="bus", primary=None):
    """Insert a photo directly (fast) and link it to the given albums."""
    primary = primary if primary is not None else (album_ids[0] if album_ids else None)
    iid = conn.execute(
        "INSERT INTO images (title, tags, filename, thumbnail, uploaded_at, album_id, category) "
        "VALUES (?, '', ?, ?, '2026-01-01', ?, ?)",
        (title, f"{title}.jpg", f"{title}_t.jpg", primary, category),
    ).lastrowid
    for a in album_ids:
        conn.execute("INSERT INTO image_albums (image_id, album_id) VALUES (?, ?)", (iid, a))
    conn.commit()
    return iid


def test_existing_album_id_is_backfilled_once(appmod):
    conn = _db(appmod)
    a = _album(conn, "Old album")
    iid = conn.execute(
        "INSERT INTO images (title, tags, filename, thumbnail, uploaded_at, album_id, category) "
        "VALUES ('t', '', 'f.jpg', 't.jpg', '2026-01-01', ?, 'bus')", (a,)).lastrowid
    conn.execute("DROP TABLE image_albums")        # what an old database looks like
    conn.commit()
    conn.close()

    appmod.init_db()                               # next startup creates + backfills
    conn = _db(appmod)
    rows = conn.execute("SELECT image_id, album_id FROM image_albums").fetchall()
    assert [(r["image_id"], r["album_id"]) for r in rows] == [(iid, a)]

    # later edits must not be undone by another startup
    conn.execute("DELETE FROM image_albums")
    conn.commit()
    conn.close()
    appmod.init_db()
    conn = _db(appmod)
    assert conn.execute("SELECT COUNT(*) FROM image_albums").fetchone()[0] == 0
    conn.close()


def test_album_pages_count_and_list_every_membership(appmod):
    conn = _db(appmod)
    a, b, c_ = _album(conn, "Alpha"), _album(conn, "Beta"), _album(conn, "Gamma")
    i1 = _image(conn, "one", [a, b])
    i2 = _image(conn, "two", [b])
    conn.close()
    c = appmod.app.test_client()

    page = c.get("/albums").get_data(as_text=True)
    assert "1 photo<" in page and "2 photos" in page and "0 photos" in page

    beta = c.get(f"/albums/{b}").get_data(as_text=True)
    assert f"/image/{i1}" in beta and f"/image/{i2}" in beta
    alpha = c.get(f"/albums/{a}").get_data(as_text=True)
    assert f"/image/{i1}" in alpha and f"/image/{i2}" not in alpha
    assert c.get(f"/albums/{c_}").status_code == 200


def test_detail_shows_all_albums_and_neighbors_follow_the_album(appmod):
    conn = _db(appmod)
    a, b = _album(conn, "Alpha"), _album(conn, "Beta")
    i1 = _image(conn, "one", [a, b])
    i2 = _image(conn, "two", [b])
    i3 = _image(conn, "three", [a])
    conn.close()
    c = appmod.app.test_client()

    page = c.get(f"/image/{i1}").get_data(as_text=True)
    assert f"/albums/{a}" in page and f"/albums/{b}" in page and "Albums:" in page

    single = c.get(f"/image/{i2}").get_data(as_text=True)
    assert "Album:" in single and "Albums:" not in single

    # browsing inside Beta (newest first): i2 -> i1, and Alpha's photo i3 is never offered
    in_beta = c.get(f"/image/{i2}?album={b}").get_data(as_text=True)
    assert f"/image/{i1}?" in in_beta and f"/image/{i3}?" not in in_beta


def test_category_badge_and_search_by_any_album_name(appmod):
    conn = _db(appmod)
    a, b = _album(conn, "Alpha"), _album(conn, "Beta")
    _image(conn, "one", [a, b])
    conn.close()
    c = appmod.app.test_client()

    html = c.get("/category/bus").get_data(as_text=True)
    assert "Album: Alpha +1" in html

    for term in ("alpha", "beta"):                  # found through either album, no duplicate card
        hit = c.get(f"/category/bus?q={term}").get_data(as_text=True)
        assert hit.count('class="card"') == 1
    assert 'class="card"' not in c.get("/category/bus?q=zzz").get_data(as_text=True)


def test_edit_sets_several_albums_and_main_album(appmod):
    conn = _db(appmod)
    a, b, c_ = _album(conn, "Alpha"), _album(conn, "Beta"), _album(conn, "Gamma")
    iid = _image(conn, "one", [a])
    conn.close()
    cl = appmod.app.test_client()
    login(cl)

    def edit(album_ids):
        with cl.session_transaction() as s:
            token = s["_csrf"]
        data = {"title": "one", "caption": "", "tags": "", "category": "bus",
                "csrf_token": token, "album_ids": [str(i) for i in album_ids]}
        return cl.post(f"/image/{iid}/edit", data=data)

    form = cl.get(f"/image/{iid}/edit").get_data(as_text=True)
    assert 'name="album_ids"' in form and form.count("checked") >= 1

    assert edit([b, a, c_, b, 99999]).status_code == 302     # duplicates and unknown ids ignored
    conn = _db(appmod)
    got = {r["album_id"] for r in conn.execute("SELECT album_id FROM image_albums WHERE image_id=?", (iid,))}
    assert got == {a, b, c_}
    assert conn.execute("SELECT album_id FROM images WHERE id=?", (iid,)).fetchone()[0] == a  # main kept
    conn.close()

    assert edit([c_]).status_code == 302
    conn = _db(appmod)
    assert conn.execute("SELECT album_id FROM images WHERE id=?", (iid,)).fetchone()[0] == c_
    conn.close()

    assert edit([]).status_code == 302
    conn = _db(appmod)
    assert conn.execute("SELECT COUNT(*) FROM image_albums WHERE image_id=?", (iid,)).fetchone()[0] == 0
    assert conn.execute("SELECT album_id FROM images WHERE id=?", (iid,)).fetchone()[0] is None
    conn.close()


def test_delete_album_keeps_photos_and_picks_a_new_main_album(appmod):
    conn = _db(appmod)
    a, b = _album(conn, "Alpha"), _album(conn, "Beta")
    both = _image(conn, "both", [a, b], primary=a)
    only_a = _image(conn, "only_a", [a])
    conn.close()
    c = appmod.app.test_client()
    login(c)
    with c.session_transaction() as s:
        token = s["_csrf"]

    assert c.post(f"/albums/{a}/delete", data={"csrf_token": token}).status_code == 302
    conn = _db(appmod)
    assert conn.execute("SELECT album_id FROM images WHERE id=?", (both,)).fetchone()[0] == b
    assert conn.execute("SELECT album_id FROM images WHERE id=?", (only_a,)).fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 2
    assert [r["album_id"] for r in conn.execute("SELECT album_id FROM image_albums")] == [b]
    conn.close()


def test_delete_photo_removes_its_memberships(appmod):
    conn = _db(appmod)
    a, b = _album(conn, "Alpha"), _album(conn, "Beta")
    iid = _image(conn, "one", [a, b])
    conn.close()
    c = appmod.app.test_client()
    login(c)
    with c.session_transaction() as s:
        token = s["_csrf"]
    assert c.post(f"/image/{iid}/delete", data={"csrf_token": token}).status_code == 302
    conn = _db(appmod)
    assert conn.execute("SELECT COUNT(*) FROM image_albums").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0] == 2
    conn.close()


def test_new_upload_with_album_is_added_to_the_join_table(appmod):
    conn = _db(appmod)
    a = _album(conn, "Alpha")
    conn.close()
    c = appmod.app.test_client()
    login(c)
    with c.session_transaction() as s:
        token = s["_csrf"]
    r = c.post("/upload/file", data={
        "photo": (io.BytesIO(make_jpeg((1200, 800))), "x.jpg"), "category": "bus",
        "title": "x", "tags": "", "album_id": str(a)},
        headers={"X-CSRF-Token": token}, content_type="multipart/form-data")
    iid = r.get_json()["id"]
    conn = _db(appmod)
    assert [x["album_id"] for x in conn.execute(
        "SELECT album_id FROM image_albums WHERE image_id=?", (iid,))] == [a]
    conn.close()


def test_upload_into_several_albums_at_once(appmod):
    conn = _db(appmod)
    a, b, c3 = _album(conn, "Alpha"), _album(conn, "Beta"), _album(conn, "Gamma")
    conn.close()
    c = appmod.app.test_client()
    login(c)
    with c.session_transaction() as s:
        token = s["_csrf"]
    r = c.post("/upload/file", data={
        "photo": (io.BytesIO(make_jpeg((1200, 800))), "x.jpg"), "category": "bus", "title": "x", "tags": "",
        "album_ids": [str(b), str(a), "99999", "not-a-number", str(b)]},     # unknown / duplicate ids are ignored
        headers={"X-CSRF-Token": token}, content_type="multipart/form-data")
    body = r.get_json()
    assert body["ok"] is True
    assert set(body["timing"]) >= {"save", "check", "optimize", "thumbnail", "store", "database", "total"}
    iid = body["id"]
    conn = _db(appmod)
    got = [x["album_id"] for x in conn.execute(
        "SELECT album_id FROM image_albums WHERE image_id=? ORDER BY album_id", (iid,))]
    main = conn.execute("SELECT album_id FROM images WHERE id=?", (iid,)).fetchone()[0]
    conn.close()
    assert got == sorted([a, b])        # both albums, nothing else
    assert main == b                    # the first one listed is the main album
    # the photo shows up on both album pages
    for album in (a, b):
        assert f"/image/{iid}" in c.get(f"/albums/{album}").get_data(as_text=True)
    assert f"/image/{iid}" not in c.get(f"/albums/{c3}").get_data(as_text=True)
