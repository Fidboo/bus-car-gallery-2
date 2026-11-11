"""One address per page for search engines: canonical tags, www -> non-www
redirects and sitemaps all use https://b12ble.com (as in production)."""
import re

import pytest

from test_multi_album import _album, _db, _image


@pytest.fixture
def prod(appmod, monkeypatch):
    monkeypatch.setattr(appmod, "IS_DEV", False)
    monkeypatch.setattr(appmod, "SITE_URL", "")
    conn = _db(appmod)
    for i in range(30):
        iid = _image(conn, f"Volvo 740 {i}")
        conn.execute("UPDATE images SET category = 'car', tags = 'volvo, 740' WHERE id = ?", (iid,))
    alb = _album(conn, "Trip")
    conn.execute("INSERT INTO image_albums (image_id, album_id) VALUES (?, ?)", (iid, alb))
    conn.commit()
    conn.close()
    appmod._topic_cache.clear()
    return appmod, iid, alb


def canonical(client, url):
    r = client.get(url, base_url="https://b12ble.com")
    assert r.status_code == 200, (url, r.status_code)
    found = re.findall(r'<link rel="canonical" href="([^"]+)"', r.get_data(as_text=True))
    assert len(found) == 1, (url, found)
    return found[0]


def test_every_page_has_one_clean_https_canonical(prod):
    appmod, iid, alb = prod
    c = appmod.app.test_client()
    B = "https://b12ble.com"
    assert canonical(c, "/?utm_source=facebook") == B + "/"
    assert canonical(c, "/category/car") == B + "/category/car"
    assert canonical(c, "/category/car?page=2&utm_source=x&fbclid=y") == B + "/category/car?page=2"
    assert canonical(c, "/category/car?q=volvo&page=2") == B + "/category/car"       # a search -> the category
    assert canonical(c, "/albums?q=trip") == B + "/albums"
    assert canonical(c, f"/albums/{alb}?gclid=1") == B + f"/albums/{alb}"
    assert canonical(c, f"/image/{iid}-volvo-740-29?cat=car&q=volvo&utm_medium=x") == B + f"/image/{iid}-volvo-740-29"
    assert canonical(c, "/tag/volvo?cat=car&page=2") == B + "/tag/volvo"
    assert canonical(c, "/tag/volvo?page=2") == B + "/tag/volvo?page=2"
    assert canonical(c, "/tags?x=1") == B + "/tags"
    assert canonical(c, "/license") == B + "/license"


def test_site_url_with_www_or_http_still_gives_the_canonical_address(prod, monkeypatch):
    appmod, _, _ = prod
    monkeypatch.setattr(appmod, "SITE_URL", "http://www.b12ble.com")
    assert canonical(appmod.app.test_client(), "/category/car") == "https://b12ble.com/category/car"


def test_www_and_fly_dev_redirect_301_to_the_same_path(prod):
    appmod, _, _ = prod
    c = appmod.app.test_client()
    for base in ("https://www.b12ble.com", "http://www.b12ble.com", "https://bus-car-gallery-2.fly.dev"):
        r = c.get("/category/car?page=2", base_url=base)
        assert r.status_code == 301 and r.headers["Location"] == "https://b12ble.com/category/car?page=2", base
        r = c.get("/", base_url=base)
        assert r.headers["Location"] == "https://b12ble.com/"
    assert c.get("/category/car", base_url="https://b12ble.com").status_code == 200
    assert c.get("/healthz", base_url="https://www.b12ble.com").status_code == 200    # health checks untouched


def test_all_sitemaps_use_only_https_b12ble_com(prod):
    appmod, _, _ = prod
    c = appmod.app.test_client()
    index = c.get("/sitemap.xml", base_url="https://www.b12ble.com/").status_code
    assert index == 301                                            # even the sitemap lives at one address
    urls = []
    for path in ["/sitemap.xml"]:
        body = c.get(path, base_url="https://b12ble.com").get_data(as_text=True)
        subs = re.findall(r"<loc>([^<]+)</loc>", body)
        urls += subs
        for sub in subs:
            urls += re.findall(r"<loc>([^<]+)</loc>",
                               c.get(sub.replace("https://b12ble.com", ""), base_url="https://b12ble.com").get_data(as_text=True))
    assert len(urls) > 30
    assert all(u.startswith("https://b12ble.com/") for u in urls), [u for u in urls if not u.startswith("https://b12ble.com/")][:5]
    robots = c.get("/robots.txt", base_url="https://b12ble.com").get_data(as_text=True)
    assert "Sitemap: https://b12ble.com/sitemap.xml" in robots
