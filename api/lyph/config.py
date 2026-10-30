"""Configuration is explicit: no demonstration credentials can log in."""
from dataclasses import dataclass, field
import os
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv("LYPH_" + name, str(default)).lower() in {"1", "true", "yes"}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("LYPH_DATA_DIR", "./data")))
    database_path: Path = field(default_factory=lambda: Path(os.getenv("LYPH_DATABASE_PATH", os.getenv("LYPH_DATA_DIR", "./data") + "/lyph.sqlite3")))
    secret_key: str = field(default_factory=lambda: os.getenv("LYPH_SECRET_KEY", ""))
    owner_password_hash: str = field(default_factory=lambda: os.getenv("LYPH_OWNER_PASSWORD_HASH", ""))
    frontend_origin: str = field(default_factory=lambda: os.getenv("LYPH_FRONTEND_ORIGIN", "http://localhost:3000").rstrip("/"))
    secure_cookies: bool = field(default_factory=lambda: _bool("SECURE_COOKIES", True))
    minio_endpoint: str = field(default_factory=lambda: os.getenv("LYPH_MINIO_ENDPOINT", "minio:9000"))
    minio_access_key: str = field(default_factory=lambda: os.getenv("LYPH_MINIO_ACCESS_KEY", ""))
    minio_secret_key: str = field(default_factory=lambda: os.getenv("LYPH_MINIO_SECRET_KEY", ""))
    minio_bucket: str = field(default_factory=lambda: os.getenv("LYPH_MINIO_BUCKET", "lyph-private"))
    minio_secure: bool = field(default_factory=lambda: _bool("MINIO_SECURE"))
    max_upload_bytes: int = 20 * 1024 * 1024
    owner_session_hours: int = 8
    owner_idle_minutes: int = 30
    scheduler_enabled: bool = field(default_factory=lambda: _bool("SCHEDULER_ENABLED", True))
    source_dir: Path = field(default_factory=lambda: Path(os.getenv("LYPH_SOURCE_DIR", "../web")))

    @property
    def cookie_secure(self) -> bool:
        return self.secure_cookies

    def validate(self) -> None:
        if len(self.secret_key) < 32:
            raise RuntimeError("LYPH_SECRET_KEY must contain at least 32 random characters; run lyph init-config")
        if not self.owner_password_hash.startswith("$argon2id$"):
            raise RuntimeError("LYPH_OWNER_PASSWORD_HASH must be an Argon2id hash; run lyph init-config")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
