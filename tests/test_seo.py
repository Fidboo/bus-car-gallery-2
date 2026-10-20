import re
import xml.etree.ElementTree as ET

from conftest import login, upload

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
      "image": "http://www.google.com/schemas/sitemap-image/1.1"}


def add_photo(appmod, title, tags="", category="bus"):
    """Insert a photo row directly (no image processing needed for SEO tests)."""
    conn = appmod.get_db()
    cur = conn.execute(
        "INSERT INTO images (title, tags, filename, thumbnail, uploaded_at, category) "
        "VALUES (?, ?, ?, ?, '2024-01-01T00:00:00', ?)",
        (title, tags, f"{title[:3]}.jpg", f"t_{title[:3]}.jpg", category))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def test_slugify_handles_danish_letters_and_junk(appmod):
    assert appmod.slugify("Volvo B10M, Århus 1998!") == "volvo-b10m-aarhus-1998"
    assert appmod.slugify("Ærø færge Ø") == "aeroe-faerge-oe"
    assert appmod.slugify("   ") == "photo"
    assert appmod.slugify("???") == "photo"
    assert len(appmod.slugify("word " * 40)) <= 60


def test_alt_text_title_and_tags(appmod):
    row = {"title": "Volvo B10M", "tags": "bus, volvo, b10m, sweden, 1998, extra"}
    # 'volvo' and 'b10m' are already in the title, so they are not repeated.
    assert appmod.image_alt(row) == "Volvo B10M - bus, sweden, 1998, extra"
    assert appmod.image_alt({"title": "Scania", "tags": ""}) == "Scania"
    long = {"title": "T" * 100, "tags": "alpha, bravo, charlie"}
    assert len(appmod.image_alt(long)) <= appmod.ALT_MAX_LEN


def test_detail_page_has_alt_canonical_and_jsonld(appmod):
    pid = add_photo(appmod, "Volvo B10M", "bus, sweden")
    c = appmod.app.test_client()
    html = c.get(f"/image/{pid}-volvo-b10m?cat=bus&q=sweden").get_data(as_text=True)
    assert 'alt="Volvo B10M - bus, sweden"' in html
    assert f'rel="canonical" href="http://localhost/image/{pid}-volvo-b10m"' in html
    assert '"@type": "ImageObject"' in html or '"@type":"ImageObject"' in html
    assert '<meta name="description"' in html


def test_old_id_url_redirects_301_and_keeps_query(appmod):
    pid = add_photo(appmod, "Volvo B10M", "bus")
    c = appmod.app.test_client()
    r = c.get(f"/image/{pid}?cat=car&q=x")
    assert r.status_code == 301
    assert r.headers["Location"].endswith(f"/image/{pid}-volvo-b10m?cat=car&q=x")
    # wrong slug (e.g. title edited later) is also corrected
    r = c.get(f"/image/{pid}-old-title")
    assert r.status_code == 301 and r.headers["Location"].endswith(f"/image/{pid}-volvo-b10m")
    assert c.get(f"/image/{pid}-volvo-b10m").status_code == 200
    assert c.get("/image/999999").status_code == 404


def test_grid_and_prev_next_links_use_clean_urls(appmod):
    a = add_photo(appmod, "First bus")
    b = add_photo(appmod, "Second bus")
    c = appmod.app.test_client()
    grid = c.get("/category/bus").get_data(as_text=True)
    assert f"/image/{a}-first-bus?cat=bus" in grid and f"/image/{b}-second-bus?cat=bus" in grid
    page = c.get(f"/image/{b}-second-bus?cat=bus").get_data(as_text=True)
    assert f"/image/{a}-first-bus?cat=bus" in page   # "Next" (older) photo, no redirect hop


def test_search_results_are_noindex_but_category_is_not(appmod):
    add_photo(appmod, "Bus")
    c = appmod.app.test_client()
    assert "noindex" in c.get("/category/bus?q=bus").get_data(as_text=True)
    assert "noindex" not in c.get("/category/bus").get_data(as_text=True)


def test_sitemap_index_and_chunks(appmod, monkeypatch):
    monkeypatch.setattr(appmod, "SITE_URL", "https://example.dk")
    monkeypatch.setattr(appmod, "SITEMAP_CHUNK", 2)
    ids = [add_photo(appmod, f"Bus number {i}") for i in range(5)]
    c = appmod.app.test_client()

    idx = ET.fromstring(c.get("/sitemap.xml").data)
    locs = [e.text for e in idx.findall("sm:sitemap/sm:loc", NS)]
    assert locs == ["https://example.dk/sitemap-pages.xml"] + [
        f"https://example.dk/sitemap-images-{n}.xml" for n in (1, 2, 3)]

    seen = []
    for n in (1, 2, 3):
        r = c.get(f"/sitemap-images-{n}.xml")
        assert r.mimetype == "application/xml"
        root = ET.fromstring(r.data)             # well-formed XML
        for u in root.findall("sm:url", NS):
            assert u.find("sm:loc", NS).text.startswith("https://example.dk/image/")
            assert u.find("image:image/image:loc", NS).text.startswith("http")
            seen.append(u.find("sm:loc", NS).text)
    assert len(seen) == 5 and len(set(seen)) == 5
    assert seen[0] == f"https://example.dk/image/{ids[0]}-bus-number-0"
    assert c.get("/sitemap-images-4.xml").status_code == 404


def test_sitemap_escapes_special_characters(appmod):
    pid = add_photo(appmod, "Tom & Jerry <bus>")
    c = appmod.app.test_client()
    ET.fromstring(c.get("/sitemap-images-1.xml").data)      # must still parse
    assert f"/image/{pid}-tom-jerry-bus" in c.get("/sitemap-images-1.xml").get_data(as_text=True)


def test_pages_sitemap_lists_categories_and_used_albums(appmod):
    pid = add_photo(appmod, "In album")
    conn = appmod.get_db()
    conn.execute("INSERT INTO albums (id, name, created_at) VALUES (1, 'Trip', 'x')")
    conn.execute("INSERT INTO image_albums (image_id, album_id) VALUES (?, 1)", (pid,))
    conn.commit(); conn.close()
    c = appmod.app.test_client()
    body = c.get("/sitemap-pages.xml").get_data(as_text=True)
    for path in ("/category/bus", "/category/car", "/albums", "/albums/1"):
        assert f"http://localhost{path}</loc>" in body


def test_robots_txt(appmod, monkeypatch):
    monkeypatch.setattr(appmod, "SITE_URL", "https://example.dk")
    r = appmod.app.test_client().get("/robots.txt")
    text = r.get_data(as_text=True)
    assert r.mimetype == "text/plain"
    assert "Sitemap: https://example.dk/sitemap.xml" in text
    assert "Disallow: /login" in text and "Disallow: /download/" in text


def test_sitemap_requests_do_not_count_as_visits(appmod):
    c = appmod.app.test_client()
    before = lambda: appmod.get_db().execute(
        "SELECT value FROM site_stats WHERE key='total_visits'").fetchone()[0]
    start = before()
    c.get("/sitemap.xml", headers={"User-Agent": "Mozilla/5.0"})
    c.get("/robots.txt", headers={"User-Agent": "Mozilla/5.0"})
    assert before() == start


def test_upload_flow_still_works_with_slug_urls(appmod):
    c = appmod.app.test_client()
    assert login(c).status_code == 302
    assert upload(c, title="Ærø bus", tags="bus").status_code == 200
    conn = appmod.get_db()
    pid = conn.execute("SELECT id FROM images WHERE title='Ærø bus'").fetchone()[0]
    conn.close()
    assert c.get(f"/image/{pid}-aeroe-bus").status_code == 200
    # editing the title keeps the old address working through a 301
    # (first the old URL, then the corrected one)
    assert c.get(f"/image/{pid}").status_code == 301
