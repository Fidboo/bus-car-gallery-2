"""Stats page shows Cloudflare Web Analytics numbers (API mocked)."""
import io
import json
import urllib.request

from conftest import login
from test_multi_album import _db, _image


def _fake_payload():
    def total(v, n):
        return [{"count": v, "sum": {"visits": n}}]
    return {"data": {"viewer": {"accounts": [{
        "day": total(40, 12), "week": total(300, 90), "month": total(1200, 350),
        "daily": [{"count": 10, "sum": {"visits": 4}, "dimensions": {"date": "2026-10-29"}},
                  {"count": 30, "sum": {"visits": 8}, "dimensions": {"date": "2026-10-30"}}],
        "pages": [{"count": 99, "dimensions": {"requestPath": "/image/1-volvo-b10m"}},
                  {"count": 50, "dimensions": {"requestPath": "/category/bus"}},
                  {"count": 20, "dimensions": {"requestPath": "/"}}],
        "countries": [{"count": 900, "sum": {"visits": 300}, "dimensions": {"countryName": "Denmark"}}],
        "referers": [{"count": 70, "dimensions": {"refererHost": "www.google.com"}},
                     {"count": 5, "dimensions": {"refererHost": ""}}],
    }]}}, "errors": None}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_stats_page_shows_cloudflare_numbers(appmod, monkeypatch):
    conn = _db(appmod)
    _image(conn, "Volvo B10M")
    conn.close()
    monkeypatch.setattr(appmod, "CF_API_TOKEN", "tok")
    monkeypatch.setattr(appmod, "CF_ACCOUNT_ID", "acc123")
    appmod._cf_cache.update(until=0, data=None)
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["auth"] = req.headers.get("Authorization")
        sent["query"] = json.loads(req.data)["query"]
        return _Resp(json.dumps(_fake_payload()).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    c = appmod.app.test_client()
    login(c)
    html = c.get("/stats").get_data(as_text=True)
    assert sent["auth"] == "Bearer tok" and '"acc123"' in sent["query"]
    assert 'requestHost: "b12ble.com"' in sent["query"]
    for text in ("350", "1200 page views", "Last 24 hours", "Volvo B10M", "Buses (category)",
                 "Front page", "Denmark", "www.google.com", "(direct / none)", "This site's own counter"):
        assert text in html, text
    # cached: a second view doesn't ask Cloudflare again
    sent.clear()
    c.get("/stats")
    assert sent == {}
    appmod._cf_cache.update(until=0, data=None)


def test_stats_page_without_or_with_broken_cloudflare(appmod, monkeypatch):
    c = appmod.app.test_client()
    login(c)
    appmod._cf_cache.update(until=0, data=None)
    assert "not set up yet" in c.get("/stats").get_data(as_text=True)
    monkeypatch.setattr(appmod, "CF_API_TOKEN", "bad")
    monkeypatch.setattr(appmod, "CF_ACCOUNT_ID", "acc")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Resp(
        json.dumps({"data": None, "errors": [{"message": "not authorized"}]}).encode()))
    html = c.get("/stats").get_data(as_text=True)
    assert "Could not fetch" in html and "not authorized" in html
    assert "Total page views" in html               # own counter still shown
    appmod._cf_cache.update(until=0, data=None)


def test_links_and_sitemap_use_https_and_no_empty_q(appmod, monkeypatch):
    conn = _db(appmod)
    for i in range(30):
        _image(conn, f"Bus {i}")
    conn.close()
    monkeypatch.setattr(appmod, "IS_DEV", False)
    c = appmod.app.test_client()
    html = c.get("/category/bus").get_data(as_text=True)
    assert "/category/bus?page=2" in html and "q=&" not in html and "?q=&" not in html
    assert 'rel="canonical" href="https://' in html
    assert "<loc>https://" in c.get("/sitemap-images-1.xml").get_data(as_text=True)
    robots = c.get("/robots.txt").get_data(as_text=True)
    assert "Sitemap: https://" in robots and "Allow: /*?q=&" in robots
