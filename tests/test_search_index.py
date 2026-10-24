"""Word search (FTS5) stays in step with the photos, and tag lists are saved."""
import sqlite3

from test_multi_album import _album, _db, _image


def _ids(appmod, q, category="bus"):
    conn = _db(appmod)
    where, params = appmod.build_filters(category=category, query=q)
    ids = [r[0] for r in conn.execute(f"SELECT images.id FROM images WHERE {where}", params)]
    conn.close()
    return set(ids)


def test_prefix_and_all_words_must_match(appmod):
    assert appmod.FTS_OK
    conn = _db(appmod)
    a = _image(conn, "Volvo B12BLE Aabenraa")
    b = _image(conn, "Setra S415 Norway")
    assert _ids(appmod, "volv") == {a}
    assert _ids(appmod, "VOLVO aaben") == {a}
    assert _ids(appmod, "volvo norway") == set()
    assert _ids(appmod, "b12ble") == {a}
    assert _ids(appmod, "") != set()          # no search = everything
    assert _ids(appmod, '"; DROP TABLE images; --') == set()
    assert _ids(appmod, "***") == set()


def test_index_follows_edits_deletes_and_albums(appmod):
    conn = _db(appmod)
    alb = _album(conn, "Greenland trip")
    iid = _image(conn, "plain title", album_ids=[alb])
    assert _ids(appmod, "greenl") == {iid}                      # found through its album
    conn.execute("UPDATE albums SET name = 'Iceland trip' WHERE id = ?", (alb,)); conn.commit()
    assert _ids(appmod, "greenl") == set() and _ids(appmod, "iceland") == {iid}
    conn.execute("UPDATE images SET tags = 'scania, rare', caption = 'on the quay' WHERE id = ?", (iid,)); conn.commit()
    assert _ids(appmod, "scania") == {iid} and _ids(appmod, "quay") == {iid}
    conn.execute("DELETE FROM image_albums WHERE image_id = ?", (iid,)); conn.commit()
    assert _ids(appmod, "iceland") == set()
    conn.execute("DELETE FROM images WHERE id = ?", (iid,)); conn.commit()
    assert _ids(appmod, "scania") == set()
    assert conn.execute("SELECT COUNT(*) FROM images_fts").fetchone()[0] == 0


def test_index_is_built_for_an_existing_database(appmod):
    conn = _db(appmod)
    iid = _image(conn, "Leyland DAB")
    conn.execute("DROP TABLE images_fts")                         # an old database has none
    for t in ("ai", "au", "ad", "ia_i", "ia_d", "alb_u"):
        conn.execute(f"DROP TRIGGER IF EXISTS images_fts_{t}")
    conn.commit()
    appmod.init_db()
    assert _ids(appmod, "leyl") == {iid}


def test_category_page_search_and_saved_tags(appmod):
    conn = _db(appmod)
    iid = _image(conn, "Volvo 9700")
    conn.execute("UPDATE images SET tags = 'volvo, 9700' WHERE id = ?", (iid,)); conn.commit()
    client = appmod.app.test_client()
    assert b"Volvo 9700" in client.get("/category/bus?q=volv").data
    assert b"Volvo 9700" not in client.get("/category/bus?q=scania").data
    appmod._tag_cache.clear()
    assert appmod.top_tags_for(conn, "bus") == ["9700", "volvo"]
    assert conn.execute("SELECT n_images FROM tag_cache WHERE category = 'bus'").fetchone()[0] == 1
    appmod._tag_cache.clear()                                      # "restart": served from the saved copy
    conn.execute("UPDATE tag_cache SET tags = 'saved-copy' WHERE category = 'bus'"); conn.commit()
    assert appmod.top_tags_for(conn, "bus") == ["saved-copy"]
    _image(conn, "Another")                                        # photo count changed -> recount
    appmod._tag_cache.clear()
    assert appmod.top_tags_for(conn, "bus") == ["9700", "volvo"]
    appmod.invalidate_tag_cache()
    assert conn.execute("SELECT COUNT(*) FROM tag_cache").fetchone()[0] == 0
