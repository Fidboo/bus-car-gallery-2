"""R2 mode, tested against a local S3-compatible server (moto)."""
import os
import urllib.request
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from moto.server import ThreadedMotoServer

from conftest import load_app, login, make_jpeg, upload

BUCKET = "bus-gallery-test"
PUBLIC = "https://billeder.example.dk"


@pytest.fixture(scope="module")
def s3_server():
    server = ThreadedMotoServer(port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def r2app(tmp_path, monkeypatch, s3_server):
    client = boto3.client("s3", endpoint_url=s3_server, aws_access_key_id="k",
                          aws_secret_access_key="s", region_name="us-east-1")
    try:
        client.create_bucket(Bucket=BUCKET)
    except Exception:
        pass
    # start every test with an empty bucket
    for obj in client.list_objects_v2(Bucket=BUCKET).get("Contents", []):
        client.delete_object(Bucket=BUCKET, Key=obj["Key"])
    appmod = load_app(
        tmp_path, monkeypatch,
        R2_BUCKET=BUCKET, R2_ENDPOINT_URL=s3_server, R2_ACCESS_KEY_ID="k",
        R2_SECRET_ACCESS_KEY="s", R2_PUBLIC_URL=PUBLIC,
    )
    appmod.s3 = client
    return appmod


def keys(appmod):
    return sorted(o["Key"] for o in appmod.s3.list_objects_v2(Bucket=BUCKET).get("Contents", []))


def test_upload_goes_to_r2_not_local_disk(r2app):
    c = r2app.app.test_client()
    login(c)
    r = upload(c, title="Scania")
    assert r.get_json()["ok"]
    ks = keys(r2app)
    assert len(ks) == 2
    assert sum(k.startswith("uploads/") for k in ks) == 1
    assert sum(k.startswith("thumbnails/") for k in ks) == 1
    # nothing left on the app server
    assert not os.path.exists(r2app.UPLOAD_FOLDER) or os.listdir(r2app.UPLOAD_FOLDER) == []
    assert os.listdir(r2app.TMP_FOLDER) == []

    up = next(k for k in ks if k.startswith("uploads/"))
    head = r2app.s3.head_object(Bucket=BUCKET, Key=up)
    assert head["ContentType"] == "image/jpeg"
    assert "immutable" in head["CacheControl"]
    # original stored byte for byte
    assert r2app.s3.get_object(Bucket=BUCKET, Key=up)["Body"].read()[:2] == b"\xff\xd8"


def test_pages_point_browsers_at_r2(r2app):
    c = r2app.app.test_client()
    login(c)
    image_id = upload(c).get_json()["id"]
    page = c.get("/category/bus")
    html = page.get_data(as_text=True)
    assert f"{PUBLIC}/thumbnails/" in html and "/media/" not in html
    assert PUBLIC in page.headers["Content-Security-Policy"]
    detail = c.get(f"/image/{image_id}").get_data(as_text=True)
    assert f"{PUBLIC}/uploads/" in detail
    assert c.get("/media/uploads/x.jpg").status_code == 404   # local route disabled


def test_download_redirects_to_signed_attachment_link(r2app):
    c = r2app.app.test_client()
    login(c)
    image_id = upload(c, title="Mercedes O305").get_json()["id"]
    r = c.get(f"/download/{image_id}")
    assert r.status_code == 302
    loc = r.headers["Location"]
    q = parse_qs(urlparse(loc).query)
    assert "attachment" in q["response-content-disposition"][0]
    assert "Mercedes_O305.jpg" in q["response-content-disposition"][0]
    assert "Signature" in loc or "X-Amz-Signature" in loc
    # follow it for real: file comes back with a save-as header
    with urllib.request.urlopen(loc) as resp:
        body = resp.read()
        assert "attachment" in resp.headers.get("Content-Disposition", "")
    assert body[:2] == b"\xff\xd8"


def test_delete_removes_objects(r2app):
    c = r2app.app.test_client()
    login(c)
    image_id = upload(c).get_json()["id"]
    assert len(keys(r2app)) == 2
    with c.session_transaction() as s:
        token = s["_csrf"]
    c.post(f"/image/{image_id}/delete", data={"csrf_token": token})
    assert keys(r2app) == []


def test_storage_failure_gives_clean_error(r2app):
    c = r2app.app.test_client()
    login(c)
    r2app.s3.delete_bucket(Bucket=BUCKET)           # simulate R2 trouble
    r = upload(c)
    assert r.status_code == 400 and "storage" in r.get_json()["error"].lower()
    conn = r2app.get_db()
    assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0
    conn.close()
    assert os.listdir(r2app.TMP_FOLDER) == []
    r2app.s3.create_bucket(Bucket=BUCKET)


def test_edit_replaces_objects(r2app):
    import io
    c = r2app.app.test_client()
    login(c)
    image_id = upload(c).get_json()["id"]
    before = keys(r2app)
    with c.session_transaction() as s:
        token = s["_csrf"]
    c.post(f"/image/{image_id}/edit", data={
        "title": "t", "caption": "", "tags": "", "category": "bus", "album_id": "",
        "photo": (io.BytesIO(make_jpeg((1000, 700))), "n.jpg"), "csrf_token": token},
        content_type="multipart/form-data")
    after = keys(r2app)
    assert len(after) == 2 and set(after).isdisjoint(before)


def test_r2_settings_must_be_complete(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="R2_PUBLIC_URL"):
        load_app(tmp_path, monkeypatch, R2_BUCKET="b", R2_ACCOUNT_ID="a",
                 R2_ACCESS_KEY_ID="k", R2_SECRET_ACCESS_KEY="s")


def test_check_r2_tool(r2app, tmp_path, monkeypatch, capsys):
    """tools/check_r2.py finds missing files and uploads them."""
    import subprocess, sys, shutil, sqlite3
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # a "legacy" data folder: 2 photos in the DB + files on disk, nothing in R2
    legacy = tmp_path / "legacy"
    (legacy / "uploads").mkdir(parents=True)
    (legacy / "thumbnails").mkdir()
    conn = sqlite3.connect(legacy / "gallery.db")
    conn.execute("CREATE TABLE images (id INTEGER PRIMARY KEY, filename TEXT, thumbnail TEXT)")
    for i in (1, 2):
        (legacy / "uploads" / f"o{i}.jpg").write_bytes(make_jpeg((400, 300)))
        (legacy / "thumbnails" / f"t{i}.jpg").write_bytes(make_jpeg((100, 80)))
        conn.execute("INSERT INTO images VALUES (?, ?, ?)", (i, f"o{i}.jpg", f"t{i}.jpg"))
    conn.commit(); conn.close()

    env = dict(os.environ, R2_BUCKET=BUCKET, R2_ENDPOINT_URL=os.environ["R2_ENDPOINT_URL"],
               R2_ACCESS_KEY_ID="k", R2_SECRET_ACCESS_KEY="s", R2_PUBLIC_URL=PUBLIC)
    run = lambda *a: subprocess.run([sys.executable, "tools/check_r2.py", "--db", str(legacy / "gallery.db"), *a],
                                    cwd=root, env=env, capture_output=True, text=True)
    first = run()
    assert first.returncode == 1 and "4 files are missing" in first.stdout
    fixed = run("--upload-missing", str(legacy))
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert run().returncode == 0 and "OK" in run().stdout
    assert keys(r2app) == ["thumbnails/t1.jpg", "thumbnails/t2.jpg", "uploads/o1.jpg", "uploads/o2.jpg"]


def test_backup_and_restore_roundtrip(r2app, tmp_path):
    import sqlite3, subprocess, sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    c = r2app.app.test_client()
    login(c)
    upload(c, title="Backup me")
    env = dict(os.environ, R2_BUCKET=BUCKET, R2_ENDPOINT_URL=os.environ["R2_ENDPOINT_URL"],
               R2_ACCESS_KEY_ID="k", R2_SECRET_ACCESS_KEY="s", R2_PUBLIC_URL=PUBLIC,
               DATA_DIR=r2app.DATA_DIR)
    out = subprocess.run([sys.executable, "tools/backup_db.py"], cwd=root, env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert any(k.startswith("backups/gallery-") for k in keys(r2app))

    new_dir = tmp_path / "newserver"
    env["DATA_DIR"] = str(new_dir)
    out = subprocess.run([sys.executable, "tools/restore_db.py", "--latest"], cwd=root, env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    conn = sqlite3.connect(new_dir / "gallery.db")
    assert conn.execute("SELECT title FROM images").fetchone()[0] == "Backup me"
    conn.close()
    again = subprocess.run([sys.executable, "tools/restore_db.py", "--latest"], cwd=root, env=env,
                           capture_output=True, text=True)
    assert again.returncode != 0 and "--force" in again.stderr + again.stdout


def test_backup_due_logic_and_retention(r2app, tmp_path):
    import backup
    from storage import R2Storage
    store = r2app.storage
    assert isinstance(store, R2Storage)
    assert backup.backup_due(store)                      # no backups yet
    key = backup.run_backup(r2app.DB_PATH, store, keep=2)
    assert key in keys(r2app)
    assert not backup.backup_due(store)                  # fresh backup exists
    import time
    assert backup.backup_due(store, now=time.time() + 24 * 3600)   # a day later
    for stamp in ("20200101-000000", "20200102-000000", "20200103-000000"):
        r2app.s3.put_object(Bucket=BUCKET, Key=f"backups/gallery-{stamp}.db", Body=b"x")
    backup.run_backup(r2app.DB_PATH, store, keep=2)
    assert len([k for k in keys(r2app) if k.startswith("backups/")]) == 2


def test_scheduler_runs_in_production_mode(tmp_path, monkeypatch, s3_server):
    """Without GALLERY_DEV the app starts the backup thread by itself."""
    import threading, time
    client = boto3.client("s3", endpoint_url=s3_server, aws_access_key_id="k",
                          aws_secret_access_key="s", region_name="us-east-1")
    try:
        client.create_bucket(Bucket=BUCKET)
    except Exception:
        pass
    for obj in client.list_objects_v2(Bucket=BUCKET).get("Contents", []):
        client.delete_object(Bucket=BUCKET, Key=obj["Key"])
    import backup
    monkeypatch.setattr(backup, "start_scheduler",
                        lambda db, store, log, **kw: backup.threading.Thread(
                            target=lambda: backup.run_backup(db, store)).start())
    appmod = load_app(
        tmp_path, monkeypatch, AUTO_BACKUP="1",
        R2_BUCKET=BUCKET, R2_ENDPOINT_URL=s3_server, R2_ACCESS_KEY_ID="k",
        R2_SECRET_ACCESS_KEY="s", R2_PUBLIC_URL=PUBLIC)
    for _ in range(50):
        found = [o["Key"] for o in client.list_objects_v2(Bucket=BUCKET).get("Contents", [])]
        if any(k.startswith("backups/gallery-") for k in found):
            break
        time.sleep(0.1)
    assert any(k.startswith("backups/gallery-") for k in found)


def test_real_scheduler_thread_makes_a_backup(r2app):
    import logging, time
    import backup
    t = backup.start_scheduler(r2app.DB_PATH, r2app.storage, logging.getLogger("t"),
                               first_delay=0, check_every=3600)
    for _ in range(50):
        if any(k.startswith("backups/gallery-") for k in keys(r2app)):
            break
        time.sleep(0.1)
    assert any(k.startswith("backups/gallery-") for k in keys(r2app))
    assert t.daemon
