"""
Storage layer for the photo files.

Two backends with the same small interface:

  LocalStorage  - files live in DATA_DIR/uploads and DATA_DIR/thumbnails
                  (what the app has always done; used for local testing).
  R2Storage     - files live in a Cloudflare R2 bucket under the keys
                  "uploads/<name>" and "thumbnails/<name>". Visitors' browsers
                  load them straight from R2 through a public custom domain,
                  so the Flask server never carries image traffic.

R2 is chosen automatically when the R2_BUCKET environment variable is set.

"area" is always either "uploads" (the full-size originals) or "thumbnails".
"""
import mimetypes
import os
import shutil
from urllib.parse import quote

AREAS = ("uploads", "thumbnails")

# File names are random UUIDs and never change, so browsers/CDN may cache
# them "forever". (A replaced photo gets a brand new name.)
CACHE_CONTROL = "public, max-age=31536000, immutable"


def _content_type(name):
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _attachment_header(download_name):
    """Content-Disposition value that works for any file name."""
    ascii_name = download_name.encode("ascii", "ignore").decode() or "photo"
    ascii_name = ascii_name.replace('"', "").replace("\\", "")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(download_name)}"


class LocalStorage:
    kind = "local"

    def __init__(self, upload_dir, thumb_dir):
        self.dirs = {"uploads": upload_dir, "thumbnails": thumb_dir}
        for d in self.dirs.values():
            os.makedirs(d, exist_ok=True)

    def path(self, area, name):
        return os.path.join(self.dirs[area], name)

    def put(self, area, name, src_path):
        """Move a finished temp file into place (same filesystem -> atomic)."""
        shutil.move(src_path, self.path(area, name))

    def delete(self, area, name):
        try:
            os.remove(self.path(area, name))
        except FileNotFoundError:
            pass

    def public_url(self, area, name):
        return None  # the app serves local files itself

    def download_url(self, area, name, download_name):
        return None  # the app streams local files itself


class R2Storage:
    kind = "r2"

    def __init__(self, bucket, public_url, access_key_id, secret_access_key,
                 account_id=None, endpoint_url=None):
        import boto3
        from botocore.config import Config

        if not endpoint_url:
            endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
        self.bucket = bucket
        self.public_base = public_url.rstrip("/")

        cfg = dict(
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "standard"},
            s3={"addressing_style": "path"},
        )
        try:
            # boto3 >= 1.36 adds checksum headers by default that R2 can
            # reject; ask it to only do so when an operation requires it.
            config = Config(
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                **cfg,
            )
        except TypeError:  # older botocore doesn't know these options
            config = Config(**cfg)

        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=config,
        )

    @staticmethod
    def key(area, name):
        return f"{area}/{name}"

    def put(self, area, name, src_path):
        self.client.upload_file(
            src_path, self.bucket, self.key(area, name),
            ExtraArgs={"ContentType": _content_type(name), "CacheControl": CACHE_CONTROL},
        )
        os.remove(src_path)

    def delete(self, area, name):
        self.client.delete_object(Bucket=self.bucket, Key=self.key(area, name))

    def public_url(self, area, name):
        return f"{self.public_base}/{self.key(area, name)}"

    def download_url(self, area, name, download_name):
        """Short-lived signed link that makes the browser *save* the file
        (a plain link to another domain would just open it)."""
        return self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": self.key(area, name),
                "ResponseContentDisposition": _attachment_header(download_name),
            },
            ExpiresIn=300,
        )

    # --- helpers used by the tools/ scripts -------------------------------
    def list_keys(self, prefix):
        keys = set()
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.add(obj["Key"])
        return keys

    def upload_raw(self, key, src_path, content_type=None, cache=True):
        extra = {"ContentType": content_type or _content_type(key)}
        if cache:
            extra["CacheControl"] = CACHE_CONTROL
        self.client.upload_file(src_path, self.bucket, key, ExtraArgs=extra)


def storage_from_env(upload_dir, thumb_dir):
    """R2 if R2_BUCKET is set, otherwise plain local folders."""
    bucket = os.environ.get("R2_BUCKET", "").strip()
    if not bucket:
        return LocalStorage(upload_dir, thumb_dir)

    needed = ["R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_PUBLIC_URL"]
    if not os.environ.get("R2_ENDPOINT_URL"):
        needed.append("R2_ACCOUNT_ID")
    missing = [n for n in needed if not os.environ.get(n, "").strip()]
    if missing:
        raise RuntimeError(
            "R2_BUCKET is set, but these settings are missing: " + ", ".join(missing)
        )
    return R2Storage(
        bucket=bucket,
        public_url=os.environ["R2_PUBLIC_URL"].strip(),
        access_key_id=os.environ["R2_ACCESS_KEY_ID"].strip(),
        secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"].strip(),
        account_id=os.environ.get("R2_ACCOUNT_ID", "").strip() or None,
        endpoint_url=os.environ.get("R2_ENDPOINT_URL", "").strip() or None,
    )
