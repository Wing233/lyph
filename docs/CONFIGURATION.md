# 配置

配置来自根目录 `.env`（Compose）和后台数据库（站点/模型）。

## 生成 `.env`

推荐：

```bash
uv run --project api lyph init-config \
  --output .env \
  --origin http://127.0.0.1:8080 \
  --minio-endpoint minio:9000
```

它会生成：

- `LYPH_SECRET_KEY`（48 字符随机）
- `LYPH_OWNER_PASSWORD_HASH`（Argon2id）
- `LYPH_MINIO_ACCESS_KEY` / `LYPH_MINIO_SECRET_KEY`
- `.env` 权限 `0600`

已有 `.env` 时拒绝覆盖。

## 必填变量

| 变量 | 说明 |
|---|---|
| `LYPH_SECRET_KEY` | ≥32 字符；加密/签名 |
| `LYPH_OWNER_PASSWORD_HASH` | `$argon2id$...` |
| `LYPH_FRONTEND_ORIGIN` | 浏览器 origin，必须与访问地址一致 |
| `LYPH_SECURE_COOKIES` | 生产 `true`，本地 HTTP `false` |
| `LYPH_MINIO_ENDPOINT` | 如 `minio:9000` |
| `LYPH_MINIO_ACCESS_KEY` | MinIO access key |
| `LYPH_MINIO_SECRET_KEY` | MinIO secret key |
| `LYPH_MINIO_BUCKET` | 私有 bucket 名 |
| `LYPH_MINIO_SECURE` | 本地 `false`，生产 `true` |

## 部署变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `LYPH_BIND_ADDRESS` | `127.0.0.1` | Nginx 绑定地址 |
| `LYPH_HTTP_PORT` | `8080` | HTTP 端口 |
| `LYPH_PROXY_IP` | `172.30.91.254` | 私有网段 Nginx 地址 |
| `LYPH_PRIVATE_SUBNET` | `172.30.91.0/24` | 内部网段 |
| `FORWARDED_ALLOW_IPS` | `172.30.91.254` | 可信代理 |
| `LYPH_CERT_NAME` | — | TLS 证书域名/IP，TLS 模式必填 |

## 可选变量

| 变量 | 说明 |
|---|---|
| `LYPH_DATA_DIR` | 数据目录，默认 `./data` |
| `LYPH_DATABASE_PATH` | SQLite 路径 |
| `LYPH_SOURCE_DIR` | Web 源目录（容器内 `/source/web`） |
| `LYPH_UI_DIR` | UI 发布目录 |
| `LYPH_CHAT_FIRST_ENABLED` | 聊天优先首页 |
| `LYPH_PUBLIC_ORIGIN` | 公开 origin，用于元数据 |
| `LYPH_ENTERPRISE_DATABASE_PATH` | 可选企业快照 SQLite |
| `LYPH_DOCKER_GID` | 可选 UI 预览的 docker 组 |
| `LYPH_SCHEDULER_ENABLED` | 是否启动调度器 |
| `LYPH_CHAT_GLOBAL_CONCURRENCY` | 全局聊天并发 |
| `LYPH_CHAT_IP_LIMIT` | 单 IP 限额 |
| `LYPH_CHAT_UNLIMITED` | 无限制模式（谨慎） |

## 站点设置

后台 → 设置：

- 站点标题/描述/页脚
- 联系方式（微信等）
- 导航菜单（`navigation_items`）
- 首页模块开关
- 模型配置
- 聊天额度

设置写入 SQLite；联系方式不再硬编码在前端。

## 模型配置

后台 → 模型：

```text
name       DeepSeek
provider   openai-compatible
base_url   https://api.deepseek.com
model      deepseek-chat
api_key    sk-...
timeout    60
```

- 密钥 Fernet 加密存库
- 前端只显示掩码
- 模型行可测试
- 没有默认 key；未配置时聊天明确失败

## 企业快照（可选）

`chat_tools.py` 的企业工具需要一个只读 SQLite：

```bash
LYPH_ENTERPRISE_DATABASE_PATH=./public-apps/companies/data/web.sqlite
```

不配置时相关工具返回 `unavailable`，不会用演示数据填充。

## 文件与数据

| 路径 | 说明 |
|---|---|
| `.env` | 密钥，0600，禁止提交 |
| `data/` | SQLite、运行时 |
| `runtime/` | TLS、ACME、UI 发布、密码文件 |
| `backups/` | 加密备份 |
| `public-apps/` | 可选公开快照 |
| `web/.env.example` | 前端本地变量示例 |

`.gitignore` 已排除这些路径；不要强制 `git add`。
