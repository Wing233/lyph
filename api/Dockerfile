FROM python:3.13-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81
ENV PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir uv==0.8.22
WORKDIR /app
COPY api/pyproject.toml api/uv.lock ./
COPY api/lyph ./lyph
RUN uv sync --frozen --no-dev && groupadd -g 10001 lyph && useradd -u 10001 -g lyph -M lyph && mkdir -p /data /ui && chown lyph:lyph /data /ui
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "lyph.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
