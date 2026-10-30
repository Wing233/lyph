# 部署

## 推荐拓扑

```text
Internet
  │ HTTPS
  ▼
Nginx (compose)
  ├── /api/  → FastAPI
  └── /      → Next.js
                │
                ├── SQLite (data/)
                ├── MinIO (objects)
                └── 模型 API
```

## 最小 HTTP 部署

```bash
cp .env.example .env      # 或使用 init-config
docker compose build minio api web
docker compose up -d
```

默认：

- Nginx：`127.0.0.1:8080`
- API：内部 `api:8000`
- Web：内部 `web:3000`
- MinIO：内部 `minio:9000`

打开 `http://127.0.0.1:8080/`，后台 `/admin/login`。

## TLS

首次证书：

```bash
docker compose -f compose.yaml -f compose.acme.yaml up -d
```

正式 TLS：

```bash
docker compose -f compose.yaml -f compose.tls.yaml up -d
```

要求：

- `LYPH_CERT_NAME` 必填（域名或 IP）
- 证书放 `runtime/tls/`
- ACME challenge 放 `runtime/acme/`
- 80/443 对外，8080 只绑定 `127.0.0.1`

`deploy/install-certificate.sh` / `deploy/renew-certificate.sh` 是可选 Certbot 脚本；默认不包含任何真实域名/IP。

## 反向代理

如果外层还有 Nginx/Caddy：

```nginx
server {
    listen 443 ssl http2;
    server_name lyph.example.com;
    client_max_body_size 32m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;
    }
}
```

把 Compose 的 `LYPH_BIND_ADDRESS` 改成 `127.0.0.1`，只暴露外层代理。

## 资源建议

| 资源 | 最小 | 建议 |
|---|---|---|
| CPU | 1 vCPU | 2 vCPU |
| RAM | 1 GB | 2 GB |
| 磁盘 | 10 GB | 40 GB+（截图/对象） |
| 模型 | 外部 API | 外部 API 或本地推理 |

MinIO 是独立 AGPL-3.0 组件；详见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

## 备份

```bash
uv run --project api --env-file .env lyph backup
```

备份是加密的 `.lyph.enc`，包含 SQLite/对象/源码清单。恢复前停止 API 和任务 worker。

```bash
uv run --project api --env-file .env lyph restore backups/<file>.lyph.enc
```

恢复后旧 session/授权会失效；需要重新登录/配对设备。

## 升级

```bash
git pull
docker compose build minio api web
docker compose up -d
```

升级前备份。数据库只做增量迁移，不自动删列。

## UI 预览/发布（可选，高风险）

`compose.ui.yaml` 会挂载 `/var/run/docker.sock`，用于后台预览/发布静态 UI。

- 只在你信任的环境启用
- 不要把 Docker socket 暴露给公网
- 建议单独主机/单独用户
- 默认不启用

## 容量与恢复

`deploy/` 里保留了最小 TLS/MinIO 脚本，不再包含生产验收证据。容量报告/恢复演练应在你的环境重新生成。

## 升级检查

- [ ] 备份 `.env`、`data/`、`runtime/`
- [ ] `docker compose config`
- [ ] `docker compose build`
- [ ] `curl /health`、`curl /ready`
- [ ] 后台登录
- [ ] 公开首页、聊天、采集端各测一次
