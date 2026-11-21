"""Stats: all comments in one searchable list, and the tidied overview."""
from conftest import login
from test_multi_album import _db, _image


def _comment(conn, image_id, name, text, when="2026-11-20T10:00:00"):
    cid = conn.execute("INSERT INTO comments (image_id, name, comment, created_at) VALUES (?, ?, ?, ?)",
                       (image_id, name, text, when)).lastrowid
    conn.commit()
    return cid


def test_comments_page_lists_finds_and_deletes(appmod):
    conn = _db(appmod)
    volvo = _image(conn, "1991' Volvo 240 GL")
    setra = _image(conn, "Setra S415")
    old = _comment(conn, volvo, "Hans", "Nice old Volvo!", "2020-01-01T10:00:00")
    for i in range(35):
        _comment(conn, setra, f"Guest {i}", f"Comment number {i}")
    gone = _image(conn, "Deleted photo")
    _comment(conn, gone, "Eva", "On a photo that is gone")
    conn.execute("DELETE FROM images WHERE id = ?", (gone,))
    conn.commit()

    c = appmod.app.test_client()
    assert c.get("/stats/comments").status_code in (302, 401, 403)          # owner only
    login(c)
    html = c.get("/stats/comments").get_data(as_text=True)
    assert "37 comments in total" in html and "Page 1 of 2" in html
    assert html.index("On a photo that is gone") < html.index("Comment number 34")   # newest first
    assert "The photo has been deleted." in html
    assert f"/image/{setra}-setra-s415#c" in html                             # straight to the comment

    found = c.get("/stats/comments?q=volvo").get_data(as_text=True)              # photo title
    assert "Nice old Volvo!" in found and "Comment number" not in found and "1 of 37" in found
    assert "Nice old Volvo!" in c.get("/stats/comments?q=hans").get_data(as_text=True)   # name
    assert "No comments match" in c.get("/stats/comments?q=100%25_x").get_data(as_text=True)

    # deleting from the list returns to the list (same search)
    with c.session_transaction() as s:
        token = s["_csrf"]
    r = c.post(f"/comment/{old}/delete", data={"csrf_token": token, "next": "/stats/comments?q=volvo"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/stats/comments?q=volvo")
    r = c.post(f"/comment/{old + 1}/delete", data={"csrf_token": token, "next": "https://evil.example/"})
    assert f"/image/{setra}" in r.headers["Location"]                         # no outside redirects
    assert conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 35

    # the photo page has an anchor for each comment
    page = c.get(f"/image/{setra}", follow_redirects=True).get_data(as_text=True)
    assert f'id="c{old + 2}"' in page


def test_overview_shows_latest_comments_and_tabs(appmod):
    conn = _db(appmod)
    iid = _image(conn, "Volvo 8900LE")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    _comment(conn, iid, "Ole", "Great shot", now)
    c = appmod.app.test_client()
    login(c)
    html = c.get("/stats").get_data(as_text=True)
    assert 'class="stats-tabs"' in html and "1 new" in html
    assert "Latest comments" in html and "Great shot" in html and "See all 1" in html
    assert "Technical details" in html and "noindex" in html
