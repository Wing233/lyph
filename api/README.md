# lyph API

FastAPI service for content, chat, devices, daily reports, and maintenance.

## Requirements

- Python 3.13
- SQLite (WAL)
- MinIO or any S3-compatible object store
- An OpenAI-compatible model endpoint (configured in the admin console)

## Local run

```bash
cd api
uv sync --frozen --extra test
uv run lyph init-config --output ../.env \
  --origin http://127.0.0.1:8080 \
  --minio-endpoint localhost:9000
uv run --env-file ../.env lyph init-db
uv run --env-file ../.env uvicorn lyph.main:app \
  --host 127.0.0.1 --port 8000 --workers 1
```

`init-config` generates `LYPH_SECRET_KEY` and an Argon2id owner password hash. It refuses to overwrite an existing `.env` and never prints secrets.

Only run **one uvicorn worker**: SQLite task recovery and the scheduler assume a single executor.

## Endpoints

- `GET /health` — process liveness
- `GET /ready` — SQLite + MinIO readiness
- `GET /docs` — OpenAPI UI
- `GET /openapi.json` — machine-readable contract
- `/api/v1/*` — auth, content, chat, devices, daily reports, admin

Owner write requests require the configured `Origin` and the `X-CSRF-Token` returned by login. Cookies are `HttpOnly`, `SameSite=Strict`, and `Secure` in production.

## Tests

```bash
uv sync --frozen --extra test
uv run pytest
```

Some integration tests require MinIO or a model endpoint; unit tests run without them.

## Data

- SQLite: `LYPH_DATABASE_PATH` (default `./data/lyph.sqlite3`)
- Objects: MinIO bucket `LYPH_MINIO_BUCKET`
- No default owner password, API key, or model key
- `.env`, `data/`, `runtime/`, `backups/`, and `public-apps/` are excluded from git

See the root [`README.md`](../README.md) and [`docs/`](../docs/) for architecture, configuration, deployment, and privacy.
