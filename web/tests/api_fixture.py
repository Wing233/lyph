"""E2E only: real FastAPI and SQLite, explicit object/provider SDK fixtures.
Never referenced by production startup; no production data or credentials are used.
"""
from pathlib import Path
import io
import os
import sys
import tempfile
import importlib.util

root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root / "api"))
temporary=tempfile.TemporaryDirectory(prefix="lyph-e2e-")
os.environ.update(LYPH_DATA_DIR=temporary.name,LYPH_SECRET_KEY="e2e-isolated-secret-" * 4,
    LYPH_FRONTEND_ORIGIN="http://127.0.0.1:3011",LYPH_SECURE_COOKIES="false",
    LYPH_SCHEDULER_ENABLED="false",LYPH_CHAT_IP_LIMIT="1000",LYPH_CHAT_UNLIMITED="false")
from argon2 import PasswordHasher
os.environ["LYPH_OWNER_PASSWORD_HASH"]=PasswordHasher().hash("e2e-owner-password")
from lyph import storage, models_routes
class ObjectFixture:
    def bucket_exists(self,bucket): return True
    def put_object(self,bucket,key,stream,size,content_type):
        target=Path(temporary.name)/"objects"/key
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(stream.read())
    def get_object(self,bucket,key):
        result=io.BytesIO((Path(temporary.name)/"objects"/key).read_bytes())
        result.release_conn=lambda: None
        return result
    def remove_object(self,bucket,key): (Path(temporary.name)/"objects"/key).unlink(missing_ok=True)
storage.set_test_adapter(ObjectFixture())
spec=importlib.util.spec_from_file_location("intelligence_test_fixture",root/"api/tests/test_intelligence.py")
fixture=importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
import httpx
models_routes.set_test_http_transport(httpx.MockTransport(fixture.ProviderFixture().handle))
from lyph.main import app
if __name__=="__main__":
    import uvicorn
    uvicorn.run(app,host="127.0.0.1",port=8011,log_level="warning")
