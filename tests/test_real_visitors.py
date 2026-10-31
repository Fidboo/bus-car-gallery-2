"""'Real visitors': only human page views are counted, per day, with country
and the website people came from."""
import json

from conftest import login
from test_multi_album import _db, _image

BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36",
           "Accept-Language": "da-DK,da;q=0.9", "CF-IPCountry": "DK"}


def send(client, path, headers=None, **extra):
    body = {"p": path, "h": 1, **extra}
    return client.post("/api/hit", data=json.dumps(body), headers=headers or BROWSER, content_type="text/plain")


def rows(appmod, sql):
    conn = _db(appmod)
    out = conn.execute(sql).fetchall()
    conn.close()
    return [tuple(r) for r in out]


def test_only_people_are_counted(appmod):
    conn = _db(appmod)
    iid = _image(conn, "Volvo B10M")
    conn.close()
    page = f"/image/{iid}-volvo-b10m"
    person = appmod.app.test_client()
    assert send(person, page, r="www.google.com").status_code == 204
    send(person, page)                                              # same person again: not counted twice
    send(appmod.app.test_client(), "/category/bus", headers={**BROWSER, "CF-IPCountry": "SE", "User-Agent": BROWSER["User-Agent"] + " other"})

    # not counted: no proof of a person, no language, automated browsers, odd paths
    c = appmod.app.test_client()
    c.post("/api/hit", data=page, headers=BROWSER, content_type="text/plain")          # old-style body, no "h"
    send(c, page, headers={**BROWSER, "Accept-Language": ""})
    send(c, page, headers={**BROWSER, "User-Agent": "Mozilla/5.0 HeadlessChrome/120"})
    send(c, page, headers={**BROWSER, "User-Agent": "python-requests/2.31"})
    send(c, "/login")
    c.post("/api/hit", data="{broken json", headers=BROWSER, content_type="text/plain")

    assert sorted(rows(appmod, "SELECT path, country, views FROM daily_views")) == [
        ("/category/bus", "SE", 1), (page, "DK", 1)]
    assert rows(appmod, "SELECT COUNT(*) FROM daily_visitors") == [(2,)]
    assert rows(appmod, "SELECT host, views FROM daily_refs") == [("www.google.com", 1)]
    assert rows(appmod, f"SELECT views FROM images WHERE id = {iid}") == [(1,)]
    # no IP address is stored anywhere
    assert all(len(v) == 16 for (v,) in rows(appmod, "SELECT visitor FROM daily_visitors"))


def test_own_site_and_bad_referrers_are_ignored(appmod):
    c = appmod.app.test_client()
    send(c, "/", r="b12ble.com")
    send(appmod.app.test_client(), "/albums", r="<script>")
    assert rows(appmod, "SELECT COUNT(*) FROM daily_refs") == [(0,)]


def test_stats_page_shows_real_visitors(appmod):
    conn = _db(appmod)
    iid = _image(conn, "Setra S415")
    conn.close()
    for n in range(3):                                   # three different people
        send(appmod.app.test_client(), f"/image/{iid}-setra-s415",
             headers={**BROWSER, "User-Agent": BROWSER["User-Agent"] + f" n{n}"})
    owner = appmod.app.test_client()
    login(owner)
    html = owner.get("/stats?days=7").get_data(as_text=True)
    start = html.index("Real visitors")
    real = html[start:html.index("For comparison")]
    assert "Setra S415" in real and "Denmark" in real
    assert '<div class="stat-number">3</div>' in real   # 3 visitors today
    assert real.count('class="cf-day"') in (7, 8)
    assert "Nobody came from another website" in real
