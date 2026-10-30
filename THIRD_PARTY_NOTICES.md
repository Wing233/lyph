# Third-Party Notices

LYPH 使用或依赖以下开源项目。各项目版权归其作者所有，遵循各自许可证。

## 重点：MinIO（AGPL-3.0）

`deploy/Minio.Dockerfile` 从固定 commit 构建 MinIO。MinIO 服务端使用 **GNU AGPL-3.0**，不属于本项目的 MIT 授权范围。

如果你分发包含 MinIO 的镜像/二进制，需要遵守 AGPL-3.0，包括提供对应源码的获取方式。构建文件中的 commit 就是对应源码版本。

- 源码：<https://github.com/minio/minio>
- 许可证：<https://github.com/minio/minio/blob/master/LICENSE>

## Web

| 项目 | 许可证 |
|---|---|
| Next.js | MIT |
| React / React DOM | MIT |
| Tailwind CSS | MIT |
| assistant-ui | MIT |
| Radix UI | MIT |
| Lucide | ISC |
| react-markdown / remark-gfm | MIT |
| rehype-sanitize | MIT |
| idb | ISC |
| clsx / tailwind-merge / CVA | MIT |
| Playfair Display | SIL OFL-1.1（见 `web/public/playfair-OFL.txt`） |

## API

| 项目 | 许可证 |
|---|---|
| FastAPI | MIT |
| Uvicorn | BSD-3-Clause |
| Pydantic | MIT |
| APScheduler | MIT |
| cryptography | Apache-2.0 / BSD-3-Clause |
| minio-py | Apache-2.0 |
| sqlite-vec | MIT / Apache-2.0 |
| langchain-text-splitters | MIT |
| pypdf | BSD-3-Clause |
| python-docx | MIT |
| docker-py | Apache-2.0 |
| MCP Python SDK | MIT |
| AG-UI protocol | MIT |
| jsonschema | MIT |
| OpenAI Python SDK | Apache-2.0 |
| httpx | BSD-3-Clause |
| SQLite | Public Domain |

## Collectors

| 项目 | 许可证 |
|---|---|
| Tauri | MIT / Apache-2.0 |
| Ratatui | MIT |
| rusqlite | MIT |
| reqwest | MIT / Apache-2.0 |
| xcap | Apache-2.0 |
| WebView2 | Microsoft 许可 |

## 部署工具

| 项目 | 许可证 |
|---|---|
| Nginx | BSD-2-Clause |
| Certbot | Apache-2.0 |
| Docker 基础镜像 | 各自许可证 |

## 外部服务

以下服务不随本项目分发：

- OpenAI 兼容模型端点
- AI Horde（可选公共匿名推理）
- Let's Encrypt / Certbot
- GitHub / npm / PyPI / crates.io

使用前请遵守各自服务条款。

## 字体

`web/public/playfair-latin.woff2` 来自 Playfair Display，遵循 SIL OFL-1.1；许可证随字体文件一起保留。
