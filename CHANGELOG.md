# Changelog

## Unreleased

- CI 运行全部离线 API 测试，并增加登录/权限、聊天结算/恢复、发布隔离和版本回滚的 Playwright 浏览器流程。
- README 增加稳定/实验/未验收状态表，新增 `docs/STATUS.md` 说明验收边界。
- 免费模型输入过滤拒绝裸合成 key（如 `test-key-not-real`），避免把疑似密钥发给匿名公共推理。

## 0.1.0 — 2026-10-30

首个公开版本。

### 站点

- Next.js 15 公开站点：首页、项目、技术栈、兴趣、博客、文档、日记
- 内容草稿 / 发布快照 / 版本恢复
- 公开/私密、RAG 授权、导航菜单
- 响应式、暖纸排版、PWA

### 后台

- 九模块管理
- Argon2id 登录、HttpOnly Cookie、CSRF
- 模型配置与密钥加密
- 内容、资料、导航、设备、日报、观测、备份
- 任务持久化、重试、容量报告

### AI

- OpenAI 兼容 Chat Completions
- RAG + sqlite-vec
- MCP / AG-UI 工具协议
- SSE 流式回答、可操作视图
- 配额、并发、缓存、取消、断流恢复

### 采集端

- Rust 核心 + SQLite 离线队列
- Windows Tauri
- Linux daemon + TUI
- PWA 采集台
- 截图、速记、上传、重试、暂停

### 部署

- Docker Compose：Nginx / Web / API / MinIO
- HTTP 最小部署
- 可选 ACME / TLS
- 备份/恢复 CLI
- MinIO 固定源码构建
