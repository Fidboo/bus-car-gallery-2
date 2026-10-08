import importlib
import io
import os
import sys

import pytest
from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

R2_VARS = ["R2_BUCKET", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
           "R2_PUBLIC_URL", "R2_ENDPOINT_URL"]


def load_app(tmp_path, monkeypatch, **env):
    """Import a fresh copy of app.py with its own empty data folder."""
    for var in R2_VARS + ["SECRET_KEY", "ADMIN_PASSWORD_HASH", "UPLOAD_PASSWORD",
                          "TRUST_CLOUDFLARE", "ORIGIN_SECRET"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GALLERY_DEV", "1")
    monkeypatch.setenv("UPLOAD_PASSWORD", "correct-horse-battery")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import app as app_module
    importlib.reload(app_module)
    app_module.app.config["TESTING"] = True
    return app_module


@pytest.fixture
def appmod(tmp_path, monkeypatch):
    return load_app(tmp_path, monkeypatch)


def make_jpeg(size=(3000, 2000), color=(200, 60, 30), taken="2023:05:17 12:00:00"):
    img = Image.new("RGB", size, color)
    for x in range(0, size[0], 97):          # some detail so it isn't a flat colour
        for y in range(0, size[1], 89):
            img.putpixel((x, y), (x % 255, y % 255, 128))
    exif = Image.Exif()
    if taken:
        exif[306] = taken
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92, exif=exif.tobytes())
    return buf.getvalue()


def csrf_from(client):
    """Load a page so the session has a CSRF token, and return it."""
    client.get("/login")
    with client.session_transaction() as s:
        return s["_csrf"]


def login(client, password="correct-horse-battery"):
    token = csrf_from(client)
    return client.post("/login", data={"password": password, "csrf_token": token})


def upload(client, data=None, name="bus.jpg", category="bus", title="", tags="", token=None):
    if token is None:
        with client.session_transaction() as s:
            token = s["_csrf"]
    return client.post(
        "/upload/file",
        data={"photo": (io.BytesIO(data if data is not None else make_jpeg()), name),
              "category": category, "title": title, "tags": tags},
        headers={"X-CSRF-Token": token},
        content_type="multipart/form-data",
    )
