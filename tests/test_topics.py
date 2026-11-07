"""Topic pages (/tag/...), the topic list, related photos and licence data."""
import json
import re

from test_multi_album import _db, _image


def _tagged(conn, title, tags, taken=None):
    iid = _image(conn, title)
    conn.execute("UPDATE images SET tags = ?, photo_taken_date = ? WHERE id = ?", (tags, taken, iid))
    conn.commit()
    return iid


def test_topic_page_lists_photos_and_follows_edits(appmod):
    conn = _db(appmod)
    a = _tagged(conn, "Volvo 8900LE no 2117", "Arriva, Volvo 8900LE, 2117", "2016-07-12")
    b = _tagged(conn, "Volvo 8900LE no 2118", "arriva, volvo 8900le", "2019-01-01")
    c = _tagged(conn, "Setra S415", "Arriva , setra", "2012-05-01")
    _tagged(conn, "Scania", "Keolis")
    client = appmod.app.test_client()
    html = client.get("/tag/arriva").get_data(as_text=True)
    assert "<h1>Arriva</h1>" in html and "3 photos" in html and "2012–2019" in html
    for t in ("Volvo 8900LE no 2117", "Volvo 8900LE no 2118", "Setra S415"):
        assert t in html
    assert "Scania" not in html and "noindex" not in html
    assert 'href="/tag/volvo-8900le"' in html                     # related topic
    # only 2 photos -> page works but isn't indexed
    assert 'content="noindex, follow"' in client.get("/tag/volvo-8900le").get_data(as_text=True)
    assert client.get("/tag/nothing-here").status_code == 404
    assert client.get("/tag/Arriva").status_code == 301              # one address per topic
    # editing tags (here directly in the database, like a tool would) is picked up
    conn.execute("UPDATE images SET tags = 'Keolis' WHERE id = ?", (c,)); conn.commit()
    appmod._topic_cache.clear()
    assert "2 photos" in client.get("/tag/arriva").get_data(as_text=True)
    conn.execute("DELETE FROM images WHERE id = ?", (a,)); conn.commit()
    appmod._topic_cache.clear()
    assert "1 photo " in client.get("/tag/arriva").get_data(as_text=True) or \
        '1 photo tagged' in client.get("/tag/arriva").get_data(as_text=True)
    assert b


def test_topic_list_sitemap_and_pagination(appmod):
    conn = _db(appmod)
    for i in range(30):
        _tagged(conn, f"Bus {i}", "Movia, bus" if i % 2 else "bus")
    _tagged(conn, "Lonely", "rare-one")
    client = appmod.app.test_client()
    html = client.get("/tags").get_data(as_text=True)
    assert 'href="/tag/bus"' in html and 'href="/tag/movia"' in html
    assert "rare-one" not in html                                     # under 3 photos
    sm = client.get("/sitemap-pages.xml").get_data(as_text=True)
    assert "/tag/bus</loc>" in sm and "/tags</loc>" in sm and "rare-one" not in sm
    p1 = client.get("/tag/bus").get_data(as_text=True)
    assert "Page 1 of 2" in p1 and 'href="/tag/bus?page=2"' in p1
    p2 = client.get("/tag/bus?page=2").get_data(as_text=True)
    assert 'rel="canonical" href="http://localhost/tag/bus?page=2"' in p2 or "/tag/bus?page=2\"" in p2
    assert client.get("/tag/bus?page=99").status_code == 302


def test_photo_page_related_photos_tag_links_and_licence(appmod):
    conn = _db(appmod)
    me = _tagged(conn, "Volvo 8900LE no 2117", "Arriva, Volvo 8900LE, bus", "2016-07-12")
    same_model = [_tagged(conn, f"8900LE {i}", "Volvo 8900LE, bus") for i in range(3)]
    _tagged(conn, "Other operator", "Keolis, bus")
    for i in range(8):
        _tagged(conn, f"Arriva {i}", "Arriva, bus")
    client = appmod.app.test_client()
    html = client.get(f"/image/{me}", follow_redirects=True).get_data(as_text=True)
    related = html[html.index('class="related-section"'):html.index('id="comments"')]
    # most specific topic first (Volvo 8900LE: 4 photos), then Arriva (9 photos)
    assert related.index('"Volvo 8900LE"') < related.index('"Arriva"')
    assert "See all 4" in related and "See all 9" in related
    assert "Other operator" not in related and f"/image/{me}-" not in related
    assert all(f"/image/{i}-" in related for i in same_model)
    assert 'href="/tag/arriva"' in html and "category/bus?q=" not in html     # tags link to topic pages
    ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S).group(1))
    assert ld["license"].endswith("/license") and ld["acquireLicensePage"].endswith("/license")
    assert ld["creditText"] and ld["copyrightNotice"].startswith("©") and ld["dateCreated"] == "2016-07-12"
    assert "Using the photos" in client.get("/license").get_data(as_text=True)
