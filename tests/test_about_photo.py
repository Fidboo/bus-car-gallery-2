"""'About this photo' text and ImageObject JSON-LD on photo pages."""
import json
import re

from test_multi_album import _album, _db, _image

BAD = ("None", "null", "undefined", "“”", " ,", ",,", "..", " .", "  ")


def _page(appmod, iid):
    html = appmod.app.test_client().get(f"/image/{iid}", follow_redirects=True).get_data(as_text=True)
    about = re.search(r'<div class="about-photo">\s*<h2>About this photo</h2>\s*<p>(.*?)</p>', html, re.S)
    ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S).group(1))
    return html, (about.group(1).strip() if about else ""), ld


def _clean(text):
    for bad in BAD:
        assert bad not in text, (bad, text)


def test_full_photo_gets_a_complete_natural_text(appmod):
    conn = _db(appmod)
    iid = _image(conn, "1991' Volvo 240 GL 2,0")
    conn.execute("UPDATE images SET category='car', caption='Copenhagen. De-reg. 02/2016', tags='Volvo, 240, denmark', "
                 "photo_taken_date='2015-11-08T10:22:00' WHERE id=?", (iid,))
    other = _image(conn, "1990' Volvo 240")
    conn.execute("UPDATE images SET category='car', tags='Volvo, 240', photo_taken_date='2015-11-08T11:00:00' WHERE id=?", (other,))
    alb = _album(conn, "Volvo 240")
    conn.execute("INSERT INTO image_albums (image_id, album_id) VALUES (?, ?)", (iid, alb))
    conn.commit()
    html, about, ld = _page(appmod, iid)
    _clean(about.replace("&#39;", "'"))
    for part in ("“1991&#39; Volvo 240 GL 2,0” is one of 2 photos in the Cars section of Vehicle Gallery",
                 "It was taken on 8 November 2015, and one more photo in the collection was taken the same day.",
                 "The photographer&#39;s note reads: “Copenhagen. De-reg. 02/2016”.",
                 "It is part of the album “Volvo 240”.",
                 "It is tagged Volvo, 240 and denmark.",
                 "The Cars section has 1 other photo tagged"):
        assert part in about, part
    assert ld["@type"] == "ImageObject" and ld["name"] == "1991' Volvo 240 GL 2,0"
    assert ld["dateCreated"] == "2015-11-08" and ld["url"].endswith(f"/image/{iid}-1991-volvo-240-gl-2-0")
    assert "?" not in ld["url"] and ld["contentUrl"].startswith("http")
    assert ld["author"]["name"] and ld["license"].endswith("/license") and "personal use" in ld["copyrightNotice"]
    assert ld["description"].startswith("“1991' Volvo 240")
    assert '<meta name="description" content="“1991&#39; Volvo 240' in html


def test_photo_with_almost_nothing_still_reads_well(appmod):
    conn = _db(appmod)
    iid = _image(conn, "IMG 2034")
    conn.execute("UPDATE images SET caption='', tags=' , ,', photo_taken_date=NULL WHERE id=?", (iid,))
    conn.commit()
    html, about, ld = _page(appmod, iid)
    _clean(about)
    assert about == "“IMG 2034” is a photo in Vehicle Gallery, a personal collection of vehicle photos."
    assert "dateCreated" not in ld and None not in ld.values()


def test_odd_values_never_leak(appmod):
    conn = _db(appmod)
    iid = _image(conn, "  Bus   with  spaces ")
    conn.execute("UPDATE images SET caption='  ', photo_taken_date='unknown', tags='None' WHERE id=?", (iid,))
    for n in range(5):
        a = _album(conn, f"Album {n}")
        conn.execute("INSERT INTO image_albums (image_id, album_id) VALUES (?, ?)", (iid, a))
    conn.commit()
    _, about, ld = _page(appmod, iid)
    assert "taken on" not in about and "dateCreated" not in ld
    assert "“Bus with spaces”" in about
    assert "albums “Album 0”, “Album 1”, “Album 2” and 2 more." in about
