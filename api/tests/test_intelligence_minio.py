"""Opt-in real MinIO integration, always using a random private test bucket."""
import os
from datetime import timedelta
from urllib.parse import quote

import httpx
import pytest
import urllib3
from minio import Minio

from lyph import content, db, storage
from lyph.config import settings
from test_intelligence import service, owner_headers


@pytest.fixture
def real_minio(service, monkeypatch):
    names = ["LYPH_TEST_MINIO_ENDPOINT", "LYPH_TEST_MINIO_ACCESS_KEY", "LYPH_TEST_MINIO_SECRET_KEY"]
    if not all(os.environ.get(name) for name in names):
        pytest.skip("真实 MinIO 专项未配置 LYPH_TEST_MINIO_*；本项不构成服务验证")
    endpoint = os.environ[names[0]]
    secure = os.environ.get("LYPH_TEST_MINIO_SECURE", "false").lower() == "true"
    access, secret = os.environ[names[1]], os.environ[names[2]]
    bucket = "lyph-integration-" + db.uid()
    monkeypatch.setattr(settings, "minio_endpoint", endpoint)
    monkeypatch.setattr(settings, "minio_access_key", access)
    monkeypatch.setattr(settings, "minio_secret_key", secret)
    monkeypatch.setattr(settings, "minio_bucket", bucket)
    monkeypatch.setattr(settings, "minio_secure", secure)
    storage.set_test_adapter(None)
    origin = ("https://" if secure else "http://") + endpoint
    try:
        healthy = httpx.get(origin + "/minio/health/live", timeout=5, trust_env=False)
    except httpx.HTTPError as exc:
        pytest.fail("真实 MinIO 测试端点不可连接: " + type(exc).__name__)
    if healthy.status_code != 200:
        pytest.fail("真实 MinIO 测试端点未就绪")
    pool = urllib3.PoolManager(timeout=urllib3.Timeout(connect=5, read=15), retries=False)
    client = Minio(endpoint, access_key=access, secret_key=secret, secure=secure, http_client=pool)
    # Still a real SDK/network client; only tests impose short failure timeouts.
    monkeypatch.setattr(storage, "_client", lambda: client)
    client.make_bucket(bucket)
    try:
        yield service, client, bucket, origin
    finally:
        if client.bucket_exists(bucket):
            for item in client.list_objects(bucket, recursive=True):
                client.remove_object(bucket, item.object_name)
            client.remove_bucket(bucket)


def test_real_private_minio_upload_read_dedup_reference_and_signed_access(real_minio):
    service, client, bucket, origin = real_minio
    headers = owner_headers(service)
    data = b"real MinIO integration payload, private by default"
    first = storage.put(data, "private.txt", "text/plain")
    second = storage.put(data, "second.txt", "text/plain")
    assert first["id"] == second["id"] and storage.read(first["id"]) == data
    assert client.stat_object(bucket, first["object_key"]).size == len(data)
    # A bucket's original object is never accessible to an anonymous client.
    anonymous = httpx.get(origin + "/" + bucket + "/" + quote(first["object_key"], safe="/"), timeout=15)
    assert anonymous.status_code == 403
    signed = client.presigned_get_object(bucket, first["object_key"], expires=timedelta(seconds=30))
    authorized = httpx.get(signed, timeout=15)
    assert authorized.status_code == 200 and authorized.content == data
    draft = content.create_draft({"title": "MinIO protected file", "markdown": "attachment", "media_ids": [first["id"]]})
    assert service.delete(f"/api/v1/admin/files/{first['id']}", headers=headers).status_code == 409
    # Release the isolated fixture's reference, then exercise actual SDK deletion.
    with db.transaction() as conn:
        conn.execute("DELETE FROM file_references WHERE owner_id=?", (draft["id"],))
    assert storage.delete(first["id"])["deleted"]
    assert list(client.list_objects(bucket, recursive=True)) == []
